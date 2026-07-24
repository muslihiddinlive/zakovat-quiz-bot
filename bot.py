# bot.py
# ------------------------------------------------------------
# "Zakovat Quiz" turniri uchun asosiy Telegram bot kodi (Aiogram 3.x)
#
# Ishga tushirish:
#   pip install aiogram aiosqlite openpyxl
#   python bot.py
# ------------------------------------------------------------
import asyncio
import html
import io
import json
import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus, ContentType
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    BufferedInputFile,
    TelegramObject,
)

import config
import database as db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


# ==================================================================
#  FSM HOLATLARI
# ==================================================================

class RegisterStates(StatesGroup):
    waiting_fullname = State()


class AnswerStates(StatesGroup):
    waiting_answer = State()


# ==================================================================
#  KLAVIATURALAR
# ==================================================================

async def main_menu_kb() -> ReplyKeyboardMarkup:
    """Bosh menyu (Reply klaviatura) - turnir holatiga qarab dinamik shakllanadi.

    - Turnir hali boshlanmagan bo'lsa: faqat "Qoidalar" va "Natijalarim" ko'rinadi.
    - Turnir boshlangan bo'lsa: "Javob yuborish" qo'shiladi.
    - Faol raund aynan 4-raund (Web App) bo'lsagina "Tez yozish" tugmasi chiqadi -
      ya'ni admin shu raundni boshlamaguncha, WebApp tugmasi umuman ko'rinmaydi.
    """
    tournament_active = await db.is_tournament_active()
    active_round = await db.get_active_round()

    rows = []
    if tournament_active and active_round:
        rows.append([KeyboardButton(text="📤 Javob yuborish")])
        if active_round == 4:
            rows.append(
                [KeyboardButton(text="⌨️ Tez yozish (Web App)", web_app=WebAppInfo(url=config.WEBAPP_URL))]
            )
    rows.append([KeyboardButton(text="ℹ️ Qoidalar"), KeyboardButton(text="📊 Mening natijalarim")])

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def subscribe_kb() -> InlineKeyboardMarkup:
    """Obuna bo'lish va tekshirish tugmalari."""
    channel_link = f"https://t.me/{config.CHANNEL_USERNAME.lstrip('@')}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Kanalga obuna bo'lish", url=channel_link)],
            [InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_sub")],
        ]
    )


