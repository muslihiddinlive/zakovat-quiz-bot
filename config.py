# config.py
# ------------------------------------------------------------
# Loyihaning barcha sozlamalari shu yerda saqlanadi.
# BOT_TOKEN, ADMIN_IDS va ADMIN_GROUP_ID ni albatta o'zingiznikiga almashtiring!
# ------------------------------------------------------------
import os

# BotFather'dan olingan token (Render'da Environment Variable sifatida qo'yish tavsiya etiladi)
BOT_TOKEN = os.getenv("BOT_TOKEN", "BU_YERGA_TOKEN_QOYING")

# Majburiy obuna bo'lish shart bo'lgan kanal
CHANNEL_USERNAME = "@ParadoksHub"      # Foydalanuvchiga ko'rsatish uchun
CHANNEL_ID = "@ParadoksHub"            # get_chat_member() uchun (agar kanal ID orqali bo'lsa: -100xxxxxxxxxx)

# Qatnashchilarning javoblari, natijalari shu guruh/kanalga (yoki topic'ga) tushadi
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "-1000000000000"))

# Dart/Futbol/Basketbol/Bowling Challenge roundlari o'tkaziladigan chat
# ("kanalning direkti" - kanalga bog'langan muhokama/discussion guruhi).
# Alohida berilmasa, ADMIN_GROUP_ID bilan bir xil chat ishlatiladi.
_game_chat_env = os.getenv("GAME_CHAT_ID", "")
GAME_CHAT_ID = int(_game_chat_env) if _game_chat_env.strip() else ADMIN_GROUP_ID

# (Ixtiyoriy) Shaxsiy chat ID - agar berilsa, barcha javoblar ADMIN_GROUP_ID'dan
# tashqari shu shaxsiy chat'ga (DM) ham qo'shimcha yuboriladi.
# Bot bilan avval shaxsiy suhbatni /start bilan boshlab qo'yish kerak,
# aks holda Telegram DM yuborishga ruxsat bermaydi.
_personal_chat_id_env = os.getenv("PERSONAL_CHAT_ID", "")
PERSONAL_CHAT_ID = int(_personal_chat_id_env) if _personal_chat_id_env.strip() else None

# DB fayli shu chatga vaqti-vaqti bilan hujjat sifatida yuklanib, PIN qilinadi.
# Render'ning bepul tarifida disk vaqtinchalik (har deploy'da o'chadi),
# shuning uchun bot ishga tushganda shu yerdan (pin qilingan hujjatdan)
# bazani avtomatik tiklaydi. Alohida berilmasa, PERSONAL_CHAT_ID ishlatiladi.
_db_backup_chat_env = os.getenv("DB_BACKUP_CHAT_ID", "")
if _db_backup_chat_env.strip():
    DB_BACKUP_CHAT_ID = int(_db_backup_chat_env)
else:
    DB_BACKUP_CHAT_ID = PERSONAL_CHAT_ID

# Admin panelidan foydalana oladigan Telegram ID'lar ro'yxati.
# Deploy qilishda ADMIN_IDS environment variable orqali (vergul bilan
# ajratilgan holda, masalan "111111,222222") berish tavsiya etiladi,
# shaxsiy ID'larni kodda saqlab qo'ymaslik uchun.
_admin_ids_env = os.getenv("ADMIN_IDS", "")
if _admin_ids_env:
    ADMIN_IDS = [int(x.strip()) for x in _admin_ids_env.split(",") if x.strip()]
else:
    ADMIN_IDS = [
        5302627260,  # <-- shu yerga o'z ID'ingizni va boshqa adminlarni qo'shing
    ]

