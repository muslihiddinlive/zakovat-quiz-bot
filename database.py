# database.py
# ------------------------------------------------------------
# SQLite (aiosqlite) yordamida asinxron ma'lumotlar bazasi qatlami.
# Jadvallar: users, answers, settings, assoc_images
#
# Eslatma: har bir so'rov uchun yangi ulanish ochish o'rniga (bu ko'p
# yozuv bo'lganda "database is locked" xatosiga olib kelishi mumkin edi),
# butun dastur davomida BITTA umumiy ulanish saqlanadi va asyncio.Lock
# bilan himoyalanadi.
#
# TURNIR TIZIMI: bitta bot ichida bir nechta turnir (masalan "Turnir 100"
# va "Turnir 300") bo'lishi mumkin. Shu sabab "answers" jadvaliga
# tournament_id ustuni qo'shilgan - xuddi shu raund raqami turli
# turnirlarda boshqa-boshqa narsani anglatishi mumkinligi uchun.
# ------------------------------------------------------------
import asyncio
import aiosqlite
from datetime import datetime

DB_PATH = "zakovat_quiz.db"

_conn: aiosqlite.Connection | None = None
_lock = asyncio.Lock()

DEFAULT_AI_PERSONA = (
    "Sen 'Zakovat Quiz' turnirining guruh/kanalidagi do'stona AI yordamchisisan. "
    "Ishtirokchilar bilan samimiy, hazilkash, lekin hurmatli ohangda, o'zbek tilida "
    "gaplash. Javoblaring qisqa (1-3 gap) va tabiiy bo'lsin, o'rinli joyda emoji ishlat."
)


async def _add_column_if_missing(table: str, column: str, coltype: str) -> None:
    """Eski bazalarni yangi ustunlar bilan xavfsiz (ma'lumot yo'qotmasdan)
    yangilash uchun - ustun allaqachon bo'lsa, xatolikni jim yutadi."""
    try:
        await _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except Exception:
        pass  # ustun allaqachon mavjud


