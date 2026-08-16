# bot.py
# ------------------------------------------------------------
# "Zakovat Quiz" turnirlari uchun asosiy Telegram bot kodi (Aiogram 3.x)
#
# Bitta bot ichida bir nechta TURNIR bo'lishi mumkin (config.TOURNAMENTS):
#   - Turnir 100 - avvalgi musobaqa, shartlari o'zgarishsiz
#   - Turnir 300 - yangi, kengaytirilgan musobaqa ("Rasmga oidlik" AI orqali
#     baholanadigan raund bilan)
#
# Ishga tushirish:
#   pip install -r requirements.txt
#   python bot.py
# ------------------------------------------------------------
import asyncio
import base64
import html
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict

import aiohttp
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
    BufferedInputFile,
    FSInputFile,
    TelegramObject,
)

import config
import database as db
import ai_providers


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# main() ichida bot.get_me() orqali to'ldiriladi - guruhda "@bot_username"
# ko'rinishidagi mention'larni aniqlash uchun kerak.
BOT_USERNAME: str | None = None

# Guruhda AI javob berish uchun flood/AI-kvota himoyasi: bir chatga
# ketma-ket juda tez-tez javob yozib, kalitlarni "kuydirib" yubormaslik.
_GROUP_REPLY_COOLDOWN_SEC = 15
_group_reply_last: dict[int, float] = {}


def get_rounds(tournament_id: int) -> dict:
    """Berilgan turnirning raundlar lug'atini qaytaradi (topilmasa - bo'sh)."""
    return config.TOURNAMENTS.get(tournament_id, {}).get("rounds", {})


# ==================================================================
#  FSM HOLATLARI
# ==================================================================

class RegisterStates(StatesGroup):
    waiting_fullname = State()


class AdminStickerAnnounceStates(StatesGroup):
    waiting_sticker = State()
    waiting_caption = State()


class AdminAssocStates(StatesGroup):
    """'Rasmga oidlik' raundi uchun: admin bir nechta rasm yuboradi,
    so'ng ✅ Tayyor bosadi - shundan keyin rasmlar RAQAMLAB kanalga ketadi."""
    collecting_images = State()


class AdminMessageUserStates(StatesGroup):
    waiting_message = State()


class AdminAIKeyStates(StatesGroup):
    """AI provayder kaliti qo'shish: avval provayder tanlanadi (tugma orqali),
    so'ng shu holatda kalit matn sifatida kutiladi."""
    waiting_key = State()


class AdminSchedPostStates(StatesGroup):
    """Rejalashtirilgan (avtomatik) post yaratish: vaqt -> kunlar (tugma) ->
    kontent turi (tugma) -> [agar matn bo'lmasa: media faylni yuborish] ->
    nishon (tugma) -> AI uchun promt/tarif ko'rsatmasi (matn) ->
    [ixtiyoriy: shu post uchungina qo'shimcha qoida]."""
    waiting_time = State()
    waiting_media = State()
    waiting_prompt = State()
    waiting_extra_rule = State()


class AdminGeneralRuleStates(StatesGroup):
    waiting_rule = State()


class AdminGroupTopicStates(StatesGroup):
    """Guruhda AI o'zi mavzu ochadigan qoida: intervalda (soat) -> promt matni."""
    waiting_interval = State()
    waiting_prompt = State()


class AdminGroupChatIdStates(StatesGroup):
    waiting_chat_id = State()


class AdminPersonaStates(StatesGroup):
    waiting_persona = State()


# ==================================================================
#  KLAVIATURALAR
# ==================================================================

async def main_menu_kb() -> ReplyKeyboardMarkup:
    """Bosh menyu (Reply klaviatura) - turnir holatiga qarab dinamik shakllanadi.

    - Turnir hali boshlanmagan bo'lsa: faqat "Qoidalar" va "Natijalarim" ko'rinadi.
    - Turnir boshlangan bo'lsa: "Javob yuborish" qo'shiladi.
    """
    tournament_active = await db.is_tournament_active()
    active_round = await db.get_active_round()

    rows = []
    if tournament_active and active_round:
        rows.append([KeyboardButton(text="📤 Javob yuborish")])
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