# ------------------------------------------------------------
# GROQ AI (Rasmga oidlik raundini avtomatik baholash uchun)
# Bir nechta kalit kiritish mumkin (vergul bilan ajratib) - biri limitga
# tushib qolsa, bot avtomatik keyingisiga o'tadi.
# Masalan Render env: GROQ_API_KEYS = gsk_xxx,gsk_yyy,gsk_zzz
# ------------------------------------------------------------
_groq_keys_env = os.getenv("GROQ_API_KEYS", os.getenv("GROQ_API_KEY", ""))
GROQ_API_KEYS = [k.strip() for k in _groq_keys_env.split(",") if k.strip()]
GROQ_MODEL = "qwen/qwen3.6-27b"  # Groq'dagi joriy vision (rasm+matn) modeli
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# ------------------------------------------------------------
# TURNIRLAR
# ------------------------------------------------------------
# Har bir turnir o'z raundlar to'plamiga ega. Raund raqamlari faqat
# O'Z TURNIRI ichida ma'noga ega (masalan Turnir 100'dagi 3-raund bilan
# Turnir 300'dagi 3-raund - ikki xil narsa). Shu sabab ma'lumotlar
# bazasida har bir javob tournament_id bilan birga saqlanadi.
#
# "kind" maydoni botning har raundga qanday munosabatda bo'lishini belgilaydi:
#   "photo"     - oddiy javob oqimi (rasm/matn/stiker/ovoz - botda javob yig'iladi)
#   "sticker"   - Sticker Battle: raund boshlanganda admin stiker+tarif yuboradi,
#                 bot uni kanalga e'lon qiladi; foydalanuvchilar botga stiker yuboradi
#   "assoc"     - Rasmga oidlik: admin bir nechta rasm yuboradi va "Tayyor" bosadi,
#                 bot ularni RAQAMLAB kanalga e'lon qiladi; userlar botga
#                 "<raqam>: so'z1, so'z2, ..." formatida javob yuboradi, AI (Groq)
#                 rasm bilan solishtirib har bir so'zni to'g'ri/noto'g'ri deb
#                 baholaydi (foydalanuvchiga ko'rsatilmaydi - faqat adminga)
#   "external"  - bot bu raundni UMUMAN boshqarmaydi (boshqa bot, guruh
#                 muhokamasi yoki kanalning direkt/muhokama guruhida o'tkaziladi,
#                 admin ballarni qo'lda kiritadi/hisoblaydi). Bot faqat
#                 kanalga/foydalanuvchilarga e'lon qiladi, javob yig'maydi.
# ------------------------------------------------------------