async def init_db() -> None:
    """Umumiy ulanishni ochadi va jadvallarni yaratadi (agar mavjud bo'lmasa)."""
    global _conn
    _conn = await aiosqlite.connect(DB_PATH)
    _conn.row_factory = aiosqlite.Row
    # SQLite'da bir nechta o'quvchi + bitta yozuvchi bir vaqtda ishlashi uchun
    await _conn.execute("PRAGMA journal_mode=WAL;")

    async with _lock:
        await _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id        INTEGER UNIQUE NOT NULL,
                full_name    TEXT,
                username     TEXT,
                registered_at TEXT
            )
            """
        )
        await _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS answers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id         INTEGER NOT NULL,
                round_num     INTEGER NOT NULL,
                content_type  TEXT,       -- text / photo / sticker / voice / audio / photo_text / assoc
                text_content  TEXT,
                file_id       TEXT,
                chat_id       INTEGER,    -- original message manzili (admin "Ko'rish" bosganda shu joydan nusxa oladi)
                message_id    INTEGER,
                wpm           REAL,
                time_sec      REAL,
                submitted_at  TEXT
            )
            """
        )
        await _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assoc_images (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                round_num     INTEGER NOT NULL,
                image_number  INTEGER NOT NULL,
                file_id       TEXT NOT NULL,
                created_at    TEXT,
                UNIQUE(tournament_id, round_num, image_number)
            )
            """
        )
        # AI provayder kalitlari (Groq/OpenAI/Gemini) - endi Render env
        # o'rniga to'g'ridan-to'g'ri /admin panelidan qo'shiladi/o'chiriladi,
        # botni qayta ishga tushirmasdan darhol kuchga kiradi.
        await _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_keys (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                provider   TEXT NOT NULL,      -- 'groq' / 'openai' / 'gemini'
                api_key    TEXT NOT NULL,
                label      TEXT,
                enabled    INTEGER DEFAULT 1,
                added_at   TEXT
            )
            """
        )
        # AI avtomatik faoliyat qoidalari (2 va 3-qism: rejalashtirilgan
        # postlar + guruh/kanal faolligi). Har bir qoida JSON konfiguratsiya
        # sifatida saqlanadi - moslashuvchan tuzilma, yangi maydon qo'shish
        # uchun migratsiya kerak bo'lmaydi.
        await _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_rules (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_type  TEXT NOT NULL,      -- 'scheduled_post' / 'group_activity' / 'auto_comment'
                config     TEXT NOT NULL,      -- JSON
                enabled    INTEGER DEFAULT 1,
                created_at TEXT,
                last_run_at TEXT
            )
            """
        )

        # ----- Eski bazalarni yangi ustunlar bilan to'ldirish (migratsiya) -----
        await _add_column_if_missing("answers", "tournament_id", "INTEGER")
        await _add_column_if_missing("answers", "ref_num", "INTEGER")       # masalan: Rasmga oidlik raundida rasm raqami
        await _add_column_if_missing("answers", "ai_score", "REAL")
        await _add_column_if_missing("answers", "ai_correct", "TEXT")       # JSON ro'yxat
        await _add_column_if_missing("answers", "ai_incorrect", "TEXT")     # JSON ro'yxat

        # XAVFSIZLIK/RACE CONDITION HIMOYASI: "has_answered() tekshirish, keyin
        # add_answer() yozish" ikki alohida bosqich bo'lgani uchun, foydalanuvchi
        # bir vaqtda bir nechta xabar yuborsa (tez ketma-ket yoki parallel),
        # ikkalasi ham tekshiruvdan o'tib, bitta raundga bir necha marta javob
        # yozilib qolishi mumkin edi. Bu partial UNIQUE index shu holatning
        # OLDINI DB DARAJASIDA oladi (assoc raundlar bundan mustasno - u yerda
        # ataylab bir necha marta javob yuborish ruxsat etilgan).
        try:
            await _conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_answers_one_per_round "
                "ON answers(tg_id, tournament_id, round_num) WHERE content_type != 'assoc'"
            )
        except Exception as e:
            pass  # eski SQLite versiyalarida partial index bo'lmasligi mumkin - jim o'tkazamiz

        # Eski (migratsiyadan oldingi) javoblar "Turnir 100" ga tegishli edi
        await _conn.execute("UPDATE answers SET tournament_id = 100 WHERE tournament_id IS NULL")

        # Boshlang'ich sozlamalar (faqat birinchi marta yaratiladi)
        await _conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('tournament_active', '0')"
        )
        # active_round: 0 = hech qanday raund faol emas. Bir vaqtning
        # o'zida faqat BITTA raund faol bo'ladi - admin yangi raund
        # boshlasa, eskisi avtomatik yopiladi.
        await _conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('active_round', '0')"
        )
        # active_tournament: 0 = hech qanday turnir tanlanmagan. 100 yoki 300
        # bo'lishi mumkin (config.TOURNAMENTS'dagi kalitlar).
        await _conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('active_tournament', '0')"
        )
        # test_mode: 1 = yoniq. Yoniq bo'lsa kanalga/userlarga HAQIQIY xabar
        # ketmaydi, faqat admin(lar)ga simulyatsiya ko'rinishida yuboriladi.
        await _conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('test_mode', '0')"
        )
        # ai_group_chat_id: AI guruh faolligi (avtomatik izoh, mavzu ochish,
        # foydalanuvchilarga javob berish) ishlaydigan guruh/muhokama chat ID.
        # 0 = sozlanmagan.
        await _conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_group_chat_id', '0')"
        )
        # ai_auto_comment_enabled: 1 bo'lsa, kanalga yangi post/raund
        # e'lon qilinganda AI avtomatik ravishda guruhga qisqa izoh yozadi.
        await _conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_auto_comment_enabled', '0')"
        )
        # ai_group_activity_enabled: 1 bo'lsa, AI foydalanuvchilarning
        # xabarlariga (mention/reply qilinganda) guruhda javob qaytaradi.
        await _conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_group_activity_enabled', '0')"
        )
        # ai_persona: AI'ning guruh/kanaldagi "xarakteri" - barcha avtomatik
        # generatsiyalarda system prompt sifatida ishlatiladi.
        await _conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_persona', ?)",
            (DEFAULT_AI_PERSONA,),
        )
        await _conn.commit()


async def close_db() -> None:
    """Dastur to'xtaganda ulanishni yopish uchun (ixtiyoriy, lekin toza yopilish uchun tavsiya etiladi)."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def checkpoint() -> None:
    """WAL rejimida yozilgan o'zgarishlarni asosiy .db fayliga to'liq ko'chiradi.
    Backup olishdan OLDIN albatta chaqirilishi kerak, aks holda faylda
    eng so'nggi yozuvlar bo'lmasligi mumkin."""
    async with _lock:
        await _conn.execute("PRAGMA wal_checkpoint(FULL);")