def admin_panel_kb(tournament_active: bool, active_round: int) -> InlineKeyboardMarkup:
    rows = []
    if tournament_active:
        rows.append([InlineKeyboardButton(text="⏹ Turnirni to'xtatish", callback_data="adm_stop")])
    else:
        rows.append([InlineKeyboardButton(text="▶️ Turnirni boshlash", callback_data="adm_start")])

    round_label = config.ROUNDS.get(active_round, {}).get("name") if active_round else None
    rows.append([InlineKeyboardButton(
        text=f"🔀 Raund boshlash" + (f" (hozir: {round_label})" if round_label else ""),
        callback_data="adm_pick_round",
    )])
    rows.append([InlineKeyboardButton(text="📥 Excel yuklab olish", callback_data="adm_export")])
    rows.append([InlineKeyboardButton(text="🗑 Javoblar tarixini tozalash", callback_data="adm_clear_answers")])
    rows.append([InlineKeyboardButton(text="⚠️ To'liq tozalash (userlar ham)", callback_data="adm_clear_all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_round_picker_kb() -> InlineKeyboardMarkup:
    """Admin qaysi raundni faol qilishni tanlashi uchun."""
    rows = [
        [InlineKeyboardButton(text=info["name"], callback_data=f"adm_setround_{num}")]
        for num, info in config.ROUNDS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==================================================================
#  MAJBURIY OBUNA MIDDLEWARE
# ==================================================================

async def is_subscribed(user_id: int) -> bool:
    """Foydalanuvchi @ParadoksHub kanaliga obuna ekanini tekshiradi."""
    try:
        member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id)
        return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    except Exception as e:
        # Bot kanalda admin bo'lmasa yoki Telegram API vaqtincha ishlamasa,
        # bu yerda xatolik chiqadi. Fail-closed (False) qaytarilsa, texnik
        # nosozlik butun botni HAMMA uchun ishlamay qo'yadi - shuning uchun
        # fail-open qilib True qaytaramiz va xatolikni logga yozamiz.
        logger.warning(f"Obunani tekshirishda xatolik (fail-open, ruxsat berildi): {e}")
        return True


class SubscriptionMiddleware(BaseMiddleware):
    """Har bir xabar/callback kelganda kanalga obunani tekshiradi."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        # "Obunani tekshirish" tugmasi har doim o'tishi kerak
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        if await is_subscribed(user.id):
            return await handler(event, data)

        # Obuna emas -> bloklaymiz
        text = (
            "🚫 Botdan foydalanish uchun avval quyidagi kanalga obuna bo'ling:\n"
            f"{config.CHANNEL_USERNAME}\n\n"
            "Obuna bo'lgach, pastdagi tugmani bosing 👇"
        )
        if isinstance(event, Message):
            await event.answer(text, reply_markup=subscribe_kb())
        elif isinstance(event, CallbackQuery):
            await event.answer("Avval kanalga obuna bo'ling!", show_alert=True)
            await event.message.answer(text, reply_markup=subscribe_kb())
        return None  # handler ishga tushmaydi


dp.message.middleware(SubscriptionMiddleware())
dp.callback_query.middleware(SubscriptionMiddleware())


# ==================================================================
#  /start VA RO'YXATDAN O'TISH
# ==================================================================

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if await db.is_registered(message.from_user.id):
        await message.answer(
            f"Assalomu alaykum, {message.from_user.full_name}! 👋\n"
            "Zakovat Quiz botiga xush kelibsiz.",
            reply_markup=await main_menu_kb(),
        )
        return

    await message.answer(
        "🎉 <b>Zakovat Quiz</b> turniriga xush kelibsiz!\n\n"
        "Ro'yxatdan o'tish uchun to'liq Ism-Familiyangizni yuboring:"
    )
    await state.set_state(RegisterStates.waiting_fullname)


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, state: FSMContext):
    if await is_subscribed(call.from_user.id):
        await call.message.delete()
        if await db.is_registered(call.from_user.id):
            await call.message.answer("✅ Obuna tasdiqlandi! Bosh menyu:", reply_markup=await main_menu_kb())
        else:
            await call.message.answer(
                "✅ Obuna tasdiqlandi!\n\nRo'yxatdan o'tish uchun to'liq Ism-Familiyangizni yuboring:"
            )
            await state.set_state(RegisterStates.waiting_fullname)
    else:
        await call.answer("❌ Siz hali obuna bo'lmagansiz!", show_alert=True)


@dp.message(StateFilter(RegisterStates.waiting_fullname))
async def process_fullname(message: Message, state: FSMContext):
    full_name = message.text.strip() if message.text else ""
    if len(full_name) < 3:
        await message.answer("❗️ Iltimos, to'liq Ism-Familiyangizni to'g'ri kiriting:")
        return

    await db.add_user(message.from_user.id, full_name, message.from_user.username)
    await state.clear()
    await message.answer(
        f"✅ Ro'yxatdan muvaffaqiyatli o'tdingiz, <b>{html.escape(full_name)}</b>!\n\n"
        "Endi turnir raundlari bo'yicha javob yuborishingiz mumkin.",
        reply_markup=await main_menu_kb(),
    )
    await notify_admins_new_registration(message.from_user, full_name)


async def notify_admins_new_registration(user, full_name: str) -> None:
    """Yangi ro'yxatdan o'tgan foydalanuvchi haqida adminlarga xabar beradi."""
    safe_full_name = html.escape(full_name)
    safe_username = html.escape(user.username) if user.username else "yoq"
    text = (
        f"🆕 <b>Yangi ro'yxatdan o'tish!</b>\n"
        f"👤 Ism-Familiya: {safe_full_name}\n"
        f"🔗 Username: @{safe_username}\n"
        f"🆔 Telegram ID: <code>{user.id}</code>"
    )
    targets = [config.ADMIN_GROUP_ID]
    if config.PERSONAL_CHAT_ID:
        targets.append(config.PERSONAL_CHAT_ID)
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, text)
        except Exception as e:
            logger.error(f"Ro'yxatdan o'tish xabarini {chat_id}'ga yuborishda xatolik: {e}")


# ==================================================================
#  QOIDALAR / NATIJALAR
# ==================================================================

@dp.message(F.text == "ℹ️ Qoidalar")
async def show_rules(message: Message):
    rounds_text = "\n".join(f"{num}. {info['name']}" for num, info in config.ROUNDS.items())
    await message.answer(
        "📜 <b>Zakovat Quiz - Qoidalar</b>\n\n"
        f"Turnir {len(config.ROUNDS)} ta raunddan iborat:\n{rounds_text}\n\n"
        "Har bir raund uchun topshiriq kanal/guruhga e'lon qilinadi. "
        "Javobingizni esa aynan shu botga \"📤 Javob yuborish\" tugmasi orqali yuboring.\n\n"
        "⌨️ 4-raund (Tez yozish) uchun Web App orqali o'yin ochiladi va natija avtomatik yuboriladi."
    )


@dp.message(F.text == "📊 Mening natijalarim")
async def my_results(message: Message):
    answers = await db.get_user_answers(message.from_user.id)
    if not answers:
        await message.answer("Siz hali hech qanday javob yubormagansiz.")
        return

    lines = ["📊 <b>Sizning yuborgan javoblaringiz:</b>\n"]
    for a in answers:
        round_name = config.ROUNDS.get(a["round_num"], {}).get("name", f"{a['round_num']}-raund")
        extra = ""
        if a["wpm"] is not None:
            extra = f" | WPM: {a['wpm']:.1f}, vaqt: {a['time_sec']:.1f}s"
        lines.append(f"• {round_name} — {a['content_type']}{extra} ({a['submitted_at']})")
    await message.answer("\n".join(lines))


# ==================================================================
#  JAVOB YUBORISH OQIMI
# ==================================================================

@dp.message(F.text == "📤 Javob yuborish")
async def start_answer_flow(message: Message, state: FSMContext):
    if not await db.is_registered(message.from_user.id):
        await message.answer("Avval /start orqali ro'yxatdan o'ting.")
        return
    if not await db.is_tournament_active():
        await message.answer("⏸ Hozircha turnir faol emas. Admin turnirni boshlashini kuting.")
        return

    round_num = await db.get_active_round()
    if not round_num:
        await message.answer("Hozircha faol raund yo'q. Admin raund boshlashini kuting.")
        return

    if round_num == 4:
        await message.answer(
            "⌨️ Hozirgi faol raund - Tez yozish (Web App). "
            "Javob yuborish uchun bosh menyudagi \"⌨️ Tez yozish\" tugmasidan foydalaning."
        )
        return

    if await db.has_answered(message.from_user.id, round_num):
        await message.answer("⚠️ Siz bu raundda allaqachon ishtirok etgansiz, qayta yuborib bo'lmaydi.")
        return

    info = config.ROUNDS[round_num]
    await state.update_data(round_num=round_num)
    await state.set_state(AnswerStates.waiting_answer)
    await message.answer(
        f"✏️ <b>{info['name']}</b>\n\n"
        f"{info['hint']}\n\n"
        "Javobingizni pastga yuboring 👇"
    )


@dp.message(
    StateFilter(AnswerStates.waiting_answer),
    F.content_type.in_({
        ContentType.TEXT, ContentType.PHOTO, ContentType.STICKER,
        ContentType.VOICE, ContentType.AUDIO,
    }),
)
async def receive_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    round_num = data.get("round_num")
    if round_num is None:
        await state.clear()
        return

    content_type = None
    text_content = None
    file_id = None

    if message.photo:
        file_id = message.photo[-1].file_id
        if message.caption:
            content_type = "photo_text"
            text_content = message.caption
        else:
            content_type = "photo"
    elif message.sticker:
        content_type = "sticker"
        file_id = message.sticker.file_id
    elif message.voice:
        content_type = "voice"
        file_id = message.voice.file_id
    elif message.audio:
        content_type = "audio"
        file_id = message.audio.file_id
    elif message.text:
        content_type = "text"
        text_content = message.text

    answer_id = await db.add_answer(
        tg_id=message.from_user.id,
        round_num=round_num,
        content_type=content_type,
        text_content=text_content,
        file_id=file_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
    )

    await state.clear()
    await message.answer(
        "✅ Javobingiz qabul qilindi va hakamlarga yuborildi!",
        reply_markup=await main_menu_kb(),
    )

    await notify_admins_new_answer(
        answer_id=answer_id, user=message.from_user, round_num=round_num, content_type=content_type,
    )


async def notify_admins_new_answer(answer_id: int, user, round_num: int, content_type: str):
    """Javob kelganini adminlarga QISQA xabar bilan bildiradi - to'liq kontent
    guruhni to'ldirib yubormasligi uchun, faqat "Ko'rish" bosilganda ochiladi."""
    safe_full_name = html.escape(user.full_name or "")
    safe_username = html.escape(user.username) if user.username else "yoq"
    round_name = config.ROUNDS.get(round_num, {}).get("name", f"{round_num}-raund")

    text = (
        f"📩 <b>Yangi javob keldi!</b>\n"
        f"🎯 Raund: {round_name}\n"
        f"👤 {safe_full_name} (@{safe_username}, ID: {user.id})\n"
        f"📎 Turi: {content_type}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="👁 Ko'rish", callback_data=f"adm_view_{answer_id}")]]
    )

    targets = [config.ADMIN_GROUP_ID]
    if config.PERSONAL_CHAT_ID:
        targets.append(config.PERSONAL_CHAT_ID)

    for chat_id in targets:
        try:
            await bot.send_message(chat_id, text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Xabarnomani {chat_id}'ga yuborishda xatolik: {e}")


@dp.callback_query(F.data.startswith("adm_view_"))
async def adm_view_answer(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)

    answer_id = int(call.data.split("_")[-1])
    answer = await db.get_answer_by_id(answer_id)
    if not answer or not answer["chat_id"] or not answer["message_id"]:
        await call.answer("Bu javob topilmadi (ehtimol tarix tozalangan).", show_alert=True)
        return

    try:
        await bot.copy_message(
            chat_id=call.message.chat.id,
            from_chat_id=answer["chat_id"],
            message_id=answer["message_id"],
        )
        await call.answer()
    except Exception as e:
        logger.error(f"Javobni ko'rsatishda xatolik: {e}")
        await call.answer("❌ Javobni ko'rsatib bo'lmadi.", show_alert=True)


# ==================================================================
#  WEB APP DAN KELGAN MA'LUMOT (Tez yozish raundi - 4-raund)
# ==================================================================

@dp.message(F.content_type == ContentType.WEB_APP_DATA)
async def handle_webapp_data(message: Message):
    if not await db.is_round_enabled(4):
        await message.answer("Bu raund hozir yopiq, natija qabul qilinmadi.")
        return

    if await db.has_answered(message.from_user.id, 4):
        await message.answer(
            "⚠️ Siz 4-raundda allaqachon ishtirok etgansiz. "
            "Har bir ishtirokchi faqat bir marta urinib ko'ra oladi, "
            "shuning uchun yangi natija qabul qilinmaydi."
        )
        return

    try:
        payload = json.loads(message.web_app_data.data)
        wpm = float(payload.get("wpm", 0))
        time_sec = float(payload.get("time_sec", 0))
        typed_text = payload.get("text", "")
    except Exception as e:
        logger.error(f"WebApp data parse xatosi: {e}")
        await message.answer("❌ Natijani o'qishda xatolik yuz berdi. Qaytadan urinib ko'ring.")
        return

    await db.add_answer(
        tg_id=message.from_user.id,
        round_num=4,
        content_type="webapp_typing",
        text_content=typed_text,
        wpm=wpm,
        time_sec=time_sec,
    )

    await message.answer(
        f"✅ Natijangiz qabul qilindi!\n⌨️ Tezlik: <b>{wpm:.1f} WPM</b>\n⏱ Vaqt: <b>{time_sec:.1f} soniya</b>",
        reply_markup=await main_menu_kb(),
    )

    user = message.from_user
    safe_full_name = html.escape(user.full_name or "")
    safe_username = html.escape(user.username) if user.username else "yoq"
    result_text = (
        f"#Raund_4\n"
        f"👤 Qatnashchi: {safe_full_name} (@{safe_username}, ID: {user.id})\n"
        f"⌨️ WPM: {wpm:.1f} | Vaqt: {time_sec:.1f}s"
    )
    targets = [config.ADMIN_GROUP_ID]
    if config.PERSONAL_CHAT_ID:
        targets.append(config.PERSONAL_CHAT_ID)
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, result_text)
        except Exception as e:
            logger.error(f"4-raund natijasini {chat_id}'ga yuborishda xatolik: {e}")


# ==================================================================
#  ADMIN PANEL
# ==================================================================

def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


async def admin_panel_text() -> str:
    tournament_active = await db.is_tournament_active()
    active_round = await db.get_active_round()
    status = "🟢 Faol" if tournament_active else "🔴 Faol emas"
    round_text = config.ROUNDS.get(active_round, {}).get("name", "yo'q") if active_round else "yo'q"
    return f"🛠 <b>Admin panel</b>\nTurnir holati: {status}\nHozirgi faol raund: {round_text}"


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Bu buyruq faqat adminlar uchun.")
        return
    tournament_active = await db.is_tournament_active()
    active_round = await db.get_active_round()
    await message.answer(await admin_panel_text(), reply_markup=admin_panel_kb(tournament_active, active_round))


@dp.callback_query(F.data == "adm_start")
async def adm_start(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.set_tournament_active(True)
    await call.message.edit_text(
        await admin_panel_text(), reply_markup=admin_panel_kb(True, await db.get_active_round())
    )
    await call.answer("Turnir boshlandi! Endi \"🔀 Raund boshlash\"dan birinchi raundni tanlang.")


@dp.callback_query(F.data == "adm_stop")
async def adm_stop(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.set_tournament_active(False)
    await call.message.edit_text(
        await admin_panel_text(), reply_markup=admin_panel_kb(False, 0)
    )
    await call.answer("Turnir to'xtatildi!")
    await broadcast_to_users("⏹ Turnir vaqtincha to'xtatildi. Admin qayta boshlashini kuting.")


@dp.callback_query(F.data == "adm_back")
async def adm_back(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    tournament_active = await db.is_tournament_active()
    active_round = await db.get_active_round()
    await call.message.edit_text(
        await admin_panel_text(), reply_markup=admin_panel_kb(tournament_active, active_round)
    )
    await call.answer()


@dp.callback_query(F.data == "adm_pick_round")
async def adm_pick_round(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    if not await db.is_tournament_active():
        return await call.answer("Avval turnirni boshlang!", show_alert=True)
    await call.message.edit_text(
        "🔀 <b>Qaysi raundni faol qilmoqchisiz?</b>\n\n"
        "⚠️ Yangi raund tanlanishi bilan hozirgi faol raund AVTOMATIK yopiladi "
        "va foydalanuvchilarga xabar (+ yangilangan tugmalar) yuboriladi.",
        reply_markup=admin_round_picker_kb(),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm_setround_"))
async def adm_set_round(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    round_num = int(call.data.split("_")[-1])
    await db.set_active_round(round_num)

    await call.message.edit_text(
        await admin_panel_text(), reply_markup=admin_panel_kb(True, round_num)
    )
    await call.answer(f"{config.ROUNDS[round_num]['name']} boshlandi!")

    info = config.ROUNDS[round_num]
    if round_num == 4:
        broadcast_text = (
            f"🎬 Yangi raund boshlandi: <b>{info['name']}</b>\n\n"
            "Bosh menyudagi \"⌨️ Tez yozish\" tugmasini bosib ishtirok eting!"
        )
    else:
        broadcast_text = (
            f"🎬 Yangi raund boshlandi: <b>{info['name']}</b>\n\n"
            f"{info['hint']}\n\n"
            "Javobingizni \"📤 Javob yuborish\" tugmasi orqali yuboring!"
        )
    await broadcast_to_users(broadcast_text)


async def broadcast_to_users(text: str) -> None:
    """Barcha ro'yxatdan o'tgan foydalanuvchilarga xabar yuboradi (yangilangan
    tugmalar bilan), masalan yangi raund boshlanganda."""
    kb = await main_menu_kb()
    user_ids = await db.get_all_user_ids()
    sent, failed = 0, 0
    for tg_id in user_ids:
        try:
            await bot.send_message(tg_id, text, reply_markup=kb)
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Broadcast {tg_id}'ga yetmadi: {e}")
        await asyncio.sleep(0.05)  # Telegram rate-limit'iga tegmaslik uchun
    logger.info(f"Broadcast yuborildi: {sent} muvaffaqiyatli, {failed} muvaffaqiyatsiz.")


@dp.callback_query(F.data == "adm_export")
async def adm_export(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await call.answer("Excel tayyorlanmoqda...")
    await export_answers_to_excel(call.message)


@dp.callback_query(F.data == "adm_clear_answers")
async def adm_clear_answers_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await call.message.edit_text(
        "⚠️ <b>Diqqat!</b>\nBarcha yuborilgan javoblar tarixi butunlay o'chiriladi "
        "(ro'yxatdan o'tganlar ro'yxati saqlanib qoladi). Bu amalni ortga qaytarib bo'lmaydi.\n\n"
        "Rostdan ham tozalaysizmi?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Ha, tozalash", callback_data="adm_clear_answers_go")],
            [InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="adm_back")],
        ]),
    )
    await call.answer()


@dp.callback_query(F.data == "adm_clear_answers_go")
async def adm_clear_answers_go(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.clear_answers()
    await call.message.edit_text(
        await admin_panel_text(),
        reply_markup=admin_panel_kb(await db.is_tournament_active(), await db.get_active_round()),
    )
    await call.answer("✅ Javoblar tarixi tozalandi.", show_alert=True)


@dp.callback_query(F.data == "adm_clear_all")
async def adm_clear_all_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await call.message.edit_text(
        "⚠️⚠️ <b>JIDDIY DIQQAT!</b>\n"
        "Bu BARCHA ma'lumotni o'chiradi: ro'yxatdan o'tganlar, barcha javoblar, "
        "turnir holati - hammasi boshlang'ich holatga qaytadi. "
        "Foydalanuvchilar qayta /start bilan ro'yxatdan o'tishlari kerak bo'ladi.\n\n"
        "Bu amalni ORTGA QAYTARIB BO'LMAYDI. Rostdan ham davom etasizmi?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ Ha, HAMMASINI o'chirish", callback_data="adm_clear_all_go")],
            [InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="adm_back")],
        ]),
    )
    await call.answer()


@dp.callback_query(F.data == "adm_clear_all_go")
async def adm_clear_all_go(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.clear_all_data()
    await call.message.edit_text(
        await admin_panel_text(), reply_markup=admin_panel_kb(False, 0)
    )
    await call.answer("✅ Barcha ma'lumotlar tozalandi.", show_alert=True)


async def export_answers_to_excel(message: Message):
    """Barcha javoblarni .xlsx faylga yig'ib, chatga yuboradi."""
    from openpyxl import Workbook

    rows = await db.get_all_answers()

    wb = Workbook()
    ws = wb.active
    ws.title = "Javoblar"
    headers = [
        "ID", "Telegram ID", "Ism-Familiya", "Username", "Raund",
        "Turi", "Matn", "File ID", "WPM", "Vaqt (s)", "Yuborilgan vaqt",
    ]
    ws.append(headers)

    for r in rows:
        round_name = config.ROUNDS.get(r["round_num"], {}).get("name", r["round_num"])
        ws.append([
            r["id"], r["tg_id"], r["full_name"] or "", r["username"] or "",
            round_name, r["content_type"] or "", r["text_content"] or "",
            r["file_id"] or "", r["wpm"], r["time_sec"], r["submitted_at"],
        ])

    # Ustunlar kengligini biroz sozlash
    for col in ws.columns:
        max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    filename = f"zakovat_quiz_natijalar_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    await message.answer_document(
        BufferedInputFile(buffer.read(), filename=filename),
        caption=f"📥 Jami {len(rows)} ta javob.",
    )


# ==================================================================
#  ISHGA TUSHIRISH
# ==================================================================

async def main():
    await db.init_db()
    logger.info("Bot ishga tushdi...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        # Render'ning bepul "Web Service" tarifi HTTP portni tinglashni talab
        # qiladi (aks holda deploy "muvaffaqiyatsiz" deb belgilanadi va
        # servis uxlab qoladi). Shuning uchun polling bilan bir vaqtda
        # juda kichik HTTP server ham ishga tushiriladi - u faqat
        # health-check so'rovlariga "OK" javob beradi, botning ishiga
        # aloqasi yo'q.
        await asyncio.gather(
            dp.start_polling(bot),
            run_health_server(),
        )
    finally:
        await db.close_db()


async def run_health_server():
    """Render health-check va UptimeRobot kabi pingerlar uchun minimal HTTP server."""
    from aiohttp import web

    async def health(request):
        return web.Response(text="OK, bot ishlayapti.")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info(f"Health-check server {port}-portda ishga tushdi.")
    # Serverni doim ochiq ushlab turish
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
