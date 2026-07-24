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

# "Tez yozish" Web App joylashgan manzil (Render/GitHub Pages/Netlify'ga statik host qilinadi)
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://sizning-domeningiz.com/web_app/index.html")

# Turnirdagi barcha raundlar
ROUNDS = {
    1: {"name": "🖼 Rasmni ta'riflash", "hint": "Rasm yoki Rasm + Ta'rif (matn) yuboring"},
    2: {"name": "🎨 Mavzuga rasm",       "hint": "Faqat Rasm/Fayl yuboring"},
    3: {"name": "🏆 Rasm Battle",        "hint": "Faqat Rasm yuboring"},
    4: {"name": "⌨️ Tez yozish (Web App)", "hint": "Bosh menyudagi 'Tez yozish' tugmasidan foydalaning"},
    5: {"name": "😂 Sticker Battle",     "hint": "Stiker yuboring"},
    6: {"name": "⚽ Futbol Sticker Quiz", "hint": "Stiker yoki matn yuboring"},
    7: {"name": "🎵 Musiqani top",       "hint": "Matn yoki Ovozli xabar yuboring"},
}
