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
    choosing_round = State()
    waiting_answer = State()


# ==================================================================
#  KLAVIATURALAR
# ==================================================================

def main_menu_kb() -> ReplyKeyboardMarkup:
    """Bosh menyu (Reply klaviatura)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📤 Javob yuborish")],
            [KeyboardButton(text="⌨️ Tez yozish (Web App)", web_app=WebAppInfo(url=config.WEBAPP_URL))],
            [KeyboardButton(text="ℹ️ Qoidalar"), KeyboardButton(text="📊 Mening natijalarim")],
        ],
        resize_keyboard=True,
    )


def subscribe_kb() -> InlineKeyboardMarkup:
    """Obuna bo'lish va tekshirish tugmalari."""
    channel_link = f"https://t.me/{config.CHANNEL_USERNAME.lstrip('@')}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Kanalga obuna bo'lish", url=channel_link)],
            [InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_sub")],
        ]
    )


async def rounds_kb() -> InlineKeyboardMarkup:
    """Faqat yoqilgan raundlarni ko'rsatuvchi inline klaviatura."""
    rows = []
    for num, info in config.ROUNDS.items():
        if await db.is_round_enabled(num):
            rows.append([InlineKeyboardButton(text=info["name"], callback_data=f"round_{num}")])
    rows.append([InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="cancel_answer")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Turnirni boshlash", callback_data="adm_start"),
                InlineKeyboardButton(text="⏹ Turnirni to'xtatish", callback_data="adm_stop"),
            ],
            [InlineKeyboardButton(text="🎛 Raundlarni boshqarish", callback_data="adm_rounds")],
            [InlineKeyboardButton(text="📥 Excel yuklab olish", callback_data="adm_export")],
        ]
    )


async def admin_rounds_kb() -> InlineKeyboardMarkup:
    rows = []
    for num, info in config.ROUNDS.items():
        enabled = await db.is_round_enabled(num)
        status = "🟢" if enabled else "🔴"
        rows.append(
            [InlineKeyboardButton(text=f"{status} {info['name']}", callback_data=f"adm_toggle_{num}")]
        )
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
            reply_markup=main_menu_kb(),
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
            await call.message.answer("✅ Obuna tasdiqlandi! Bosh menyu:", reply_markup=main_menu_kb())
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
        reply_markup=main_menu_kb(),
    )


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
    if not await db.is_tournament_active():
        await message.answer("⏸ Hozircha turnir faol emas. Admin turnirni boshlashini kuting.")
        return
    if not await db.is_registered(message.from_user.id):
        await message.answer("Avval /start orqali ro'yxatdan o'ting.")
        return

    kb = await rounds_kb()
    if not kb.inline_keyboard[:-1]:
        await message.answer("Hozircha faol raund yo'q. Keyinroq urinib ko'ring.")
        return

    await message.answer("Qaysi raund uchun javob yubormoqchisiz?", reply_markup=kb)
    await state.set_state(AnswerStates.choosing_round)


