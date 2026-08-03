# Zakovat Quiz Bot

Ikkita turnirni boshqaradigan Telegram bot: **Turnir 100** (avvalgi musobaqa,
shartlari o'zgarishsiz) va **Turnir 300** (yangi, kengaytirilgan musobaqa —
shu jumladan "Rasmga oidlik" raundi, Groq AI yordamida avtomatik baholanadi).

## O'rnatish

```bash
pip install -r requirements.txt
```

## Sozlash (environment variables yoki `config.py`)

1. `BOT_TOKEN` — BotFather'dan olingan token.
2. `CHANNEL_USERNAME` / `CHANNEL_ID` — majburiy obuna kanali (`@ParadoksHub`). Bot ushbu kanalda **admin** bo'lishi shart, aks holda `get_chat_member` ishlamaydi.
3. `ADMIN_GROUP_ID` — javoblar tushadigan guruh/kanal ID (`-100...`). Botni shu guruhga admin qilib qo'shing.
4. `GAME_CHAT_ID` — (ixtiyoriy) Dart/Futbol/Basketbol/Bowling Challenge roundlari e'lon qilinadigan chat (kanalning muhokama guruhi). Berilmasa `ADMIN_GROUP_ID` ishlatiladi.
5. `ADMIN_IDS` — admin panelidan foydalana oladigan Telegram ID'lar, vergul bilan (masalan `111,222`).
6. `PERSONAL_CHAT_ID` — (ixtiyoriy) barcha xabarnomalarning nusxasi tushadigan shaxsiy chat.
7. `DB_BACKUP_CHAT_ID` — DB zaxira fayli PIN qilinadigan chat (berilmasa `PERSONAL_CHAT_ID`).
8. `GROQ_API_KEYS` — "Rasmga oidlik" raundini AI orqali avtomatik baholash uchun Groq API kalitlari, vergul bilan bir nechtasi kiritilishi mumkin (biri limitga tushsa, keyingisiga o'tiladi). Masalan: `gsk_xxx,gsk_yyy`.

## Ishga tushirish

```bash
python bot.py
```

Birinchi ishga tushganda `zakovat_quiz.db` fayli avtomatik yaratiladi.

## Turnirlar va raundlar

Admin `/admin` orqali avval **qaysi turnirni** (Turnir 100 / Turnir 300)
boshlashni tanlaydi, so'ng shu turnirning raundlarini birma-bir faollashtiradi.
Barcha raundlar tuzilmasi `config.py` ichidagi `TOURNAMENTS` lug'atida.

Raund turlari (`kind`):
- `photo` — oddiy javob (rasm/matn/stiker/ovoz) botga to'g'ridan-to'g'ri yuboriladi.
- `sticker` — Sticker Battle: admin stiker+tarif yuboradi, foydalanuvchilar stiker bilan javob beradi.
- `assoc` — **Rasmga oidlik**: admin bir nechta rasm yuboradi va ✅ Tayyor bosadi, bot ularni raqamlab kanalga e'lon qiladi. Foydalanuvchilar `"<rasm raqami>: so'z1, so'z2, ..."` formatida javob yuboradi, Groq AI rasm bilan solishtirib har bir so'zni baholaydi (natija FAQAT admin guruhiga yuboriladi, foydalanuvchiga ko'rsatilmaydi).
- `external` — bot bu raundni boshqarmaydi (kanal/guruh muhokamasi yoki boshqa bot orqali o'tkaziladi), faqat e'lon qiladi.

## Render'ga deploy qilish

- `bot.py`ni Background Worker (yoki Web Service, health-check server ichida mavjud) sifatida deploy qiling.
- Yuqoridagi barcha environment variables'ni Render Dashboard'da qo'shing.

## Admin buyruqlari

- `/admin` — Admin panelni ochadi: turnirni tanlash/boshlash/to'xtatish, raundlarni yoqish, natijalarni Excel qilib yuklab olish (AI baholovi bilan birga).