TOURNAMENTS: dict[int, dict] = {
    # ------------------------------------------------------------------
    # TURNIR 100 - avvalgi musobaqa, shartlari o'zgarishsiz qoladi.
    # (Eski "Tez yozish - Web App" raundi butunlay olib tashlandi, shu
    # sabab 5-raqam ataylab bo'sh qoldirilgan - eski javoblar tarixidagi
    # raqamlar buzilib qolmasligi uchun qolgan raundlar qayta raqamlanmadi.)
    # ------------------------------------------------------------------
    100: {
        "name": "Turnir 100",
        "rounds": {
            1: {"name": "🖼 Rasmni ta'riflash", "hint": "Rasm yoki Rasm + Ta'rif (matn) yuboring", "kind": "photo"},
            2: {"name": "🎨 Mavzuga rasm", "hint": "Faqat Rasm/Fayl yuboring", "kind": "photo"},
            3: {"name": "🏆 Rasm Battle", "hint": "Faqat Rasm yuboring", "kind": "photo"},
            4: {"name": "🧮 Matematika Rush", "hint": "Bu raund kanal muhokamasida BOSHQA BOT orqali o'tkaziladi. Botda javob yuborish shart emas.", "kind": "external"},
            6: {"name": "😂 Sticker Battle", "hint": "Stiker yuboring", "kind": "sticker"},
            7: {"name": "⚽ Futbol Sticker Quiz", "hint": "Bu raund GURUHDA o'tkaziladi, admin ballarni qo'lda hisoblaydi. Botda javob yuborish shart emas.", "kind": "external"},
            8: {"name": "🎵 Musiqani top", "hint": "Musiqa/audio faylini yuboring", "kind": "photo"},
        },
    },
    # ------------------------------------------------------------------
    # TURNIR 300 - yangi musobaqa, kengaytirilgan raundlar bilan.
    # ------------------------------------------------------------------
    300: {
        "name": "Turnir 300",
        "rounds": {
            1: {
                "name": "🏛 Zakovat Battle",
                "hint": (
                    "3-5 kishilik jamoalarda maslahatlashib, savollarga to'g'ri javob toping. "
                    "Hamjihatlik va tezkor fikrlash sizni g'alabaga olib boradi!\n"
                    "Bu raundni bot boshqarmaydi - admin uni alohida tashkil qiladi, "
                    "bot faqat boshlanganini e'lon qiladi."
                ),
                "kind": "external",
            },
            2: {
                "name": "🖼 Rasmga oidlik",
                "hint": (
                    "Bot rasm yuboradi. Siz esa ushbu rasmga oid imkon qadar ko'proq bog'liq "
                    "so'z, shaxs, kompaniya, brend, mahsulot, joy, voqea yoki tushunchalarni "
                    "topishingiz kerak. Eng ko'p va eng to'g'ri assotsiatsiyalarni topgan "
                    "ishtirokchi g'olib bo'ladi!\n"
                    "Misol: Instagram logosi → Instagram, Meta, Mark Zuckerberg, Facebook, "
                    "Reels, Stories, Threads, hashtag, DM va hokazo.\n\n"
                    "Javobingizni: <b>\"&lt;rasm raqami&gt;: so'z1, so'z2, ...\"</b> shaklida yuboring "
                    "(masalan: <code>3: Instagram, Meta, Zuckerberg</code>). "
                    "Keyingi raund boshlanmaguncha istagancha marta javob yuborishingiz mumkin."
                ),
                "kind": "assoc",
            },
            3: {"name": "🎨 Mavzuga rasm", "hint": "Berilgan mavzu asosida rasm chizing.", "kind": "photo"},
            4: {"name": "🏆 Rasm Battle", "hint": "Eng kreativ va chiroyli rasmni chizing!", "kind": "photo"},
            5: {
                "name": "🌍 Bayroq Challenge",
                "hint": (
                    "Bayroq, xarita, poytaxt yoki mashhur ramzlarga qarab davlatni toping! "
                    "Bu raund boshqa yo'l bilan (kanal/guruh muhokamasida) o'tkaziladi."
                ),
                "kind": "external",
            },
            6: {
                "name": "🧮 Matematika Rush",
                "hint": "Guruh bo'lib tezkor matematik misollarni yeching. Bu raund kanal muhokamasida boshqa bot orqali o'tkaziladi.",
                "kind": "external",
            },
            7: {
                "name": "🎯 Dart Challenge",
                "hint": "Telegramning 🎯 dart o'yinida eng yaxshi natijani qayd eting. Bu raund muhokama guruhida o'tkaziladi.",
                "kind": "external",
                "extra_target": "game_chat",
            },
            8: {
                "name": "⚽ Futbol Challenge",
                "hint": "Telegramning ⚽ futbol o'yinida eng yaxshi natijani qo'lga kiriting. Bu raund muhokama guruhida o'tkaziladi.",
                "kind": "external",
                "extra_target": "game_chat",
            },
            9: {
                "name": "🏀 Basketbol Challenge",
                "hint": "🏀 To'pni savatga tushiring va eng yuqori natijani qayd eting. Bu raund muhokama guruhida o'tkaziladi.",
                "kind": "external",
                "extra_target": "game_chat",
            },
            10: {
                "name": "🎳 Bowling Challenge",
                "hint": "🎳 Strike qiling va eng yaxshi natija uchun bellashing. Bu raund muhokama guruhida o'tkaziladi.",
                "kind": "external",
                "extra_target": "game_chat",
            },
            11: {"name": "🎵 Musiqani top", "hint": "Berilgan mavzu yoki qisqa parchaga qarab qo'shiqni toping.", "kind": "photo"},
        },
    },
}