# ------------------------- USERS -------------------------

async def get_user(tg_id: int):
    async with _lock:
        cur = await _conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        return await cur.fetchone()


async def is_registered(tg_id: int) -> bool:
    user = await get_user(tg_id)
    return user is not None


async def add_user(tg_id: int, full_name: str, username: str | None) -> None:
    async with _lock:
        await _conn.execute(
            "INSERT OR IGNORE INTO users (tg_id, full_name, username, registered_at) VALUES (?, ?, ?, ?)",
            (tg_id, full_name, username or "yo'q", datetime.now().isoformat(timespec="seconds")),
        )
        await _conn.commit()


# ------------------------- ANSWERS -------------------------

async def add_answer(
    tg_id: int,
    round_num: int,
    content_type: str,
    tournament_id: int | None = None,
    text_content: str | None = None,
    file_id: str | None = None,
    wpm: float | None = None,
    time_sec: float | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
    ref_num: int | None = None,
) -> int:
    """Javobni bazaga yozadi va yangi qatorning ID'sini qaytaradi."""
    async with _lock:
        cur = await _conn.execute(
            """
            INSERT INTO answers
                (tg_id, round_num, content_type, text_content, file_id, chat_id, message_id,
                 wpm, time_sec, submitted_at, tournament_id, ref_num)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tg_id, round_num, content_type, text_content, file_id, chat_id, message_id, wpm, time_sec,
                datetime.now().isoformat(timespec="seconds"), tournament_id, ref_num,
            ),
        )
        await _conn.commit()
        return cur.lastrowid


async def add_answer_if_new(
    tg_id: int,
    round_num: int,
    content_type: str,
    tournament_id: int | None = None,
    text_content: str | None = None,
    file_id: str | None = None,
    wpm: float | None = None,
    time_sec: float | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
    ref_num: int | None = None,
) -> int | None:
    """add_answer bilan bir xil, lekin ATOMIK: agar bu foydalanuvchi shu
    turnir+raundga allaqachon javob yozgan bo'lsa (idx_answers_one_per_round
    UNIQUE indexi orqali), hech narsa yozmay None qaytaradi. has_answered()
    tekshiruvi + alohida add_answer() chaqiruvi orasida boshqa xabar
    "kirib qolishi" (race condition) MUMKIN EMAS, chunki bu yerda hammasi
    bitta lock ostida, bitta INSERT sifatida bajariladi."""
    async with _lock:
        try:
            cur = await _conn.execute(
                """
                INSERT INTO answers
                    (tg_id, round_num, content_type, text_content, file_id, chat_id, message_id,
                     wpm, time_sec, submitted_at, tournament_id, ref_num)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tg_id, round_num, content_type, text_content, file_id, chat_id, message_id, wpm, time_sec,
                    datetime.now().isoformat(timespec="seconds"), tournament_id, ref_num,
                ),
            )
            await _conn.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            # idx_answers_one_per_round'ga urildi - demak bu javob allaqachon mavjud
            return None


async def update_answer_ai_result(answer_id: int, ai_score: float, ai_correct_json: str, ai_incorrect_json: str) -> None:
    """AI (Groq) 'Rasmga oidlik' javobini baholagach, natijani yozib qo'yadi.
    Foydalanuvchiga HECH QACHON ko'rsatilmaydi - faqat admin guruhiga/eksportga."""
    async with _lock:
        await _conn.execute(
            "UPDATE answers SET ai_score = ?, ai_correct = ?, ai_incorrect = ? WHERE id = ?",
            (ai_score, ai_correct_json, ai_incorrect_json, answer_id),
        )
        await _conn.commit()


