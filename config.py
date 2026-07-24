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