@dp.callback_query(StateFilter(AnswerStates.choosing_round), F.data == "cancel_answer")
async def cancel_answer(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.answer("Bekor qilindi")


@dp.callback_query(StateFilter(AnswerStates.choosing_round), F.data.startswith("round_"))
async def choose_round(call: CallbackQuery, state: FSMContext):
    round_num = int(call.data.split("_")[1])

    if not await db.is_round_enabled(round_num):
        await call.answer("Bu raund hozir yopiq!", show_alert=True)
        return

    if round_num == 4:
        await call.message.edit_text(
            "⌨️ 4-raund uchun javob Web App orqali avtomatik yuboriladi.\n"
            "Bosh menyudagi \"Tez yozish\" tugmasini bosing."
        )
        await state.clear()
        return

    info = config.ROUNDS[round_num]
    await state.update_data(round_num=round_num)
    await state.set_state(AnswerStates.waiting_answer)
    await call.message.edit_text(
        f"✏️ <b>{info['name']}</b>\n\n"
        f"{info['hint']}\n\n"
        "Javobingizni pastga yuboring 👇"
    )
    await call.answer()


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

    await db.add_answer(
        tg_id=message.from_user.id,
        round_num=round_num,
        content_type=content_type,
        text_content=text_content,
        file_id=file_id,
    )

    await state.clear()
    await message.answer(
        "✅ Javobingiz qabul qilindi va hakamlarga yuborildi!",
        reply_markup=main_menu_kb(),
    )

    await forward_answer_to_admins(
        message=message, round_num=round_num, content_type=content_type,
        text_content=text_content, file_id=file_id,
    )


async def forward_answer_to_admins(
    message: Message, round_num: int, content_type: str,
    text_content: str | None, file_id: str | None,
):
    """Kelgan javobni ADMIN_GROUP_ID'ga va (agar sozlangan bo'lsa) PERSONAL_CHAT_ID'ga yuboradi."""
    user = message.from_user
    safe_full_name = html.escape(user.full_name or "")
    safe_username = html.escape(user.username) if user.username else "yoq"
    safe_text = html.escape(text_content) if text_content else None

    caption = (
        f"#Raund_{round_num}\n"
        f"👤 Qatnashchi: {safe_full_name} (@{safe_username}, ID: {user.id})\n"
    )

    targets = [config.ADMIN_GROUP_ID]
    if config.PERSONAL_CHAT_ID:
        targets.append(config.PERSONAL_CHAT_ID)

    for chat_id in targets:
        try:
            if content_type in ("photo", "photo_text"):
                await bot.send_photo(
                    chat_id, photo=file_id,
                    caption=caption + f"📝 Javob: {safe_text or '(faqat rasm)'}",
                )
            elif content_type == "sticker":
                await bot.send_message(chat_id, caption + "📝 Javob: (stiker quyida)")
                await bot.send_sticker(chat_id, sticker=file_id)
            elif content_type == "voice":
                await bot.send_voice(
                    chat_id, voice=file_id, caption=caption + "📝 Javob: (ovozli xabar quyida)"
                )
            elif content_type == "audio":
                await bot.send_audio(
                    chat_id, audio=file_id, caption=caption + "📝 Javob: (audio quyida)"
                )
            else:  # text
                await bot.send_message(chat_id, caption + f"📝 Javob: {safe_text}")
        except Exception as e:
            # Bitta manzilga yuborish muvaffaqiyatsiz bo'lsa ham,
            # qolgan manzillarga yuborishda davom etamiz.
            logger.error(f"Javobni {chat_id}'ga yuborishda xatolik: {e}")


# ==================================================================
#  WEB APP DAN KELGAN MA'LUMOT (Tez yozish raundi - 4-raund)
# ==================================================================

@dp.message(F.content_type == ContentType.WEB_APP_DATA)
async def handle_webapp_data(message: Message):
    if not await db.is_round_enabled(4):
        await message.answer("Bu raund hozir yopiq, natija qabul qilinmadi.")
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
        reply_markup=main_menu_kb(),
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


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Bu buyruq faqat adminlar uchun.")
        return
    status = "🟢 Faol" if await db.is_tournament_active() else "🔴 Faol emas"
    await message.answer(
        f"🛠 <b>Admin panel</b>\nTurnir holati: {status}",
        reply_markup=admin_panel_kb(),
    )


@dp.callback_query(F.data == "adm_start")
async def adm_start(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.set_tournament_active(True)
    await call.message.edit_text(
        "🛠 <b>Admin panel</b>\nTurnir holati: 🟢 Faol", reply_markup=admin_panel_kb()
    )
    await call.answer("Turnir boshlandi!")


@dp.callback_query(F.data == "adm_stop")
async def adm_stop(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.set_tournament_active(False)
    await call.message.edit_text(
        "🛠 <b>Admin panel</b>\nTurnir holati: 🔴 Faol emas", reply_markup=admin_panel_kb()
    )
    await call.answer("Turnir to'xtatildi!")


@dp.callback_query(F.data == "adm_back")
async def adm_back(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    status = "🟢 Faol" if await db.is_tournament_active() else "🔴 Faol emas"
    await call.message.edit_text(
        f"🛠 <b>Admin panel</b>\nTurnir holati: {status}", reply_markup=admin_panel_kb()
    )
    await call.answer()


@dp.callback_query(F.data == "adm_rounds")
async def adm_rounds(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await call.message.edit_text(
        "🎛 <b>Raundlarni boshqarish</b>\n🟢 = yoqilgan, 🔴 = o'chirilgan\nBosib holatini o'zgartiring:",
        reply_markup=await admin_rounds_kb(),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm_toggle_"))
async def adm_toggle_round(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    round_num = int(call.data.split("_")[-1])
    new_state = await db.toggle_round(round_num)
    await call.message.edit_reply_markup(reply_markup=await admin_rounds_kb())
    status_text = "yoqildi ✅" if new_state else "o'chirildi ⛔️"
    await call.answer(f"{config.ROUNDS[round_num]['name']} {status_text}")


@dp.callback_query(F.data == "adm_export")
async def adm_export(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await call.answer("Excel tayyorlanmoqda...")
    await export_answers_to_excel(call.message)


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
