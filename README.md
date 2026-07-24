# Zakovat Quiz Bot

## O'rnatish

```bash
pip install -r requirements.txt
```

## Sozlash (`config.py`)

1. `BOT_TOKEN` — BotFather'dan olingan token.
2. `CHANNEL_USERNAME` / `CHANNEL_ID` — majburiy obuna kanali (`@ParadoksHub`). Bot ushbu kanalda **admin** bo'lishi shart, aks holda `get_chat_member` ishlamaydi.
3. `ADMIN_GROUP_ID` — javoblar tushadigan guruh/kanal ID (`-100...`). Botni shu guruhga admin qilib qo'shing.
4. `ADMIN_IDS` — admin panelidan foydalana oladigan Telegram ID'lar.
5. `WEBAPP_URL` — `web_app/index.html` joylashgan HTTPS manzil (Render static site, GitHub Pages yoki Netlify'ga yuklang — Telegram WebApp faqat HTTPS bilan ishlaydi).

## Ishga tushirish

```bash
python bot.py
```

Birinchi ishga tushganda `zakovat_quiz.db` fayli avtomatik yaratiladi.

## Render'ga deploy qilish

- `bot.py`ni Background Worker sifatida deploy qiling.
- `web_app/index.html`ni alohida Static Site sifatida deploy qiling va shu manzilni `WEBAPP_URL`ga yozing.
- `BOT_TOKEN`, `ADMIN_GROUP_ID`, `WEBAPP_URL` — Environment Variables orqali beriladi.

## Admin buyruqlari

- `/admin` — Admin panelni ochadi: turnirni boshlash/to'xtatish, raundlarni yoqish/o'chirish, natijalarni Excel qilib yuklab olish.
