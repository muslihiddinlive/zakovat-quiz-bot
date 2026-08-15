# ai_providers.py
# ------------------------------------------------------------
# MULTI-PROVIDER AI QATLAMI
#
# Bir nechta AI provayder (Groq, OpenAI, Gemini) va har birida bir nechta
# API kalit bo'lishi mumkin - hammasi /admin > "🤖 AI provayderlar"
# orqali DB'ga qo'shiladi (config.py'ga qo'lda yozish shart emas).
#
# generate() chaqirilganda, DB'dagi barcha YOQILGAN kalitlar ketma-ket
# sinab ko'riladi (avval bitta provayderning ichidagi kalitlari, keyin
# navbatdagi provayderga o'tiladi) - biri limitga tushsa yoki xato bersa,
# navbatdagisiga avtomatik o'tiladi. Bu "Rasmga oidlik" (vision) raundida
# ham, kanalga avtomatik post/izoh yozishda (matn) ham xuddi shu funksiya
# orqali ishlatiladi.
#
# ESLATMA (xavfsizlik): rasm argumenti har doim base64 data-URL sifatida
# kutiladi ("data:image/jpeg;base64,..."), Telegram file URL emas - bot
# tokeni uchinchi tomon serveriga hech qachon ketmasligi uchun.
# ------------------------------------------------------------
import logging
from typing import Optional

import aiohttp

import config
import database as db

logger = logging.getLogger(__name__)

# Har bir provayder uchun: API manzili, matn/vision modeli va so'rov
# formati ("style"): "openai" - OpenAI-mos /chat/completions formati
# (Groq va OpenAI ikkisi ham shu formatda ishlaydi), "gemini" - Google'ning
# o'ziga xos generateContent formati.
PROVIDERS: dict[str, dict] = {
    "groq": {
        "label": "Groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "text_model": "llama-3.1-8b-instant",
        "vision_model": "llama-3.2-11b-vision-preview",
        "style": "openai",
    },
    "openai": {
        "label": "OpenAI",
        "url": "https://api.openai.com/v1/chat/completions",
        "text_model": "gpt-4o-mini",
        "vision_model": "gpt-4o-mini",
        "style": "openai",
    },
    "gemini": {
        "label": "Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "text_model": "gemini-1.5-flash",
        "vision_model": "gemini-1.5-flash",
        "style": "gemini",
    },
}


def mask_key(key: str) -> str:
    """Admin panelda kalitni to'liq ko'rsatmaslik uchun (masalan sk-...ab12)."""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


async def _get_active_keys() -> list[dict]:
    """DB'dagi barcha YOQILGAN kalitlarni qaytaradi. Agar DB'da groq kaliti
    umuman bo'lmasa (masalan eski deploy hali yangilanmagan), config.py
    ichidagi GROQ_API_KEYS ga fallback qilinadi - eski o'rnatishlar
    buzilib qolmasligi uchun."""
    rows = await db.get_enabled_ai_keys()
    keys = [
        {"id": r["id"], "provider": r["provider"], "api_key": r["api_key"], "label": r["label"]}
        for r in rows
    ]
    if not any(k["provider"] == "groq" for k in keys) and getattr(config, "GROQ_API_KEYS", None):
        for i, k in enumerate(config.GROQ_API_KEYS):
            keys.append({"id": f"legacy_{i}", "provider": "groq", "api_key": k, "label": "legacy (config.py)"})
    return keys


async def has_any_key() -> bool:
    keys = await _get_active_keys()
    return len(keys) > 0


async def _call_openai_style(url: str, model: str, api_key: str, messages: list, json_mode: bool) -> Optional[str]:
    body = {"model": model, "messages": messages, "temperature": 0.4, "max_tokens": 800}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=aiohttp.ClientTimeout(total=40),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
                err_text = await resp.text()
                logger.warning(f"AI so'rovida xatolik ({resp.status}): {err_text[:300]}")
    except Exception as e:
        logger.warning(f"AI so'rovida tarmoq xatoligi: {e}")
    return None


async def _call_gemini(
    url_template: str, model: str, api_key: str, prompt: str, system: Optional[str],
    image_data_url: Optional[str], json_mode: bool,
) -> Optional[str]:
    url = url_template.format(model=model) + f"?key={api_key}"
    parts: list[dict] = []
    if prompt:
        parts.append({"text": prompt})
    if image_data_url:
        try:
            header, b64data = image_data_url.split(",", 1)
            mime = header.split(":")[1].split(";")[0]
        except Exception:
            mime, b64data = "image/jpeg", image_data_url
        parts.append({"inline_data": {"mime_type": mime, "data": b64data}})

    body: dict = {"contents": [{"role": "user", "parts": parts}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"] = {"response_mime_type": "application/json"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=40)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                err_text = await resp.text()
                logger.warning(f"Gemini xatolik ({resp.status}): {err_text[:300]}")
    except Exception as e:
        logger.warning(f"Gemini so'rovida tarmoq xatoligi: {e}")
    return None


async def generate(
    prompt: str,
    system: Optional[str] = None,
    image_data_url: Optional[str] = None,
    json_mode: bool = False,
) -> Optional[str]:
    """Barcha yoqilgan provayder/kalitlarni ketma-ket sinab ko'radi.
    Rasm berilsa vision modeli, bo'lmasa oddiy matn modeli ishlatiladi.
    Birinchi muvaffaqiyatli javobni qaytaradi; hech biri ishlamasa None."""
    keys = await _get_active_keys()
    if not keys:
        logger.warning("Hech qanday AI kaliti sozlanmagan (/admin > 🤖 AI provayderlar).")
        return None

    for k in keys:
        cfg = PROVIDERS.get(k["provider"])
        if not cfg:
            continue
        model = cfg["vision_model"] if image_data_url else cfg["text_model"]

        if cfg["style"] == "openai":
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            if image_data_url:
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                })
            else:
                messages.append({"role": "user", "content": prompt})
            result = await _call_openai_style(cfg["url"], model, k["api_key"], messages, json_mode)
        else:  # gemini
            result = await _call_gemini(cfg["url"], model, k["api_key"], prompt, system, image_data_url, json_mode)

        if result is not None:
            return result
        logger.info(f"{cfg['label']} kaliti (id={k['id']}) ishlamadi, keyingi kalit/provayderga o'tilmoqda...")

    logger.error("Barcha AI kalit/provayderlar urinishlari muvaffaqiyatsiz tugadi.")
    return None