async def get_answer_by_id(answer_id: int):
    async with _lock:
        cur = await _conn.execute(
            """
            SELECT a.*, u.full_name, u.username
            FROM answers a LEFT JOIN users u ON u.tg_id = a.tg_id
            WHERE a.id = ?
            """,
            (answer_id,),
        )
        return await cur.fetchone()


async def get_all_user_ids() -> list[int]:
    """Broadcast (masalan, yangi raund boshlanganini xabar qilish) uchun barcha ro'yxatdan o'tgan foydalanuvchilar."""
    async with _lock:
        cur = await _conn.execute("SELECT tg_id FROM users")
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def get_all_users():
    """Admin panelda ro'yxatdan o'tganlar ro'yxatini ko'rsatish uchun to'liq ma'lumot."""
    async with _lock:
        cur = await _conn.execute("SELECT * FROM users ORDER BY registered_at DESC")
        return await cur.fetchall()


async def get_user_answers(tg_id: int):
    async with _lock:
        cur = await _conn.execute(
            "SELECT * FROM answers WHERE tg_id = ? ORDER BY round_num", (tg_id,)
        )
        return await cur.fetchall()


async def has_answered(tg_id: int, tournament_id: int, round_num: int) -> bool:
    """Foydalanuvchi shu TURNIRNING shu raundiga allaqachon javob yuborganmi -
    qayta urinishni oldini olish uchun (turnir bo'yicha alohida hisoblanadi,
    chunki raund raqamlari turli turnirlarda takrorlanishi mumkin)."""
    async with _lock:
        cur = await _conn.execute(
            "SELECT 1 FROM answers WHERE tg_id = ? AND round_num = ? AND tournament_id = ? LIMIT 1",
            (tg_id, round_num, tournament_id),
        )
        return await cur.fetchone() is not None


async def get_all_answers():
    """Excel eksport uchun barcha javoblarni foydalanuvchi ma'lumotlari bilan qo'shib qaytaradi."""
    async with _lock:
        cur = await _conn.execute(
            """
            SELECT a.id, a.tg_id, u.full_name, u.username, a.tournament_id, a.round_num, a.content_type,
                   a.text_content, a.file_id, a.ref_num, a.ai_score, a.ai_correct, a.ai_incorrect,
                   a.wpm, a.time_sec, a.submitted_at
            FROM answers a
            LEFT JOIN users u ON u.tg_id = a.tg_id
            ORDER BY a.tournament_id, a.round_num, a.submitted_at
            """
        )
        return await cur.fetchall()


# ------------------------- SETTINGS -------------------------

async def get_setting(key: str) -> str | None:
    async with _lock:
        cur = await _conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    async with _lock:
        await _conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await _conn.commit()


async def is_tournament_active() -> bool:
    val = await get_setting("tournament_active")
    return val == "1"


async def set_tournament_active(active: bool) -> None:
    await set_setting("tournament_active", "1" if active else "0")
    if not active:
        # Turnir to'xtatilganda faol raund va tanlangan turnir ham tozalanadi
        await set_setting("active_round", "0")
        await set_setting("active_tournament", "0")


async def get_active_tournament() -> int:
    """Hozir o'tkazilayotgan turnir ID'si (masalan 100 yoki 300). 0 = tanlanmagan."""
    val = await get_setting("active_tournament")
    try:
        return int(val) if val else 0
    except ValueError:
        return 0


async def set_active_tournament(tournament_id: int) -> None:
    await set_setting("active_tournament", str(tournament_id))


async def get_active_round() -> int:
    """Hozir javob qabul qilinayotgan yagona raund raqami (0 = hech qaysi faol emas)."""
    val = await get_setting("active_round")
    try:
        return int(val) if val else 0
    except ValueError:
        return 0


async def set_active_round(round_num: int) -> None:
    """Yangi raundni faol qiladi - shu bilan avvalgi faol raund AVTOMATIK yopiladi
    (chunki bir vaqtning o'zida faqat bitta raund faol bo'lishi mumkin)."""
    await set_setting("active_round", str(round_num))


