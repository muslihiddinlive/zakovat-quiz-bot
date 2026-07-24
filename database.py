# database.py
# ------------------------------------------------------------
# SQLite (aiosqlite) yordamida asinxron ma'lumotlar bazasi qatlami.
# Jadvallar: users, answers, settings
#
# Eslatma: har bir so'rov uchun yangi ulanish ochish o'rniga (bu ko'p
# yozuv bo'lganda "database is locked" xatosiga olib kelishi mumkin edi),
# butun dastur davomida BITTA umumiy ulanish saqlanadi va asyncio.Lock
# bilan himoyalanadi.
# ------------------------------------------------------------
import asyncio
import aiosqlite
from datetime import datetime

DB_PATH = "zakovat_quiz.db"

_conn: aiosqlite.Connection | None = None
_lock = asyncio.Lock()


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
                content_type  TEXT,       -- text / photo / sticker / voice / audio / photo_text
                text_content  TEXT,
                file_id       TEXT,
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
        # Boshlang'ich sozlamalar (faqat birinchi marta yaratiladi)
        await _conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('tournament_active', '0')"
        )
        for i in range(1, 8):
            await _conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, '1')",
                (f"round_{i}_enabled",),
            )
        await _conn.commit()


async def close_db() -> None:
    """Dastur to'xtaganda ulanishni yopish uchun (ixtiyoriy, lekin toza yopilish uchun tavsiya etiladi)."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


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
    text_content: str | None = None,
    file_id: str | None = None,
    wpm: float | None = None,
    time_sec: float | None = None,
) -> None:
    async with _lock:
        await _conn.execute(
            """
            INSERT INTO answers (tg_id, round_num, content_type, text_content, file_id, wpm, time_sec, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tg_id, round_num, content_type, text_content, file_id, wpm, time_sec,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        await _conn.commit()


async def get_user_answers(tg_id: int):
    async with _lock:
        cur = await _conn.execute(
            "SELECT * FROM answers WHERE tg_id = ? ORDER BY round_num", (tg_id,)
        )
        return await cur.fetchall()


async def has_answered(tg_id: int, round_num: int) -> bool:
    """Foydalanuvchi shu raundga allaqachon javob yuborganmi - qayta urinishni oldini olish uchun."""
    async with _lock:
        cur = await _conn.execute(
            "SELECT 1 FROM answers WHERE tg_id = ? AND round_num = ? LIMIT 1",
            (tg_id, round_num),
        )
        return await cur.fetchone() is not None


async def get_all_answers():
    """Excel eksport uchun barcha javoblarni foydalanuvchi ma'lumotlari bilan qo'shib qaytaradi."""
    async with _lock:
        cur = await _conn.execute(
            """
            SELECT a.id, a.tg_id, u.full_name, u.username, a.round_num, a.content_type,
                   a.text_content, a.file_id, a.wpm, a.time_sec, a.submitted_at
            FROM answers a
            LEFT JOIN users u ON u.tg_id = a.tg_id
            ORDER BY a.round_num, a.submitted_at
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


async def is_round_enabled(round_num: int) -> bool:
    val = await get_setting(f"round_{round_num}_enabled")
    return val == "1"


async def toggle_round(round_num: int) -> bool:
    """Raund holatini teskarisiga o'zgartiradi va yangi holatni qaytaradi."""
    current = await is_round_enabled(round_num)
    new_val = "0" if current else "1"
    await set_setting(f"round_{round_num}_enabled", new_val)
    return new_val == "1"