async def admin_panel_kb(tournament_active: bool, active_tournament: int, active_round: int) -> InlineKeyboardMarkup:
    rows = []
    if tournament_active:
        rows.append([InlineKeyboardButton(text="⏹ Turnirni to'xtatish", callback_data="adm_stop")])
    else:
        rows.append([InlineKeyboardButton(text="▶️ Turnirni boshlash", callback_data="adm_start")])

    rounds = get_rounds(active_tournament)
    round_label = rounds.get(active_round, {}).get("name") if active_round else None
    rows.append([InlineKeyboardButton(
        text="🔀 Raund boshlash" + (f" (hozir: {round_label})" if round_label else ""),
        callback_data="adm_pick_round",
    )])
    rows.append([InlineKeyboardButton(text="📥 Excel yuklab olish", callback_data="adm_export")])
    rows.append([InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="adm_users_0")])
    rows.append([InlineKeyboardButton(text="📊 Statistika", callback_data="adm_stats")])
    rows.append([InlineKeyboardButton(text="🤖 AI provayderlar", callback_data="adm_ai_menu")])
    rows.append([InlineKeyboardButton(text="💾 Zaxira olish (qo'lda)", callback_data="adm_backup_now")])

    # TEST REJIMI: yoniq bo'lsa kanalga/foydalanuvchilarga HECH NARSA
    # ketmaydi - faqat admin(lar)ga simulyatsiya ko'rinishida yuboriladi.
    test_mode = await db.is_test_mode()
    test_label = "🧪 Test rejimi: YONIQ (o'chirish)" if test_mode else "🧪 Test rejimi: O'CHIQ (yoqish)"
    rows.append([InlineKeyboardButton(text=test_label, callback_data="adm_toggle_test")])

    rows.append([InlineKeyboardButton(text="🗑 Javoblar tarixini tozalash", callback_data="adm_clear_answers")])
    rows.append([InlineKeyboardButton(text="⚠️ To'liq tozalash (userlar ham)", callback_data="adm_clear_all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_tournament_picker_kb() -> InlineKeyboardMarkup:
    """Admin qaysi TURNIRni boshlashni tanlashi uchun (Turnir 100 / Turnir 300)."""
    rows = [
        [InlineKeyboardButton(text=info["name"], callback_data=f"adm_choose_t_{tid}")]
        for tid, info in config.TOURNAMENTS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_round_picker_kb(tournament_id: int) -> InlineKeyboardMarkup:
    """Admin tanlangan turnirda qaysi raundni faol qilishni tanlashi uchun."""
    rounds = get_rounds(tournament_id)
    rows = [
        [InlineKeyboardButton(text=info["name"], callback_data=f"adm_setround_{num}")]
        for num, info in sorted(rounds.items())
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==================================================================
#  KANALGA E'LON YUBORISH (bot @ParadoksHub kanalida admin huquqiga ega)
# ==================================================================

async def _test_mode_targets() -> list[int]:
    targets = [config.ADMIN_GROUP_ID]
    if config.PERSONAL_CHAT_ID:
        targets.append(config.PERSONAL_CHAT_ID)
    return targets


async def send_test_mode_notice(text: str) -> None:
    """TEST REJIMI yoniq bo'lganda, kanalga/userlarga ketishi kerak bo'lgan
    xabar o'rniga shu funksiya orqali FAQAT admin(lar)ga [TEST] belgisi bilan
    yuboriladi - hech kim boshqa buni ko'rmaydi."""
    for chat_id in await _test_mode_targets():
        try:
            await bot.send_message(chat_id, text)
        except Exception as e:
            logger.error(f"Test rejimi xabarini {chat_id}'ga yuborishda xatolik: {e}")


async def post_text_to_channel(text: str) -> None:
    """Kanalga oddiy matnli e'lon yuboradi. TEST REJIMIDA kanalga ketmaydi -
    o'rniga admin(lar)ga [TEST] belgisi bilan simulyatsiya yuboriladi."""
    if await db.is_test_mode():
        await send_test_mode_notice(f"🧪 <b>[TEST] Kanalga ketishi kerak edi:</b>\n\n{text}")
        return
    try:
        await bot.send_message(config.CHANNEL_ID, text)
        asyncio.create_task(_maybe_auto_comment(text))
    except Exception as e:
        logger.error(f"Kanalga xabar yuborishda xatolik: {e}")


async def post_sticker_to_channel(sticker_file_id: str, caption: str | None = None) -> None:
    """Kanalga stiker, so'ng (agar berilsa) tarif matnini alohida xabar sifatida yuboradi
    (Telegram stikerlarda caption maydoni yo'q, shuning uchun ikkita xabar bo'ladi).
    TEST REJIMIDA kanalga ketmaydi - o'rniga admin(lar)ga simulyatsiya yuboriladi."""
    if await db.is_test_mode():
        await send_test_mode_notice(
            f"🧪 <b>[TEST] Stiker kanalga ketishi kerak edi</b> (file_id: <code>{sticker_file_id}</code>)"
            + (f"\nTarif: {caption}" if caption else "")
        )
        return
    try:
        await bot.send_sticker(config.CHANNEL_ID, sticker=sticker_file_id)
        if caption:
            await bot.send_message(config.CHANNEL_ID, caption)
            asyncio.create_task(_maybe_auto_comment(caption))
    except Exception as e:
        logger.error(f"Kanalga stiker yuborishda xatolik: {e}")


async def _build_ai_system(extra_rule: str | None = None) -> str:
    """AI'ning barcha 'ijodiy' (post/izoh/guruh javobi) generatsiyalari uchun
    system promptni yig'adi: xarakter (persona) + umumiy qoida (agar
    sozlangan bo'lsa, masalan 'qisqaroq yoz, oxirida @ParadoksHub qo'sh') +
    shu aniq post uchungina beriladigan qo'shimcha qoida (ixtiyoriy).
    'Rasmga oidlik' AI-hakami (call_groq_vision) BUNDAN foydalanmaydi - u
    qat'iy JSON formatli maxsus vazifa, persona bilan aralashmasligi kerak."""
    parts = [await db.get_ai_persona()]
    general_rule = await db.get_ai_general_rule()
    if general_rule:
        parts.append(f"Qo'shimcha umumiy qoida: {general_rule}")
    if extra_rule:
        parts.append(f"Shu post uchun maxsus qoida: {extra_rule}")
    return "\n\n".join(p for p in parts if p)


async def _maybe_auto_comment(context_text: str) -> None:
    """Kanalga yangi e'lon ketgach, agar 'Auto-izoh' yoniq bo'lsa, AI shu
    e'lon haqida qisqa izoh yozib, sozlangan guruhga yuboradi. asyncio.create_task
    orqali fon vazifasi sifatida chaqiriladi - kanal postini kechiktirmaslik uchun."""
    try:
        if not await db.is_ai_auto_comment_enabled() or await db.is_test_mode():
            return
        group_chat_id = await db.get_ai_group_chat_id()
        if not group_chat_id:
            return
        persona = await _build_ai_system()
        prompt = (
            f"Hozirgina kanalga quyidagi e'lon qilindi:\n\n{context_text}\n\n"
            "Shu haqida muhokama guruhidagi a'zolarni qiziqtiradigan, qisqa "
            "(1-2 gap) tabiiy izoh/sharh yoz. Faqat izoh matnini yoz, boshqa hech narsa qo'shma."
        )
        comment = await ai_providers.generate(prompt=prompt, system=persona)
        if comment:
            await bot.send_message(group_chat_id, comment)
            await db.log_ai_event("auto_comment_sent")
    except Exception as e:
        logger.error(f"Auto-izoh yuborishda xatolik: {e}")


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

        # Majburiy obuna tekshiruvi FAQAT shaxsiy chatga tegishli (botning
        # o'zi bilan bevosita muloqot). Guruh/kanal xabarlarida bu tekshiruv
        # o'chirilgan - aks holda AI guruh faolligi yoqilganda, obuna
        # bo'lmagan har bir a'zoning oddiy xabariga botning "obuna bo'ling"
        # ogohlantirishi GURUHGA E'LON qilinib, spam bo'lib qolar edi.
        if isinstance(event, Message) and event.chat.type != "private":
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
    asyncio.create_task(backup_db_to_telegram())


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
    active_tournament = await db.get_active_tournament()
    rounds = get_rounds(active_tournament)
    if not rounds:
        await message.answer(
            "📜 <b>Zakovat Quiz</b>\n\nHozircha faol turnir yo'q. Admin turnir boshlashini kuting."
        )
        return

    t_name = config.TOURNAMENTS[active_tournament]["name"]
    rounds_text = "\n".join(f"{num}. {info['name']}" for num, info in sorted(rounds.items()))
    external_names = ", ".join(info["name"] for info in rounds.values() if info.get("kind") == "external")

    text = (
        f"📜 <b>{t_name} - Qoidalar</b>\n\n"
        f"Turnir {len(rounds)} ta raunddan iborat:\n{rounds_text}\n\n"
        "Har bir raund uchun topshiriq kanal/guruhga e'lon qilinadi. "
        "Javobingizni esa aynan shu botga \"📤 Javob yuborish\" tugmasi orqali yuboring.\n"
    )
    if external_names:
        text += f"\n⚠️ <b>{external_names}</b> - bular botda emas, kanal/guruh muhokamasida o'tkaziladi."
    await message.answer(text)


@dp.message(F.text == "📊 Mening natijalarim")
async def my_results(message: Message):
    answers = await db.get_user_answers(message.from_user.id)
    if not answers:
        await message.answer("Siz hali hech qanday javob yubormagansiz.")
        return

    lines = ["📊 <b>Sizning yuborgan javoblaringiz:</b>\n"]
    for a in answers:
        tournament_id = a["tournament_id"] or 100
        rounds = get_rounds(tournament_id)
        round_name = rounds.get(a["round_num"], {}).get("name", f"{a['round_num']}-raund")
        extra = ""
        if a["wpm"] is not None:
            extra = f" | WPM: {a['wpm']:.1f}, vaqt: {a['time_sec']:.1f}s"
        if a["ref_num"] is not None:
            extra += f" | rasm #{a['ref_num']}"
        lines.append(f"• {round_name} — {a['content_type']}{extra} ({a['submitted_at']})")
    await message.answer("\n".join(lines))


# ==================================================================
#  JAVOB YUBORISH OQIMI
# ==================================================================

RESERVED_MENU_TEXTS = {"📤 Javob yuborish", "ℹ️ Qoidalar", "📊 Mening natijalarim"}
ASSOC_ANSWER_RE = re.compile(r"^\s*(\d+)\s*[\).:\-]?\s*(.+)$", re.S)

# XAVFSIZLIK: "Rasmga oidlik" raundida cheklovsiz qayta yuborish har safar
# Groq API'ga chaqiruv qiladi (pullik/kvota bilan cheklangan resurs). Agar
# himoyasiz qoldirilsa, bitta foydalanuvchi soniyalab xabar yuborib
# GROQ_API_KEYS limitini bir necha daqiqada tugatib qo'yishi mumkin - shundan
# keyin butun turnir uchun AI baholash ishlamay qoladi. Shu sabab har
# foydalanuvchiga oddiy cooldown qo'yiladi (xotirada, restart'da tozalanadi -
# bu yetarli, chunki maqsad faqat flood/abuse'ning oldini olish).
_ASSOC_COOLDOWN_SEC = 10
_assoc_last_submit: dict[int, float] = {}


@dp.message(F.text == "📤 Javob yuborish")
async def show_answer_prompt(message: Message):
    """Eslatma/ma'lumot beradi - FSM holatiga o'tkazmaydi. Javobni istalgan
    payt to'g'ridan-to'g'ri (matn/rasm/stiker/ovoz) yuborish mumkin - bu
    pastdagi 'receive_answer' orqali FSM holatisiz qabul qilinadi, shuning
    uchun bot qayta ishga tushib qolsa ham (Render deploy va h.k.) hech
    qanday javob yo'qolib qolmaydi."""
    if not await db.is_registered(message.from_user.id):
        await message.answer("Avval /start orqali ro'yxatdan o'ting.")
        return
    if not await db.is_tournament_active():
        await message.answer("⏸ Hozircha turnir faol emas. Admin turnirni boshlashini kuting.")
        return

    tournament_id = await db.get_active_tournament()
    rounds = get_rounds(tournament_id)
    round_num = await db.get_active_round()
    if not round_num or round_num not in rounds:
        await message.answer("Hozircha faol raund yo'q. Admin raund boshlashini kuting.")
        return

    info = rounds[round_num]

    if info["kind"] == "external":
        await message.answer(f"ℹ️ <b>{info['name']}</b>\n\n{info['hint']}")
        return

    if info["kind"] == "assoc":
        await message.answer(f"🖼 <b>{info['name']}</b>\n\n{info['hint']}")
        return

    if await db.has_answered(message.from_user.id, tournament_id, round_num):
        await message.answer("⚠️ Siz bu raundda allaqachon ishtirok etgansiz, qayta yuborib bo'lmaydi.")
        return

    await message.answer(
        f"✏️ <b>{info['name']}</b>\n\n"
        f"{info['hint']}\n\n"
        "Javobingizni pastga to'g'ridan-to'g'ri yuboring 👇"
    )


async def notify_admins_new_answer(answer_id: int, user, round_num: int, content_type: str, tournament_id: int):
    """Javob kelganini adminlarga QISQA xabar bilan bildiradi - to'liq kontent
    guruhni to'ldirib yubormasligi uchun, faqat "Ko'rish" bosilganda ochiladi."""
    safe_full_name = html.escape(user.full_name or "")
    safe_username = html.escape(user.username) if user.username else "yoq"
    round_name = get_rounds(tournament_id).get(round_num, {}).get("name", f"{round_num}-raund")

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
#  "RASMGA OIDLIK" RAUNDI - AI (Groq) YORDAMIDA AVTOMATIK BAHOLASH
# ==================================================================

async def call_groq_vision(image_url: str, words: list[str]) -> dict | None:
    """Rasm + so'zlar ro'yxatini AI'ga yuborib, qaysi so'zlar rasmga chindan
    ham aloqador (to'g'ri) va qaysilari emas (noto'g'ri) ekanini so'raydi.
    Ism eski qolgan (call_groq_vision) - lekin endi ai_providers.generate()
    orqali DB'da sozlangan BARCHA provayder/kalitlarni sinab ko'radi (faqat
    Groq emas), biri ishlamasa avtomatik navbatdagisiga o'tadi."""
    system_prompt = (
        "Sen 'Zakovat' viktorinasining 'Rasmga oidlik' raundida hakamsan. "
        "Senga bitta rasm va ishtirokchi yozgan so'z/ibora ro'yxati beriladi. "
        "Vazifang: har bir so'z/iborani rasmga chindan ham aloqador "
        "(bog'liq shaxs, kompaniya, brend, mahsulot, xizmat, joy, voqea yoki "
        "tushuncha) yoki aloqasiz ekanini aniqlash. Faqat quyidagi JSON "
        "formatda javob ber, boshqa hech qanday matn yozma:\n"
        '{"correct": ["so\'z1", "so\'z2"], "incorrect": ["so\'z3"]}'
    )
    user_text = "Ishtirokchi yuborgan so'zlar (vergul bilan ajratilgan):\n" + ", ".join(words)

    content = await ai_providers.generate(
        prompt=user_text, system=system_prompt, image_data_url=image_url, json_mode=True,
    )
    if content is None:
        return None
    try:
        parsed = json.loads(content)
        correct = [str(x) for x in parsed.get("correct", [])]
        incorrect = [str(x) for x in parsed.get("incorrect", [])]
        return {"correct": correct, "incorrect": incorrect}
    except Exception as e:
        logger.error(f"AI javobini JSON sifatida o'qib bo'lmadi: {e} | javob: {content[:300]}")
        return None


async def score_assoc_answer(answer_id: int, image_file_id: str, words: list[str], user, image_number: int) -> None:
    """AI orqali baholaydi va natijani (FAQAT adminlarga - foydalanuvchiga
    HECH QACHON emas) yuboradi.

    XAVFSIZLIK ESLATMASI: rasmni Groq'ga hech qachon
    "https://api.telegram.org/file/bot<TOKEN>/..." ko'rinishidagi to'g'ridan-to'g'ri
    URL orqali YUBORMAYMIZ - bunday URL ichida bot tokeni ochiq matnda uchinchi
    tomon (Groq) serveriga ketadi va u yerda log/keshda saqlanib qolishi mumkin.
    Shu sabab rasmni o'zimiz yuklab olib, base64 data-URL sifatida yuboramiz -
    token hech qachon botimizdan tashqariga chiqmaydi."""
    try:
        file_info = await bot.get_file(image_file_id)
        file_bytes_io = await bot.download_file(file_info.file_path)
        image_bytes = file_bytes_io.read()
    except Exception as e:
        logger.error(f"Rasm faylini olishda xatolik: {e}")
        return

    ext = file_info.file_path.rsplit(".", 1)[-1].lower() if "." in file_info.file_path else "jpg"
    mime = "image/png" if ext == "png" else "image/jpeg"
    image_data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"

    result = await call_groq_vision(image_data_url, words)
    if result is None:
        return

    correct, incorrect = result["correct"], result["incorrect"]
    score = len(correct) - len(incorrect)
    await db.update_answer_ai_result(
        answer_id, score, json.dumps(correct, ensure_ascii=False), json.dumps(incorrect, ensure_ascii=False)
    )

    safe_name = html.escape(user.full_name or "")
    safe_username = html.escape(user.username) if user.username else "yoq"
    text = (
        f"🤖 <b>AI baholadi — Rasmga oidlik</b>\n"
        f"🖼 Rasm: #{image_number}\n"
        f"👤 {safe_name} (@{safe_username}, ID: {user.id})\n"
        f"✅ To'g'ri ({len(correct)}): {html.escape(', '.join(correct)) or '—'}\n"
        f"❌ Noto'g'ri ({len(incorrect)}): {html.escape(', '.join(incorrect)) or '—'}\n"
        f"🧮 Ball: <b>{score:+d}</b>"
    )
    targets = [config.ADMIN_GROUP_ID]
    if config.PERSONAL_CHAT_ID:
        targets.append(config.PERSONAL_CHAT_ID)
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, text)
        except Exception as e:
            logger.error(f"AI natijasini {chat_id}'ga yuborishda xatolik: {e}")


async def handle_assoc_answer(message: Message, tournament_id: int, round_num: int) -> None:
    """'Rasmga oidlik' raundi uchun javobni qabul qiladi. Format:
    '<rasm raqami>: so'z1, so'z2, ...'. Foydalanuvchi keyingi round
    boshlanmaguncha istagancha marta javob yubora oladi (has_answered
    tekshiruvi YO'Q - bu ataylab shunday)."""
    if not message.text:
        await message.answer(
            "🖼 Bu raundda javobni FAQAT matn ko'rinishida yuboring:\n"
            "\"&lt;rasm raqami&gt;: so'z1, so'z2, ...\"\n"
            "Masalan: <code>3: Instagram, Meta, Zuckerberg</code>"
        )
        return

    # XAVFSIZLIK: Groq kvotasini flood orqali "kuydirishning" oldini olish
    now = time.monotonic()
    last = _assoc_last_submit.get(message.from_user.id, 0.0)
    if now - last < _ASSOC_COOLDOWN_SEC:
        wait = int(_ASSOC_COOLDOWN_SEC - (now - last)) + 1
        await message.answer(f"⏳ Juda tez-tez yuboryapsiz, {wait} soniyadan keyin qayta urinib ko'ring.")
        return
    _assoc_last_submit[message.from_user.id] = now

    m = ASSOC_ANSWER_RE.match(message.text)
    if not m:
        await message.answer(
            "❗️ Format noto'g'ri. Avval rasm raqamini, keyin so'zlarni yuboring.\n"
            "Masalan: <code>3: Instagram, Meta, Zuckerberg</code>"
        )
        return

    image_number = int(m.group(1))
    words_part = m.group(2).strip()
    words = [w.strip() for w in re.split(r"[,\n]+", words_part) if w.strip()]
    if not words:
        await message.answer("❗️ Iltimos, rasm raqamidan keyin kamida bitta so'z/ibora yozing.")
        return
    if len(words) > 30:
        await message.answer("❗️ Bitta xabarda ko'pi bilan 30 ta so'z/ibora yuborish mumkin.")
        return

    image = await db.get_assoc_image(tournament_id, round_num, image_number)
    if image is None:
        await message.answer(
            f"❗️ #{image_number} raqamli rasm topilmadi. Kanaldagi rasm raqamlaridan birini yozing."
        )
        return

    answer_id = await db.add_answer(
        tg_id=message.from_user.id,
        round_num=round_num,
        tournament_id=tournament_id,
        content_type="assoc",
        text_content=words_part,
        ref_num=image_number,
        chat_id=message.chat.id,
        message_id=message.message_id,
    )

    await message.answer("✅ Javobingiz qabul qilindi va hakamlarga yuborildi!")

    asyncio.create_task(
        score_assoc_answer(
            answer_id=answer_id,
            image_file_id=image["file_id"],
            words=words,
            user=message.from_user,
            image_number=image_number,
        )
    )


# ==================================================================
#  ADMIN PANEL
# ==================================================================

def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


async def admin_panel_text() -> str:
    tournament_active = await db.is_tournament_active()
    active_tournament = await db.get_active_tournament()
    active_round = await db.get_active_round()
    test_mode = await db.is_test_mode()
    status = "🟢 Faol" if tournament_active else "🔴 Faol emas"
    t_name = config.TOURNAMENTS.get(active_tournament, {}).get("name", "tanlanmagan") if active_tournament else "tanlanmagan"
    round_text = get_rounds(active_tournament).get(active_round, {}).get("name", "yo'q") if active_round else "yo'q"
    test_status = "🧪 YONIQ - kanalga/userlarga hech narsa ketmaydi!" if test_mode else "O'chiq"
    return (
        f"🛠 <b>Admin panel</b>\n"
        f"Turnir holati: {status}\n"
        f"Tanlangan turnir: {t_name}\n"
        f"Hozirgi faol raund: {round_text}\n"
        f"Test rejimi: {test_status}"
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Bu buyruq faqat adminlar uchun.")
        return
    tournament_active = await db.is_tournament_active()
    active_tournament = await db.get_active_tournament()
    active_round = await db.get_active_round()
    await message.answer(
        await admin_panel_text(),
        reply_markup=await admin_panel_kb(tournament_active, active_tournament, active_round),
    )


@dp.callback_query(F.data == "adm_start")
async def adm_start(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await call.message.edit_text(
        "🏆 <b>Qaysi turnirni boshlamoqchisiz?</b>",
        reply_markup=admin_tournament_picker_kb(),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm_choose_t_"))
async def adm_choose_tournament(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    tournament_id = int(call.data.split("_")[-1])
    if tournament_id not in config.TOURNAMENTS:
        return await call.answer("Noma'lum turnir.", show_alert=True)

    await db.set_active_tournament(tournament_id)
    await db.set_tournament_active(True)
    t_name = config.TOURNAMENTS[tournament_id]["name"]
    await call.answer(f"✅ {t_name} boshlandi!")
    await post_text_to_channel(f"🎉 <b>{t_name} boshlandi!</b>\nRaundlar birma-bir e'lon qilinadi, tayyor turing!")
    asyncio.create_task(backup_db_to_telegram())

    # MUHIM: turnir "boshlangani" hali hech qanday raund faol degani emas!
    # Shuning uchun darhol raund tanlash menyusini ochamiz - admin buni
    # o'tkazib yubormasligi uchun (aks holda "📤 Javob yuborish" tugmasi
    # hech qachon chiqmaydi va foydalanuvchilar javob yubora olmaydi).
    await call.message.edit_text(
        f"✅ {t_name} boshlandi!\n\n"
        "⚠️ <b>Diqqat: hozircha hech qanday raund faol emas.</b>\n"
        "Foydalanuvchilarda \"📤 Javob yuborish\" tugmasi chiqishi uchun "
        "pastdan BIRINCHI raundni tanlang 👇",
        reply_markup=admin_round_picker_kb(tournament_id),
    )


@dp.callback_query(F.data == "adm_stop")
async def adm_stop(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.set_tournament_active(False)
    await call.message.edit_text(
        await admin_panel_text(), reply_markup=await admin_panel_kb(False, 0, 0)
    )
    await call.answer("Turnir to'xtatildi!")
    end_text = "🏁 Turnir yakunlandi! Rahmat, barchangizga!\n🏆 G'oliblar ERTAGA e'lon qilinadi!"
    await broadcast_to_users(end_text)
    await post_text_to_channel(end_text)
    asyncio.create_task(backup_db_to_telegram())


@dp.callback_query(F.data == "adm_back")
async def adm_back(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    tournament_active = await db.is_tournament_active()
    active_tournament = await db.get_active_tournament()
    active_round = await db.get_active_round()
    await call.message.edit_text(
        await admin_panel_text(), reply_markup=await admin_panel_kb(tournament_active, active_tournament, active_round)
    )
    await call.answer()


@dp.callback_query(F.data == "adm_toggle_test")
async def adm_toggle_test(call: CallbackQuery):
    """TEST REJIMINI yoqadi/o'chiradi. Yoniq bo'lganda: kanalga hech qanday
    post ketmaydi, ro'yxatdan o'tgan foydalanuvchilarga HECH QANDAY broadcast
    ketmaydi - buning o'rniga hammasi faqat admin(lar)ga [TEST] belgisi bilan
    simulyatsiya ko'rinishida yuboriladi. Boshqa hamma funksiya (raund
    tanlash, javob qabul qilish, admin panel va h.k.) xuddi avvalgidek ishlayveradi."""
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    new_state = not await db.is_test_mode()
    await db.set_test_mode(new_state)
    tournament_active = await db.is_tournament_active()
    active_tournament = await db.get_active_tournament()
    active_round = await db.get_active_round()
    await call.message.edit_text(
        await admin_panel_text(),
        reply_markup=await admin_panel_kb(tournament_active, active_tournament, active_round),
    )
    await call.answer("🧪 Test rejimi YONDI!" if new_state else "✅ Test rejimi o'chdi, hammasi haqiqiy ketadi.", show_alert=True)


@dp.callback_query(F.data == "adm_pick_round")
async def adm_pick_round(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    if not await db.is_tournament_active():
        return await call.answer("Avval turnirni boshlang!", show_alert=True)
    active_tournament = await db.get_active_tournament()
    await call.message.edit_text(
        "🔀 <b>Qaysi raundni faol qilmoqchisiz?</b>\n\n"
        "⚠️ Yangi raund tanlanishi bilan hozirgi faol raund AVTOMATIK yopiladi "
        "va foydalanuvchilarga hamda kanalga xabar yuboriladi.",
        reply_markup=admin_round_picker_kb(active_tournament),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm_setround_"))
async def adm_set_round(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    round_num = int(call.data.split("_")[-1])
    tournament_id = await db.get_active_tournament()
    rounds = get_rounds(tournament_id)
    info = rounds.get(round_num)
    if info is None:
        return await call.answer("Noma'lum raund.", show_alert=True)

    if info["kind"] == "sticker":
        # Sticker Battle - avval admin'dan e'lon qilinadigan STIKER so'raymiz,
        # raund faqat shundan keyin (stiker+tarif kanalga ketgach) faollashadi.
        await state.update_data(pending_round_num=round_num, pending_tournament_id=tournament_id)
        await state.set_state(AdminStickerAnnounceStates.waiting_sticker)
        await call.message.edit_text(
            f"😂 <b>{info['name']}</b>\n\n"
            "Kanalga e'lon qilish uchun avval STIKERNI yuboring 👇"
        )
        await call.answer()
        return

    if info["kind"] == "assoc":
        # Rasmga oidlik - admin bir nechta rasm yuboradi, so'ng ✅ Tayyor bosadi.
        await state.update_data(pending_round_num=round_num, pending_tournament_id=tournament_id, assoc_images=[])
        await state.set_state(AdminAssocStates.collecting_images)
        await call.message.edit_text(
            f"🖼 <b>{info['name']}</b>\n\n"
            "Rasmlarni birma-bir botga yuboring (istagancha). Hammasini yuborib "
            "bo'lgach, pastdagi ✅ Tayyor tugmasini bosing - shundagina rasmlar "
            "RAQAMLANIB kanalga e'lon qilinadi.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Tayyor", callback_data="adm_assoc_done")]
            ]),
        )
        await call.answer()
        return

    await db.set_active_round(round_num)
    await call.message.edit_text(
        await admin_panel_text(), reply_markup=await admin_panel_kb(True, tournament_id, round_num)
    )
    await call.answer(f"{info['name']} boshlandi!")

    if info["kind"] == "external":
        text = f"🎬 Yangi raund boshlandi: <b>{info['name']}</b>\n\n{info['hint']}"
    else:
        text = (
            f"🎬 Yangi raund boshlandi: <b>{info['name']}</b>\n\n"
            f"{info['hint']}\n\n"
            "Javobingizni \"📤 Javob yuborish\" tugmasi orqali yuboring!"
        )
    await broadcast_to_users(text)
    await post_text_to_channel(text)

    # Dart/Futbol/Basketbol/Bowling kabi "o'yin" raundlari uchun e'lon
    # kanaldan tashqari muhokama/o'yin guruhiga (GAME_CHAT_ID) ham boriladi,
    # chunki aynan shu roundlar o'sha yerda o'ynaladi.
    if info.get("extra_target") == "game_chat" and config.GAME_CHAT_ID:
        try:
            await bot.send_message(config.GAME_CHAT_ID, text)
        except Exception as e:
            logger.error(f"GAME_CHAT_ID'ga e'lon yuborishda xatolik: {e}")

    asyncio.create_task(backup_db_to_telegram())


@dp.message(StateFilter(AdminStickerAnnounceStates.waiting_sticker), F.sticker)
async def adm_sticker_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(sticker_file_id=message.sticker.file_id)
    await state.set_state(AdminStickerAnnounceStates.waiting_caption)
    await message.answer("✏️ Endi shu stikerga tarif (izoh) matnini yuboring:")


@dp.message(StateFilter(AdminStickerAnnounceStates.waiting_sticker))
async def adm_sticker_wrong_type(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("❗️ Iltimos, aynan STIKER yuboring (matn emas).")


@dp.message(StateFilter(AdminStickerAnnounceStates.waiting_caption), F.text)
async def adm_sticker_caption_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    round_num = data.get("pending_round_num")
    tournament_id = data.get("pending_tournament_id") or await db.get_active_tournament()
    sticker_file_id = data.get("sticker_file_id")
    caption = message.text
    await state.clear()

    info = get_rounds(tournament_id)[round_num]
    await db.set_active_round(round_num)

    # Kanalga: stiker + tarif (test rejimida avtomatik admin(lar)ga yo'naladi)
    await post_sticker_to_channel(sticker_file_id, caption=f"🎬 {info['name']}\n\n{caption}")
    # Ro'yxatdan o'tgan foydalanuvchilarga ham bir xil tarzda
    if await db.is_test_mode():
        user_count = len(await db.get_all_user_ids())
        await send_test_mode_notice(
            f"🧪 <b>[TEST] Sticker broadcast {user_count} ta foydalanuvchiga ketishi kerak edi.</b>"
        )
    else:
        try:
            kb = await main_menu_kb()
            for tg_id in await db.get_all_user_ids():
                try:
                    await bot.send_sticker(tg_id, sticker=sticker_file_id)
                    await bot.send_message(
                        tg_id, f"🎬 <b>{info['name']}</b>\n\n{caption}\n\n"
                        "Javobingizni \"📤 Javob yuborish\" orqali stiker sifatida yuboring!",
                        reply_markup=kb,
                    )
                except Exception as e:
                    logger.warning(f"Sticker broadcast {tg_id}'ga yetmadi: {e}")
                await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Sticker broadcast xatosi: {e}")

    await message.answer(
        f"✅ {info['name']} boshlandi va kanalga e'lon qilindi!",
        reply_markup=await admin_panel_kb(True, tournament_id, round_num),
    )
    asyncio.create_task(backup_db_to_telegram())


# ------------------------------------------------------------
#  "RASMGA OIDLIK" - admin rasmlarni yig'ish oqimi
# ------------------------------------------------------------

@dp.message(StateFilter(AdminAssocStates.collecting_images), F.photo)
async def adm_assoc_photo_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    images = data.get("assoc_images", [])
    images.append(message.photo[-1].file_id)
    await state.update_data(assoc_images=images)
    await message.answer(f"🖼 {len(images)}-rasm qabul qilindi. Yana yuboring yoki ✅ Tayyor bosing.")


@dp.message(StateFilter(AdminAssocStates.collecting_images))
async def adm_assoc_wrong_type(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("❗️ Iltimos, aynan RASM yuboring (yoki ✅ Tayyor tugmasini bosing).")


@dp.callback_query(F.data == "adm_assoc_done")
async def adm_assoc_done(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    current_state = await state.get_state()
    if current_state != AdminAssocStates.collecting_images.state:
        return await call.answer("Bu tugma endi faol emas.", show_alert=True)

    data = await state.get_data()
    images = data.get("assoc_images", [])
    round_num = data.get("pending_round_num")
    tournament_id = data.get("pending_tournament_id") or await db.get_active_tournament()
    if not images:
        return await call.answer("❗️ Kamida bitta rasm yuboring!", show_alert=True)

    info = get_rounds(tournament_id)[round_num]
    await state.clear()

    await db.clear_assoc_images(tournament_id, round_num)
    for i, file_id in enumerate(images, start=1):
        await db.add_assoc_image(tournament_id, round_num, i, file_id)
    await db.set_active_round(round_num)

    await call.answer("✅ E'lon qilinmoqda...")
    await call.message.edit_text(
        await admin_panel_text(), reply_markup=await admin_panel_kb(True, tournament_id, round_num)
    )

    # Kanalga: avval qoida/tarif matni, keyin rasmlar RAQAMLAB
    # (test rejimida post_text_to_channel avtomatik admin(lar)ga yo'naladi)
    await post_text_to_channel(f"🎬 Yangi raund boshlandi: <b>{info['name']}</b>\n\n{info['hint']}")
    if await db.is_test_mode():
        # Rasmlar HAM to'g'ridan-to'g'ri senga (admin) yuboriladi - aks holda
        # ularni hech qayerda ko'rmaysan va AI'ni sinab bo'lmaydi. Raqami
        # kanaldagi bilan bir xil, shuning uchun "<raqam>: so'z1, so'z2"
        # formatida javob yuborib, AI (Groq) baholovini xuddi jonli rejimdagidek
        # test qilishing mumkin.
        for target_id in await _test_mode_targets():
            for i, file_id in enumerate(images, start=1):
                try:
                    await bot.send_photo(target_id, photo=file_id, caption=f"🧪 [TEST] {i}-rasm")
                except Exception as e:
                    logger.error(f"Test rejimida rasmni {target_id}'ga yuborishda xatolik: {e}")
                await asyncio.sleep(0.05)
    else:
        for i, file_id in enumerate(images, start=1):
            try:
                await bot.send_photo(config.CHANNEL_ID, photo=file_id, caption=f"{i}-rasm")
            except Exception as e:
                logger.error(f"Kanalga {i}-rasmni yuborishda xatolik: {e}")
            await asyncio.sleep(0.05)

    # Foydalanuvchilarga DM: matnli ko'rsatma (rasmlarni har biriga qayta
    # yubormaymiz - ular kanalda ko'rinadi, ortiqcha flood'ning keragi yo'q)
    dm_text = (
        f"🎬 Yangi raund boshlandi: <b>{info['name']}</b>\n\n{info['hint']}\n\n"
        f"🖼 Rasmlarni kanalda ({config.CHANNEL_USERNAME}) ko'ring."
    )
    await broadcast_to_users(dm_text)
    asyncio.create_task(backup_db_to_telegram())


async def broadcast_to_users(text: str) -> None:
    """Barcha ro'yxatdan o'tgan foydalanuvchilarga xabar yuboradi (yangilangan
    tugmalar bilan), masalan yangi raund boshlanganda.
    TEST REJIMIDA hech kimga ketmaydi - o'rniga admin(lar)ga nechta userga
    ketishi kerak ediligi va matni [TEST] belgisi bilan yuboriladi."""
    if await db.is_test_mode():
        user_count = len(await db.get_all_user_ids())
        await send_test_mode_notice(
            f"🧪 <b>[TEST] Broadcast {user_count} ta foydalanuvchiga ketishi kerak edi:</b>\n\n{text}"
        )
        return
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


@dp.message(StateFilter(AdminMessageUserStates.waiting_message), F.text)
async def adm_msguser_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    target_id = data.get("target_tg_id")
    await state.clear()
    if not target_id:
        return
    try:
        await bot.send_message(target_id, f"📩 <b>Admindan xabar:</b>\n\n{html.escape(message.text)}")
        await message.answer("✅ Xabar yuborildi.")
    except Exception as e:
        logger.error(f"Foydalanuvchiga xabar yuborishda xatolik: {e}")
        await message.answer("❌ Xabar yuborilmadi (ehtimol foydalanuvchi botni bloklagan yoki hech qachon /start bosmagan).")


# ==================================================================
#  ADMIN: STATISTIKA
# ==================================================================

EVENT_LABELS = {
    "scheduled_post_sent": "📅 Rejalashtirilgan post yuborildi",
    "group_topic_sent": "🗨 Guruh mavzu boshlovchisi yuborildi",
    "auto_comment_sent": "💬 Auto-izoh yozildi",
    "group_reply_sent": "🗣 Real-vaqt javob berildi",
    "group_message_seen": "👥 Guruhdagi umumiy xabarlar",
}


@dp.callback_query(F.data == "adm_stats")
async def adm_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)

    total_users = await db.get_total_users_count()
    total_answers = await db.get_total_answers_count()

    now = datetime.now()
    today_start = now.strftime("%Y-%m-%dT00:00:00")
    week_start = (now - timedelta(days=7)).isoformat(timespec="seconds")

    today_counts = await db.get_ai_event_counts(since_iso=today_start)
    week_counts = await db.get_ai_event_counts(since_iso=week_start)
    total_counts = await db.get_ai_event_counts()

    lines = [
        "📊 <b>Statistika</b>\n",
        f"👤 Jami ro'yxatdan o'tgan foydalanuvchilar: <b>{total_users}</b>",
        f"📝 Jami yuborilgan javoblar: <b>{total_answers}</b>\n",
        "🤖 <b>AI faolligi</b> (bugun / oxirgi 7 kun / jami):",
    ]
    for event_type, label in EVENT_LABELS.items():
        t = today_counts.get(event_type, 0)
        w = week_counts.get(event_type, 0)
        a = total_counts.get(event_type, 0)
        lines.append(f"{label}: {t} / {w} / {a}")

    if not await db.get_ai_group_chat_id():
        lines.append("\n⚠️ Guruh ID sozlanmagan - guruh statistikasi to'planmayapti.")

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_back")]])
    await call.message.edit_text("\n".join(lines), reply_markup=kb)
    await call.answer()


# ==================================================================
#  ADMIN: AI PROVAYDERLAR (kalitlarni /admin panelidan boshqarish)
# ==================================================================

async def _ai_menu_text_and_kb():
    keys = await db.get_all_ai_keys()
    lines = ["🤖 <b>AI provayderlar</b>\n"]
    rows = []
    if not keys:
        lines.append("Hozircha hech qanday kalit qo'shilmagan.")
    else:
        for k in keys:
            label = ai_providers.PROVIDERS.get(k["provider"], {}).get("label", k["provider"])
            status = "🟢" if k["enabled"] else "🔴"
            nick = f" — {html.escape(k['label'])}" if k["label"] else ""
            lines.append(f"{status} <b>{label}</b> #{k['id']} ({ai_providers.mask_key(k['api_key'])}){nick}")
            rows.append([
                InlineKeyboardButton(
                    text=f"{status} {label} #{k['id']}", callback_data=f"adm_ai_toggle_{k['id']}"
                ),
                InlineKeyboardButton(text="🗑", callback_data=f"adm_ai_del_{k['id']}"),
            ])
    rows.append([InlineKeyboardButton(text="➕ Kalit qo'shish", callback_data="adm_ai_addkey_menu")])
    rows.append([InlineKeyboardButton(text="⚙️ Avtomatlashtirish (post/guruh)", callback_data="adm_aiauto_menu")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_back")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_ai_menu")
async def adm_ai_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    text, kb = await _ai_menu_text_and_kb()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "adm_ai_addkey_menu")
async def adm_ai_addkey_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    rows = [
        [InlineKeyboardButton(text=cfg["label"], callback_data=f"adm_ai_addkey_{pid}")]
        for pid, cfg in ai_providers.PROVIDERS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_ai_menu")])
    await call.message.edit_text(
        "🤖 <b>Qaysi provayder uchun kalit qo'shmoqchisiz?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm_ai_addkey_"))
async def adm_ai_addkey_provider(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    provider = call.data.removeprefix("adm_ai_addkey_")
    if provider not in ai_providers.PROVIDERS:
        return await call.answer("Noma'lum provayder.", show_alert=True)

    await state.update_data(pending_provider=provider)
    await state.set_state(AdminAIKeyStates.waiting_key)
    label = ai_providers.PROVIDERS[provider]["label"]
    await call.message.edit_text(
        f"🔑 <b>{label}</b> uchun API kalitini yuboring.\n\n"
        "⚠️ Xabaringiz o'qilgach DARHOL o'chiriladi (chatda tokenning izi qolmasligi uchun)."
    )
    await call.answer()


@dp.message(StateFilter(AdminAIKeyStates.waiting_key), F.text)
async def adm_ai_key_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    provider = data.get("pending_provider")
    await state.clear()

    api_key = message.text.strip()
    # XAVFSIZLIK: kalit chatda qolib ketmasligi uchun admin xabarini darhol o'chiramiz.
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Admin kalit xabarini o'chirib bo'lmadi: {e}")

    if not provider or provider not in ai_providers.PROVIDERS or not api_key:
        await message.answer("❗️ Xatolik yuz berdi, qaytadan /admin > 🤖 AI provayderlar orqali urinib ko'ring.")
        return

    key_id = await db.add_ai_key(provider, api_key)
    label = ai_providers.PROVIDERS[provider]["label"]
    text, kb = await _ai_menu_text_and_kb()
    await message.answer(
        f"✅ {label} kaliti qo'shildi (#{key_id}, {ai_providers.mask_key(api_key)}).\n\n{text}",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("adm_ai_toggle_"))
async def adm_ai_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    key_id = int(call.data.removeprefix("adm_ai_toggle_"))
    await db.toggle_ai_key(key_id)
    text, kb = await _ai_menu_text_and_kb()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("adm_ai_del_"))
async def adm_ai_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    key_id = int(call.data.removeprefix("adm_ai_del_"))
    await db.delete_ai_key(key_id)
    text, kb = await _ai_menu_text_and_kb()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer("🗑 O'chirildi.")


# ==================================================================
#  AI AVTOMATLASHTIRISH: rejalashtirilgan postlar, guruh mavzu
#  boshlovchisi, real-vaqt javob va sozlamalar (guruh ID, xarakter)
# ==================================================================

DAYS_LABELS = {"daily": "Har kuni", "weekdays": "Ish kunlari (Dush-Jum)", "weekend": "Dam olish (Shan-Yak)"}
CTYPE_LABELS = {
    "text": "📝 Faqat matn (AI yozadi)",
    "photo": "🖼 Rasm (o'zim yuboraman)",
    "video": "🎬 Video (o'zim yuboraman)",
    "audio": "🎵 Audio (o'zim yuboraman)",
}
TARGET_LABELS = {"channel": "📢 Kanal", "group": "👥 Guruh"}


async def _notify_admins(text: str) -> None:
    """AI avtomatlashtirish xatoliklarini adminlarga yetkazish uchun - avval
    bu xatoliklar faqat logga yozilib, jim ketardi va admin sabab
    tushunmasdan qolardi (masalan AI kaliti sozlanmagani)."""
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logger.warning(f"Adminga ({admin_id}) xatolik xabarini yuborib bo'lmadi: {e}")


async def _fire_scheduled_post(rule_id: int, cfg: dict) -> None:
    """Rejalashtirilgan post ishga tushganda chaqiriladi. Matn - to'liq AI
    tomonidan yoziladi. Rasm/video/audio - admin OLDINDAN yuborgan haqiqiy
    faylni (file_id orqali) joylaydi, faqat tarif matnini AI yozadi -
    AI hech qachon media generatsiya qilmaydi, faqat admin yuborgan faylni tarqatadi."""
    persona = await _build_ai_system(extra_rule=cfg.get("extra_rule"))
    prompt = cfg.get("prompt", "")
    target = cfg.get("target", "channel")
    ctype = cfg.get("content", "text")
    media_file_id = cfg.get("media_file_id")

    if target == "group":
        chat_id = await db.get_ai_group_chat_id()
        if not chat_id:
            logger.warning(f"Rejalashtirilgan post #{rule_id}: guruh ID sozlanmagan, o'tkazib yuborildi.")
            await _notify_admins(
                f"⚠️ Rejalashtirilgan post #{rule_id} ishga tushmadi: guruh ID sozlanmagan.\n"
                "/admin > 🤖 AI provayderlar > ⚙️ Avtomatlashtirish > 🎯 Guruh ID sozlash orqali kiriting."
            )
            return
    else:
        chat_id = config.CHANNEL_ID

    if not await ai_providers.has_any_key():
        logger.warning(f"Rejalashtirilgan post #{rule_id}: hech qanday AI kaliti sozlanmagan.")
        await _notify_admins(
            f"⚠️ Rejalashtirilgan post #{rule_id} ishga tushmadi: AI kaliti sozlanmagan.\n"
            "/admin > 🤖 AI provayderlar > ➕ Kalit qo'shish orqali kamida bitta kalit qo'shing."
        )
        return

    caption = await ai_providers.generate(prompt=prompt, system=persona)
    if caption is None:
        logger.warning(f"Rejalashtirilgan post #{rule_id}: AI matn generatsiya qila olmadi.")
        await _notify_admins(
            f"⚠️ Rejalashtirilgan post #{rule_id} ishga tushmadi: AI so'rovi muvaffaqiyatsiz tugadi "
            "(barcha kalit/provayderlar javob bermadi - kalit noto'g'ri yoki limit tugagan bo'lishi mumkin)."
        )
        return

    if await db.is_test_mode():
        await send_test_mode_notice(
            f"🧪 <b>[TEST] Rejalashtirilgan post ketishi kerak edi</b> (#{rule_id}, {ctype}):\n\n{caption}"
        )
        return

    try:
        if ctype == "photo" and media_file_id:
            await bot.send_photo(chat_id, media_file_id, caption=caption)
        elif ctype == "video" and media_file_id:
            await bot.send_video(chat_id, media_file_id, caption=caption)
        elif ctype == "audio" and media_file_id:
            await bot.send_audio(chat_id, media_file_id, caption=caption)
        else:
            await bot.send_message(chat_id, caption)
        await db.log_ai_event("scheduled_post_sent")
    except Exception as e:
        logger.error(f"Rejalashtirilgan post #{rule_id} yuborishda xatolik: {e}")
        await _notify_admins(f"⚠️ Rejalashtirilgan post #{rule_id} yuborishda xatolik: {e}")


async def _run_scheduled_posts() -> None:
    now = datetime.now()
    hhmm = now.strftime("%H:%M")
    today_str = now.strftime("%Y-%m-%d")
    for rule in await db.get_enabled_ai_rules("scheduled_post"):
        try:
            cfg = json.loads(rule["config"])
        except Exception:
            continue
        if cfg.get("time") != hhmm:
            continue
        days_code = cfg.get("days", "daily")
        if days_code == "weekdays" and now.weekday() not in (0, 1, 2, 3, 4):
            continue
        if days_code == "weekend" and now.weekday() not in (5, 6):
            continue
        last_run = rule["last_run_at"] or ""
        if last_run.startswith(today_str):
            continue  # shu vaqt oralig'ida bugun allaqachon ishga tushgan
        await _fire_scheduled_post(rule["id"], cfg)
        await db.update_ai_rule_last_run(rule["id"], now.isoformat(timespec="seconds"))



async def _run_group_topics() -> None:
    group_chat_id = await db.get_ai_group_chat_id()
    if not group_chat_id:
        return
    now = datetime.now()
    for rule in await db.get_enabled_ai_rules("group_topic"):
        try:
            cfg = json.loads(rule["config"])
        except Exception:
            continue
        interval_hours = cfg.get("interval_hours", 6)
        last_run = rule["last_run_at"]
        if last_run:
            try:
                if (now - datetime.fromisoformat(last_run)).total_seconds() < interval_hours * 3600:
                    continue
            except Exception:
                pass
        persona = await _build_ai_system()
        text = await ai_providers.generate(
            prompt=cfg.get("prompt", "Guruhda ishtirokchilar bilan qiziqarli mavzu och."), system=persona
        )
        if not text:
            continue
        if await db.is_test_mode():
            await send_test_mode_notice(f"🧪 <b>[TEST] Guruh mavzu boshlovchisi ketishi kerak edi:</b>\n\n{text}")
        else:
            try:
                await bot.send_message(group_chat_id, text)
                await db.log_ai_event("group_topic_sent")
            except Exception as e:
                logger.error(f"Guruh mavzu boshlovchisini yuborishda xatolik: {e}")
        await db.update_ai_rule_last_run(rule["id"], now.isoformat(timespec="seconds"))


async def ai_scheduler_loop() -> None:
    """Har daqiqada rejalashtirilgan postlar va guruh mavzu boshlovchisi
    qoidalarini tekshiradi. periodic_backup_loop bilan bir xil naqshda -
    xatolik butun loopni o'ldirmasligi uchun try/except ichida."""
    while True:
        await asyncio.sleep(60)
        try:
            await _run_scheduled_posts()
        except Exception as e:
            logger.error(f"Rejalashtirilgan postlarni tekshirishda xatolik: {e}")
        try:
            await _run_group_topics()
        except Exception as e:
            logger.error(f"Guruh mavzu boshlovchisini tekshirishda xatolik: {e}")


# ------------------------- ADMIN: AVTOMATLASHTIRISH MENYUSI -------------------------

async def _aiauto_menu_text_and_kb():
    group_id = await db.get_ai_group_chat_id()
    group_activity = await db.is_ai_group_activity_enabled()
    auto_comment = await db.is_ai_auto_comment_enabled()
    realtime_label = "🟢 yoniq" if group_activity else "🔴 o'chiq"
    autocomment_label = "🟢 yoniq" if auto_comment else "🔴 o'chiq"
    text = (
        "⚙️ <b>AI avtomatlashtirish</b>\n\n"
        f"🎯 Guruh ID: <code>{group_id if group_id else 'sozlanmagan'}</code>\n"
        f"🗣 Real-vaqt javob (mention/reply): {realtime_label}\n"
        f"💬 Auto-izoh (yangi post): {autocomment_label}"
    )
    rows = [
        [InlineKeyboardButton(text="📅 Rejalashtirilgan postlar", callback_data="adm_schedposts_menu")],
        [InlineKeyboardButton(text="🗨 Guruh mavzu boshlovchisi", callback_data="adm_grouptopics_menu")],
        [InlineKeyboardButton(
            text=("🔴 Real-vaqt javobni o'chirish" if group_activity else "🟢 Real-vaqt javobni yoqish"),
            callback_data="adm_toggle_groupactivity",
        )],
        [InlineKeyboardButton(
            text=("🔴 Auto-izohni o'chirish" if auto_comment else "🟢 Auto-izohni yoqish"),
            callback_data="adm_toggle_autocomment",
        )],
        [InlineKeyboardButton(text="🎯 Guruh ID sozlash", callback_data="adm_set_groupid")],
        [InlineKeyboardButton(text="🎭 AI xarakterini sozlash", callback_data="adm_set_persona")],
        [InlineKeyboardButton(text="📏 Umumiy qoida (barcha AI matnlari)", callback_data="adm_set_general_rule")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_ai_menu")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_aiauto_menu")
async def adm_aiauto_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    text, kb = await _aiauto_menu_text_and_kb()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "adm_toggle_groupactivity")
async def adm_toggle_groupactivity(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.set_ai_group_activity_enabled(not await db.is_ai_group_activity_enabled())
    text, kb = await _aiauto_menu_text_and_kb()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "adm_toggle_autocomment")
async def adm_toggle_autocomment(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.set_ai_auto_comment_enabled(not await db.is_ai_auto_comment_enabled())
    text, kb = await _aiauto_menu_text_and_kb()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "adm_set_groupid")
async def adm_set_groupid(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await state.set_state(AdminGroupChatIdStates.waiting_chat_id)
    await call.message.edit_text(
        "🎯 Guruh (muhokama chati) ID'sini yuboring.\n\n"
        "Masalan: <code>-1001234567890</code>\n\n"
        "💡 Bilmasangiz: botni shu guruhga admin qilib qo'shing, guruhda istalgan xabar yozing - "
        "log/xatolik xabarlarida chat ID ko'rinadi, yoki @userinfobot kabi botlardan foydalaning."
    )
    await call.answer()


@dp.message(StateFilter(AdminGroupChatIdStates.waiting_chat_id), F.text)
async def adm_set_groupid_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        chat_id = int(message.text.strip())
    except ValueError:
        await message.answer("❗️ Noto'g'ri format. Butun son bo'lishi kerak, masalan -1001234567890.")
        return
    await db.set_ai_group_chat_id(chat_id)
    text, kb = await _aiauto_menu_text_and_kb()
    await message.answer(f"✅ Guruh ID saqlandi: <code>{chat_id}</code>\n\n{text}", reply_markup=kb)


@dp.callback_query(F.data == "adm_set_persona")
async def adm_set_persona(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    current = await db.get_ai_persona()
    await state.set_state(AdminPersonaStates.waiting_persona)
    await call.message.edit_text(
        f"🎭 AI'ning hozirgi xarakteri (system prompt):\n\n<i>{html.escape(current)}</i>\n\n"
        "Yangisini yozib yuboring (butun matnni almashtiradi):"
    )
    await call.answer()


@dp.message(StateFilter(AdminPersonaStates.waiting_persona), F.text)
async def adm_set_persona_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await db.set_ai_persona(message.text.strip())
    text, kb = await _aiauto_menu_text_and_kb()
    await message.answer(f"✅ AI xarakteri yangilandi.\n\n{text}", reply_markup=kb)


@dp.callback_query(F.data == "adm_set_general_rule")
async def adm_set_general_rule(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    current = await db.get_ai_general_rule()
    await state.set_state(AdminGeneralRuleStates.waiting_rule)
    current_display = html.escape(current) if current else "<i>(sozlanmagan)</i>"
    await call.message.edit_text(
        f"📏 Hozirgi umumiy qoida:\n\n{current_display}\n\n"
        "Bu qoida xarakterdan (persona) farqli - BARCHA AI generatsiyalariga "
        "(rejalashtirilgan postlar, auto-izoh, guruh mavzu boshlovchisi, real-vaqt javoblar) "
        "qo'llaniladi. Masalan: <i>\"Har doim qisqaroq yoz va oxirida @ParadoksHub deb qo'sh\"</i>.\n\n"
        "Yangisini yozing (butunlay almashtiradi), yoki tozalash uchun <code>-</code> yuboring:"
    )
    await call.answer()


@dp.message(StateFilter(AdminGeneralRuleStates.waiting_rule), F.text)
async def adm_set_general_rule_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    new_rule = message.text.strip()
    if new_rule == "-":
        new_rule = ""
    await db.set_ai_general_rule(new_rule)
    text, kb = await _aiauto_menu_text_and_kb()
    status = "tozalandi" if not new_rule else "yangilandi"
    await message.answer(f"✅ Umumiy qoida {status}.\n\n{text}", reply_markup=kb)


# ------------------------- ADMIN: REJALASHTIRILGAN POSTLAR -------------------------

async def _schedposts_menu_text_and_kb():
    rules = await db.get_all_ai_rules("scheduled_post")
    lines = ["📅 <b>Rejalashtirilgan postlar</b>\n"]
    rows = []
    if not rules:
        lines.append("Hozircha hech qanday post rejalashtirilmagan.")
    for rule in rules:
        try:
            cfg = json.loads(rule["config"])
        except Exception:
            cfg = {}
        status = "🟢" if rule["enabled"] else "🔴"
        summary = (
            f"{cfg.get('time', '?')} | {DAYS_LABELS.get(cfg.get('days'), '?')} | "
            f"{CTYPE_LABELS.get(cfg.get('content'), '?')} → {TARGET_LABELS.get(cfg.get('target'), '?')}"
        )
        lines.append(f"{status} #{rule['id']}: {summary}")
        rows.append([
            InlineKeyboardButton(text=f"{status} #{rule['id']}", callback_data=f"adm_schedpost_toggle_{rule['id']}"),
            InlineKeyboardButton(text="▶️ Hozir sinash", callback_data=f"adm_schedpost_test_{rule['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"adm_schedpost_del_{rule['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Yangi post qo'shish", callback_data="adm_schedpost_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_aiauto_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_schedposts_menu")
async def adm_schedposts_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    text, kb = await _schedposts_menu_text_and_kb()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("adm_schedpost_toggle_"))
async def adm_schedpost_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.toggle_ai_rule(int(call.data.removeprefix("adm_schedpost_toggle_")))
    text, kb = await _schedposts_menu_text_and_kb()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("adm_schedpost_test_"))
async def adm_schedpost_test(call: CallbackQuery):
    """Vaqt/kunni kutmasdan, qoidani DARHOL bir marta ishga tushiradi -
    sozlamalar (AI kalit, guruh ID va h.k.) to'g'ri ishlayotganini tez
    tekshirish uchun. last_run_at'ga TA'SIR QILMAYDI - rejadagi asl vaqt
    o'zgarmaydi."""
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    rule_id = int(call.data.removeprefix("adm_schedpost_test_"))
    rules = await db.get_all_ai_rules("scheduled_post")
    rule = next((r for r in rules if r["id"] == rule_id), None)
    if not rule:
        return await call.answer("Topilmadi.", show_alert=True)
    try:
        cfg = json.loads(rule["config"])
    except Exception:
        return await call.answer("❗️ Qoida konfiguratsiyasi buzilgan.", show_alert=True)
    await call.answer("⏳ Sinovdan o'tkazilyapti...")
    await _fire_scheduled_post(rule_id, cfg)
    await call.message.answer(
        "✅ Sinov yakunlandi. Agar hech narsa kelmagan bo'lsa - adminlarga xatolik sababi haqida xabar ketgan bo'lishi kerak (yuqoriga qarang)."
    )


@dp.callback_query(F.data.startswith("adm_schedpost_del_"))
async def adm_schedpost_del(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.delete_ai_rule(int(call.data.removeprefix("adm_schedpost_del_")))
    text, kb = await _schedposts_menu_text_and_kb()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer("🗑 O'chirildi.")




@dp.callback_query(F.data == "adm_schedpost_add")
async def adm_schedpost_add(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await state.set_state(AdminSchedPostStates.waiting_time)
    await call.message.edit_text(
        "🕒 Vaqtni kiriting (24 soatlik format, masalan <code>09:00</code>):"
    )
    await call.answer()


@dp.message(StateFilter(AdminSchedPostStates.waiting_time), F.text)
async def adm_schedpost_time_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    time_str = message.text.strip()
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", time_str):
        await message.answer("❗️ Noto'g'ri format. Masalan: 09:00 yoki 21:30. Qaytadan kiriting:")
        return
    await state.update_data(sp_time=time_str)
    rows = [[InlineKeyboardButton(text=label, callback_data=f"adm_schedpost_days_{code}")]
            for code, label in DAYS_LABELS.items()]
    await message.answer("📆 Qaysi kunlari yuborilsin?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("adm_schedpost_days_"))
async def adm_schedpost_days(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    days_code = call.data.removeprefix("adm_schedpost_days_")
    await state.update_data(sp_days=days_code)
    rows = [[InlineKeyboardButton(text=label, callback_data=f"adm_schedpost_ctype_{code}")]
            for code, label in CTYPE_LABELS.items()]
    await call.message.edit_text("🎨 Kontent turi qanday bo'lsin?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("adm_schedpost_ctype_"))
async def adm_schedpost_ctype(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    ctype = call.data.removeprefix("adm_schedpost_ctype_")
    await state.update_data(sp_ctype=ctype)

    if ctype == "text":
        # Matnli post uchun fayl kerak emas - to'g'ridan-to'g'ri nishonni so'raymiz.
        rows = [[InlineKeyboardButton(text=label, callback_data=f"adm_schedpost_target_{code}")]
                for code, label in TARGET_LABELS.items()]
        await call.message.edit_text("🎯 Qayerga yuborilsin?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await call.answer()
        return

    await state.set_state(AdminSchedPostStates.waiting_media)
    kind_label = {"photo": "rasm", "video": "video", "audio": "audio"}.get(ctype, "fayl")
    await call.message.edit_text(
        f"📎 Kanalga/guruhga yuboriladigan haqiqiy {kind_label}ni shu yerga yuboring "
        "(AI hech narsa generatsiya qilmaydi - siz yuborgan fayl aynan shu ko'rinishda joylanadi, "
        "faqat tarif matnini AI yozadi)."
    )
    await call.answer()


@dp.message(StateFilter(AdminSchedPostStates.waiting_media), F.photo | F.video | F.audio)
async def adm_schedpost_media_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    expected = data.get("sp_ctype")

    if expected == "photo" and message.photo:
        file_id = message.photo[-1].file_id
    elif expected == "video" and message.video:
        file_id = message.video.file_id
    elif expected == "audio" and message.audio:
        file_id = message.audio.file_id
    else:
        await message.answer(f"❗️ Siz «{CTYPE_LABELS.get(expected, '?')}» turini tanlagansiz, shunga mos fayl yuboring.")
        return

    await state.update_data(sp_media_file_id=file_id)
    rows = [[InlineKeyboardButton(text=label, callback_data=f"adm_schedpost_target_{code}")]
            for code, label in TARGET_LABELS.items()]
    await message.answer("✅ Fayl qabul qilindi.\n\n🎯 Qayerga yuborilsin?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(StateFilter(AdminSchedPostStates.waiting_media))
async def adm_schedpost_media_wrong_type(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("❗️ Bu fayl turi mos emas. Rasm/video/audio sifatida (fayl/hujjat emas) yuboring.")


@dp.callback_query(F.data.startswith("adm_schedpost_target_"))
async def adm_schedpost_target(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    target = call.data.removeprefix("adm_schedpost_target_")
    if target == "group" and not await db.get_ai_group_chat_id():
        return await call.answer("⚠️ Avval guruh ID'sini sozlang (⚙️ Avtomatlashtirish > 🎯 Guruh ID).", show_alert=True)
    await state.update_data(sp_target=target)
    await state.set_state(AdminSchedPostStates.waiting_prompt)
    await call.message.edit_text(
        "✍️ Tarif/matn uchun AI'ga ko'rsatma yozing.\n\n"
        "Masalan: <i>\"Kunning qiziqarli faktini yoz\"</i> yoki "
        "<i>\"Bugungi turnir haqida qisqa reklama matni yoz\"</i>."
    )
    await call.answer()


@dp.message(StateFilter(AdminSchedPostStates.waiting_prompt), F.text)
async def adm_schedpost_prompt_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(sp_prompt=message.text.strip())
    await state.set_state(AdminSchedPostStates.waiting_extra_rule)
    rows = [[
        InlineKeyboardButton(text="➕ Ha, qo'shaman", callback_data="adm_schedpost_extra_yes"),
        InlineKeyboardButton(text="⏭ Yo'q, kerak emas", callback_data="adm_schedpost_extra_no"),
    ]]
    await message.answer(
        "📏 Shu POST uchungina qo'shimcha qoida qo'shmoqchimisiz? "
        "(masalan: \"faqat 2 gapdan iborat bo'lsin\", \"emoji ishlatma\"). "
        "Bu umumiy qoidaga QO'SHIMCHA ravishda ishlaydi, uni almashtirmaydi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data == "adm_schedpost_extra_no")
async def adm_schedpost_extra_no(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await _finalize_schedpost(call.message, state, extra_rule=None)
    await call.answer()


@dp.callback_query(F.data == "adm_schedpost_extra_yes")
async def adm_schedpost_extra_yes(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await call.message.edit_text("✍️ Qo'shimcha qoidani yozing:")
    await call.answer()


@dp.message(StateFilter(AdminSchedPostStates.waiting_extra_rule), F.text)
async def adm_schedpost_extra_rule_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await _finalize_schedpost(message, state, extra_rule=message.text.strip())


async def _finalize_schedpost(message: Message, state: FSMContext, extra_rule: str | None) -> None:
    data = await state.get_data()
    await state.clear()
    cfg = {
        "time": data.get("sp_time"),
        "days": data.get("sp_days", "daily"),
        "content": data.get("sp_ctype", "text"),
        "target": data.get("sp_target", "channel"),
        "prompt": data.get("sp_prompt", ""),
    }
    if data.get("sp_media_file_id"):
        cfg["media_file_id"] = data["sp_media_file_id"]
    if extra_rule:
        cfg["extra_rule"] = extra_rule
    rule_id = await db.add_ai_rule("scheduled_post", json.dumps(cfg, ensure_ascii=False))
    text, kb = await _schedposts_menu_text_and_kb()
    await message.answer(f"✅ Rejalashtirilgan post qo'shildi (#{rule_id}).\n\n{text}", reply_markup=kb)


# ------------------------- ADMIN: GURUH MAVZU BOSHLOVCHISI -------------------------

async def _grouptopics_menu_text_and_kb():
    rules = await db.get_all_ai_rules("group_topic")
    lines = ["🗨 <b>Guruh mavzu boshlovchisi</b>\n"]
    rows = []
    if not rules:
        lines.append("Hozircha hech qanday qoida yo'q.")
    for rule in rules:
        try:
            cfg = json.loads(rule["config"])
        except Exception:
            cfg = {}
        status = "🟢" if rule["enabled"] else "🔴"
        lines.append(f"{status} #{rule['id']}: har {cfg.get('interval_hours', '?')} soatda — {cfg.get('prompt', '')[:40]}")
        rows.append([
            InlineKeyboardButton(text=f"{status} #{rule['id']}", callback_data=f"adm_grouptopic_toggle_{rule['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"adm_grouptopic_del_{rule['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Yangi qoida qo'shish", callback_data="adm_grouptopic_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_aiauto_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_grouptopics_menu")
async def adm_grouptopics_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    if not await db.get_ai_group_chat_id():
        return await call.answer("⚠️ Avval guruh ID'sini sozlang (⚙️ Avtomatlashtirish > 🎯 Guruh ID).", show_alert=True)
    text, kb = await _grouptopics_menu_text_and_kb()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("adm_grouptopic_toggle_"))
async def adm_grouptopic_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.toggle_ai_rule(int(call.data.removeprefix("adm_grouptopic_toggle_")))
    text, kb = await _grouptopics_menu_text_and_kb()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("adm_grouptopic_del_"))
async def adm_grouptopic_del(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await db.delete_ai_rule(int(call.data.removeprefix("adm_grouptopic_del_")))
    text, kb = await _grouptopics_menu_text_and_kb()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer("🗑 O'chirildi.")


@dp.callback_query(F.data == "adm_grouptopic_add")
async def adm_grouptopic_add(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await state.set_state(AdminGroupTopicStates.waiting_interval)
    await call.message.edit_text("⏱ Necha soatda bir marta mavzu ochilsin? (masalan: 6)")
    await call.answer()


@dp.message(StateFilter(AdminGroupTopicStates.waiting_interval), F.text)
async def adm_grouptopic_interval_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        hours = int(message.text.strip())
        if hours < 1:
            raise ValueError
    except ValueError:
        await message.answer("❗️ Musbat butun son kiriting (masalan: 6).")
        return
    await state.update_data(gt_interval=hours)
    await state.set_state(AdminGroupTopicStates.waiting_prompt)
    await message.answer(
        "✍️ AI uchun ko'rsatma yozing.\n\n"
        "Masalan: <i>\"Ishtirokchilardan bugungi kayfiyati haqida so'ra\"</i> yoki "
        "<i>\"Zakovat/bilim haqida qiziqarli savol ber\"</i>."
    )


@dp.message(StateFilter(AdminGroupTopicStates.waiting_prompt), F.text)
async def adm_grouptopic_prompt_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    cfg = {"interval_hours": data.get("gt_interval", 6), "prompt": message.text.strip()}
    rule_id = await db.add_ai_rule("group_topic", json.dumps(cfg, ensure_ascii=False))
    text, kb = await _grouptopics_menu_text_and_kb()
    await message.answer(f"✅ Qoida qo'shildi (#{rule_id}).\n\n{text}", reply_markup=kb)


# ------------------------- GURUHDA REAL-VAQT AI JAVOBI -------------------------

@dp.message(F.chat.type.in_({"group", "supergroup"}), F.text)
async def ai_group_realtime_reply(message: Message):
    """Guruhda botga reply qilingan yoki @username orqali mention qilingan
    xabarlarga AI javob beradi. Boshqa har qanday oddiy xabarga tegmaydi -
    aks holda guruh AI-spam bo'lib qolar edi."""
    if message.from_user and message.from_user.is_bot:
        return  # boshqa botlar (yoki o'zimiz) bilan cheksiz sikl bo'lmasin
    group_chat_id = await db.get_ai_group_chat_id()
    if not group_chat_id or message.chat.id != group_chat_id:
        return

    # Guruh faolligi statistikasi uchun - AI real-vaqt javobi
    # yoqilgan/o'chirilganidan qat'i nazar, umumiy xabar oqimini kuzatamiz.
    await db.log_ai_event("group_message_seen")

    if not await db.is_ai_group_activity_enabled():
        return

    mentioned = bool(BOT_USERNAME) and f"@{BOT_USERNAME.lower()}" in message.text.lower()
    replied_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot.id
    )
    if not (mentioned or replied_to_bot):
        return

    now = time.monotonic()
    last = _group_reply_last.get(message.chat.id, 0.0)
    if now - last < _GROUP_REPLY_COOLDOWN_SEC:
        return
    _group_reply_last[message.chat.id] = now

    persona = await _build_ai_system()
    reply_text = await ai_providers.generate(prompt=message.text, system=persona)
    if reply_text:
        try:
            await message.reply(reply_text)
            await db.log_ai_event("group_reply_sent")
        except Exception as e:
            logger.error(f"Guruhda AI javob yuborishda xatolik: {e}")


# ==================================================================
#  JAVOBNI QABUL QILISH - FSM HOLATISIZ (muhim!)
#
#  MUHIM: bu handler eng OXIRGI @dp.message bo'lib ro'yxatga olinishi SHART.
#  Aiogram xabar handlerlarini RO'YXATGA OLISH TARTIBIDA tekshiradi va birinchi
#  mos kelgan filtrda to'xtaydi. Bu handlerning filtri (content_type) deyarli
#  har qanday matn/rasm/stiker/ovoz xabariga mos keladi - shuning uchun agar
#  u boshqa (masalan /admin buyrug'i yoki admin FSM holatlari uchun) handlerlardan
#  OLDIN turib qolsa, o'sha handlerlar HECH QACHON ishga tushmaydi (ular
#  "ko'rinmas" bo'lib qoladi). Shu sabab bu blok fayl OXIRIDA turishi kerak.
# ==================================================================

@dp.message(
    F.content_type.in_({
        ContentType.TEXT, ContentType.PHOTO, ContentType.STICKER,
        ContentType.VOICE, ContentType.AUDIO,
    })
)
async def receive_answer(message: Message):
    if message.text and (message.text in RESERVED_MENU_TEXTS or message.text.startswith("/")):
        return

    if not await db.is_registered(message.from_user.id):
        return  # ro'yxatdan o'tmagan - sukut (spam bo'lmasin)
    if not await db.is_tournament_active():
        return

    tournament_id = await db.get_active_tournament()
    rounds = get_rounds(tournament_id)
    round_num = await db.get_active_round()
    if not round_num or round_num not in rounds:
        return

    info = rounds[round_num]

    if info["kind"] == "external":
        return  # bot bu raundni umuman boshqarmaydi, javob yig'maydi

    if info["kind"] == "assoc":
        await handle_assoc_answer(message, tournament_id, round_num)
        return

    # Qolgan turlar ("photo", "sticker") - odatiy javob yig'ish oqimi.
    # ESLATMA: bu yerda oldindan has_answered() bilan tekshirib qo'ymaymiz -
    # buni pastda add_answer_if_new() BITTA atomik amalda (tekshirish+yozish)
    # bajaradi, shunda race condition (tez ketma-ket yuborilgan ikki xabar
    # ikkalasi ham "hali javob yo'q" deb o'tib ketishi) butunlay yo'q qilinadi.
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
    else:
        return

    answer_id = await db.add_answer_if_new(
        tg_id=message.from_user.id,
        round_num=round_num,
        tournament_id=tournament_id,
        content_type=content_type,
        text_content=text_content,
        file_id=file_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
    )

    if answer_id is None:
        # Boshqa (deyarli bir vaqtdagi) xabar allaqachon yozib ulgurgan
        await message.answer("⚠️ Siz bu raundda allaqachon ishtirok etgansiz, qayta yuborib bo'lmaydi.")
        return

    await message.answer(
        "✅ Javobingiz qabul qilindi va hakamlarga yuborildi!",
        reply_markup=await main_menu_kb(),
    )

    await notify_admins_new_answer(
        answer_id=answer_id, user=message.from_user, round_num=round_num,
        content_type=content_type, tournament_id=tournament_id,
    )


# ==================================================================
#  ADMIN: RO'YXATDAN O'TGANLAR RO'YXATI + ULARGA TO'G'RIDAN-TO'G'RI YOZISH
# ==================================================================

@dp.callback_query(F.data.startswith("adm_users"))
async def adm_users_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)

    parts = call.data.split("_")
    page = int(parts[-1]) if parts[-1].isdigit() else 0

    users = await db.get_all_users()
    per_page = 10
    total_pages = max(1, (len(users) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = users[page * per_page: (page + 1) * per_page]

    rows = []
    for u in chunk:
        uname = f" (@{u['username']})" if u["username"] and u["username"] != "yo'q" else ""
        rows.append([InlineKeyboardButton(
            text=f"👤 {u['full_name']}{uname}", callback_data=f"adm_msguser_{u['tg_id']}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_users_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_users_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_back")])

    if not users:
        text = "👥 Hozircha ro'yxatdan o'tgan foydalanuvchi yo'q."
    else:
        text = f"👥 <b>Ro'yxatdan o'tganlar</b> - jami {len(users)} kishi\n\nBirortasini bosib, unga to'g'ridan-to'g'ri xabar yozing:"

    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data == "noop")
async def noop_cb(call: CallbackQuery):
    await call.answer()


@dp.callback_query(F.data.startswith("adm_msguser_"))
async def adm_msguser_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    target_id = int(call.data.split("_")[-1])
    user = await db.get_user(target_id)
    if not user:
        await call.answer("Foydalanuvchi topilmadi.", show_alert=True)
        return

    await state.update_data(target_tg_id=target_id)
    await state.set_state(AdminMessageUserStates.waiting_message)
    await call.message.edit_text(
        f"✉️ <b>{html.escape(user['full_name'])}</b>ga yubormoqchi bo'lgan xabaringizni yozing:\n\n"
        "(Bekor qilish uchun /admin bosing)"
    )
    await call.answer()


@dp.callback_query(F.data == "adm_export")
async def adm_export(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    await call.answer("Excel tayyorlanmoqda...")
    await export_answers_to_excel(call.message)


@dp.callback_query(F.data == "adm_backup_now")
async def adm_backup_now(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔️", show_alert=True)
    if not config.DB_BACKUP_CHAT_ID:
        await call.answer(
            "⚠️ DB_BACKUP_CHAT_ID sozlanmagan (PERSONAL_CHAT_ID ham). "
            "Render'da shu environment variable'ni qo'shing.",
            show_alert=True,
        )
        return
    await call.answer("💾 Zaxira olinmoqda...")
    await backup_db_to_telegram()
    await call.message.answer("✅ Zaxira muvaffaqiyatli olindi va pin qilindi.")


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
        reply_markup=await admin_panel_kb(
            await db.is_tournament_active(), await db.get_active_tournament(), await db.get_active_round()
        ),
    )
    await call.answer("✅ Javoblar tarixi tozalandi.", show_alert=True)
    asyncio.create_task(backup_db_to_telegram())


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
        await admin_panel_text(), reply_markup=await admin_panel_kb(False, 0, 0)
    )
    await call.answer("✅ Barcha ma'lumotlar tozalandi.", show_alert=True)
    asyncio.create_task(backup_db_to_telegram())


async def export_answers_to_excel(message: Message):
    """Barcha javoblarni .xlsx faylga yig'ib, chatga yuboradi (AI baholovi ham
    kiritilgan - 'Rasmga oidlik' raundi uchun ball/to'g'ri/noto'g'ri so'zlar)."""
    from openpyxl import Workbook

    rows = await db.get_all_answers()

    wb = Workbook()
    ws = wb.active
    ws.title = "Javoblar"
    headers = [
        "ID", "Telegram ID", "Ism-Familiya", "Username", "Turnir", "Raund",
        "Turi", "Matn", "Rasm #", "AI ball", "AI to'g'ri", "AI noto'g'ri",
        "File ID", "WPM", "Vaqt (s)", "Yuborilgan vaqt",
    ]
    ws.append(headers)

    for r in rows:
        tournament_id = r["tournament_id"] or 100
        t_name = config.TOURNAMENTS.get(tournament_id, {}).get("name", tournament_id)
        round_name = get_rounds(tournament_id).get(r["round_num"], {}).get("name", r["round_num"])
        ws.append([
            r["id"], r["tg_id"], r["full_name"] or "", r["username"] or "", t_name,
            round_name, r["content_type"] or "", r["text_content"] or "",
            r["ref_num"], r["ai_score"], r["ai_correct"] or "", r["ai_incorrect"] or "",
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
#  DB BACKUP -> TELEGRAM (Render bepul tarifida disk vaqtinchalik bo'lgani uchun)
# ==================================================================

async def backup_db_to_telegram() -> None:
    """Joriy DB faylini DB_BACKUP_CHAT_ID'ga hujjat sifatida yuklaydi va PIN qiladi.
    Bot qayta ishga tushganda shu pin qilingan fayldan baza tiklanadi."""
    if not config.DB_BACKUP_CHAT_ID:
        return
    try:
        await db.checkpoint()  # WAL'dagi o'zgarishlarni asosiy faylga ko'chirish
        file = FSInputFile(db.DB_PATH, filename="zakovat_quiz_backup.db")
        msg = await bot.send_document(
            config.DB_BACKUP_CHAT_ID, file,
            caption=f"💾 Avtomatik zaxira - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            disable_notification=True,
        )
        try:
            await bot.unpin_all_chat_messages(config.DB_BACKUP_CHAT_ID)
        except Exception:
            pass  # pin qilingan xabar bo'lmasa ham davom etamiz
        await bot.pin_chat_message(config.DB_BACKUP_CHAT_ID, msg.message_id, disable_notification=True)
        logger.info("DB zaxirasi Telegram'ga yuklandi va pin qilindi.")
    except Exception as e:
        logger.error(f"DB zaxiralashda xatolik: {e}")


async def restore_db_from_telegram() -> None:
    """Bot ishga tushganda DB_BACKUP_CHAT_ID'dagi pin qilingan zaxira fayldan
    bazani tiklaydi (agar mavjud bo'lsa). Topilmasa - yangi (bo'sh) baza bilan davom etadi."""
    if not config.DB_BACKUP_CHAT_ID:
        logger.info("DB_BACKUP_CHAT_ID sozlanmagan - zaxiradan tiklash o'tkazib yuborildi.")
        return
    try:
        chat = await bot.get_chat(config.DB_BACKUP_CHAT_ID)
        pinned = chat.pinned_message
        if pinned and pinned.document:
            file_info = await bot.get_file(pinned.document.file_id)
            await bot.download_file(file_info.file_path, destination=db.DB_PATH)
            logger.info("✅ DB oldingi zaxiradan muvaffaqiyatli tiklandi.")
        else:
            logger.info("Pin qilingan zaxira topilmadi - yangi (bo'sh) baza bilan boshlanadi.")
    except Exception as e:
        logger.warning(f"DB'ni zaxiradan tiklashda xatolik (yangi baza bilan davom etiladi): {e}")


async def periodic_backup_loop() -> None:
    """Har 2 daqiqada avtomatik zaxira - javoblar oqimida hech narsa yo'qolmasligi uchun."""
    while True:
        await asyncio.sleep(120)
        await backup_db_to_telegram()


async def main():
    global BOT_USERNAME
    await restore_db_from_telegram()
    await db.init_db()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info(f"Bot ishga tushdi... (@{BOT_USERNAME})")
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
            periodic_backup_loop(),
            ai_scheduler_loop(),
        )
    finally:
        await backup_db_to_telegram()  # to'xtashdan oldin ham oxirgi holatni saqlab qolamiz
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