async def is_test_mode() -> bool:
    """TEST REJIMI yoniqmi? Yoniq bo'lsa, kanalga va foydalanuvchilarga
    HAQIQIY xabar/broadcast ketmaydi - faqat admin(lar)ga simulyatsiya ko'rinishida boradi."""
    val = await get_setting("test_mode")
    return val == "1"


async def set_test_mode(active: bool) -> None:
    await set_setting("test_mode", "1" if active else "0")


# ------------------------- AI GURUH/KANAL FAOLLIGI SOZLAMALARI -------------------------

async def get_ai_group_chat_id() -> int:
    """AI avtomatik izoh/mavzu ochish/javob berish ishlaydigan guruh ID'si. 0 = sozlanmagan."""
    val = await get_setting("ai_group_chat_id")
    try:
        return int(val) if val else 0
    except ValueError:
        return 0


async def set_ai_group_chat_id(chat_id: int) -> None:
    await set_setting("ai_group_chat_id", str(chat_id))


async def is_ai_auto_comment_enabled() -> bool:
    val = await get_setting("ai_auto_comment_enabled")
    return val == "1"


async def set_ai_auto_comment_enabled(active: bool) -> None:
    await set_setting("ai_auto_comment_enabled", "1" if active else "0")


async def is_ai_group_activity_enabled() -> bool:
    """Yoniq bo'lsa: AI foydalanuvchilar mention/reply qilganda guruhda javob beradi."""
    val = await get_setting("ai_group_activity_enabled")
    return val == "1"


async def set_ai_group_activity_enabled(active: bool) -> None:
    await set_setting("ai_group_activity_enabled", "1" if active else "0")


async def get_ai_persona() -> str:
    val = await get_setting("ai_persona")
    return val or DEFAULT_AI_PERSONA


async def set_ai_persona(text: str) -> None:
    await set_setting("ai_persona", text)


async def clear_answers() -> None:
    """Faqat javoblar tarixini tozalaydi (ro'yxatdan o'tganlar saqlanib qoladi)."""
    async with _lock:
        await _conn.execute("DELETE FROM answers")
        await _conn.execute("DELETE FROM assoc_images")
        await _conn.commit()


async def clear_all_data() -> None:
    """Botning barcha ma'lumotlarini (foydalanuvchilar + javoblar) butunlay tozalaydi,
    turnir holatini ham boshlang'ich holatga qaytaradi. QAYTARIB BO'LMAYDI."""
    async with _lock:
        await _conn.execute("DELETE FROM answers")
        await _conn.execute("DELETE FROM users")
        await _conn.execute("DELETE FROM assoc_images")
        await _conn.execute(
            "INSERT INTO settings (key, value) VALUES ('tournament_active', '0') "
            "ON CONFLICT(key) DO UPDATE SET value = '0'"
        )
        await _conn.execute(
            "INSERT INTO settings (key, value) VALUES ('active_round', '0') "
            "ON CONFLICT(key) DO UPDATE SET value = '0'"
        )
        await _conn.execute(
            "INSERT INTO settings (key, value) VALUES ('active_tournament', '0') "
            "ON CONFLICT(key) DO UPDATE SET value = '0'"
        )
        await _conn.commit()


# ------------------------- ASSOC IMAGES (Rasmga oidlik raundi) -------------------------

async def add_assoc_image(tournament_id: int, round_num: int, image_number: int, file_id: str) -> None:
    async with _lock:
        await _conn.execute(
            "INSERT OR REPLACE INTO assoc_images (tournament_id, round_num, image_number, file_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tournament_id, round_num, image_number, file_id, datetime.now().isoformat(timespec="seconds")),
        )
        await _conn.commit()


async def get_assoc_image(tournament_id: int, round_num: int, image_number: int):
    async with _lock:
        cur = await _conn.execute(
            "SELECT * FROM assoc_images WHERE tournament_id = ? AND round_num = ? AND image_number = ?",
            (tournament_id, round_num, image_number),
        )
        return await cur.fetchone()


async def get_assoc_images(tournament_id: int, round_num: int):
    async with _lock:
        cur = await _conn.execute(
            "SELECT * FROM assoc_images WHERE tournament_id = ? AND round_num = ? ORDER BY image_number",
            (tournament_id, round_num),
        )
        return await cur.fetchall()


async def clear_assoc_images(tournament_id: int, round_num: int) -> None:
    """Round qayta boshlanganda eski rasmlar/raqamlar chalkashmasligi uchun tozalash."""
    async with _lock:
        await _conn.execute(
            "DELETE FROM assoc_images WHERE tournament_id = ? AND round_num = ?",
            (tournament_id, round_num),
        )
        await _conn.commit()


# ------------------------- AI KALITLARI -------------------------

async def add_ai_key(provider: str, api_key: str, label: str | None = None) -> int:
    async with _lock:
        cur = await _conn.execute(
            "INSERT INTO ai_keys (provider, api_key, label, enabled, added_at) VALUES (?, ?, ?, 1, ?)",
            (provider, api_key, label, datetime.now().isoformat(timespec="seconds")),
        )
        await _conn.commit()
        return cur.lastrowid


async def get_all_ai_keys():
    """Admin panelda ro'yxat ko'rsatish uchun - yoqilgan/o'chirilgan barchasi."""
    async with _lock:
        cur = await _conn.execute("SELECT * FROM ai_keys ORDER BY provider, id")
        return await cur.fetchall()


async def get_enabled_ai_keys():
    """AI chaqiruvida foydalanish uchun - faqat yoqilganlar."""
    async with _lock:
        cur = await _conn.execute("SELECT * FROM ai_keys WHERE enabled = 1 ORDER BY provider, id")
        return await cur.fetchall()


async def toggle_ai_key(key_id: int) -> None:
    async with _lock:
        await _conn.execute("UPDATE ai_keys SET enabled = 1 - enabled WHERE id = ?", (key_id,))
        await _conn.commit()


async def delete_ai_key(key_id: int) -> None:
    async with _lock:
        await _conn.execute("DELETE FROM ai_keys WHERE id = ?", (key_id,))
        await _conn.commit()


# ------------------------- AI QOIDALARI (rejalashtirilgan post / guruh faolligi) -------------------------

async def add_ai_rule(rule_type: str, config_json: str) -> int:
    async with _lock:
        cur = await _conn.execute(
            "INSERT INTO ai_rules (rule_type, config, enabled, created_at) VALUES (?, ?, 1, ?)",
            (rule_type, config_json, datetime.now().isoformat(timespec="seconds")),
        )
        await _conn.commit()
        return cur.lastrowid


async def get_all_ai_rules(rule_type: str | None = None):
    async with _lock:
        if rule_type:
            cur = await _conn.execute("SELECT * FROM ai_rules WHERE rule_type = ? ORDER BY id", (rule_type,))
        else:
            cur = await _conn.execute("SELECT * FROM ai_rules ORDER BY rule_type, id")
        return await cur.fetchall()


async def get_enabled_ai_rules(rule_type: str | None = None):
    async with _lock:
        if rule_type:
            cur = await _conn.execute(
                "SELECT * FROM ai_rules WHERE rule_type = ? AND enabled = 1 ORDER BY id", (rule_type,)
            )
        else:
            cur = await _conn.execute("SELECT * FROM ai_rules WHERE enabled = 1 ORDER BY rule_type, id")
        return await cur.fetchall()


async def toggle_ai_rule(rule_id: int) -> None:
    async with _lock:
        await _conn.execute("UPDATE ai_rules SET enabled = 1 - enabled WHERE id = ?", (rule_id,))
        await _conn.commit()


async def delete_ai_rule(rule_id: int) -> None:
    async with _lock:
        await _conn.execute("DELETE FROM ai_rules WHERE id = ?", (rule_id,))
        await _conn.commit()


async def update_ai_rule_last_run(rule_id: int, when: str) -> None:
    async with _lock:
        await _conn.execute("UPDATE ai_rules SET last_run_at = ? WHERE id = ?", (when, rule_id))
        await _conn.commit()
