import os
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery


load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "football_bot.sqlite3")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tashkent")

# Можно указать Telegram ID админов через запятую:
# ADMIN_IDS=123456789,987654321
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError(
        "Заполните API_ID, API_HASH и BOT_TOKEN в .env или Railway Variables. "
        "Смотрите .env.example"
    )

app = Client(
    "football_team_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# Railway Volume: если DB_PATH=/data/football_bot.sqlite3,
# заранее создаем папку /data, чтобы SQLite мог открыть файл.
db_parent = Path(DB_PATH).expanduser().parent
if str(db_parent) not in ("", "."):
    db_parent.mkdir(parents=True, exist_ok=True)

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row


PROFILE_STEPS = [
    ("name", "👤 Напиши имя игрока, например: Ali"),
    ("main_position", "🎯 Основная позиция? Напиши: GK, DEF, MID, FWD или WING"),
    ("second_position", "🔁 Доп. позиция? Напиши: нет, GK, DEF, MID, FWD или WING"),
    ("speed", "⚡ Скорость от 1 до 100"),
    ("dribbling", "🎩 Дриблинг от 1 до 100"),
    ("passing", "🎯 Пас от 1 до 100"),
    ("shooting", "🥅 Атака/удар от 1 до 100"),
    ("defense", "🛡 Защита от 1 до 100"),
    ("stamina", "🔋 Выносливость от 1 до 100"),
    ("physical", "💪 Физика от 1 до 100"),
    ("goalkeeper_skill", "🧤 Умение играть в воротах от 1 до 100"),
    (
        "goalkeeper_willingness",
        "🚪 Готовность стоять на воротах:\n"
        "0 — никогда\n"
        "1 — только если нет вратаря\n"
        "2 — иногда\n"
        "3 — могу играть\n"
        "4 — я вратарь",
    ),
]

# Простой FSM в памяти. После перезапуска недозаполненные анкеты сбросятся.
profile_sessions: dict[int, dict[str, Any]] = {}

# Сессии админа: создание чужого игрока и редактирование характеристик.
admin_sessions: dict[int, dict[str, Any]] = {}

PLAYERS_PER_PAGE = 20

EDITABLE_FIELDS: list[tuple[str, str]] = [
    ("name", "👤 Имя"),
    ("main_position", "🎯 Основная позиция"),
    ("second_position", "🔁 Доп. позиция"),
    ("speed", "⚡ Скорость"),
    ("dribbling", "🎩 Дриблинг"),
    ("passing", "🎯 Пас"),
    ("shooting", "🥅 Атака/удар"),
    ("defense", "🛡 Защита"),
    ("stamina", "🔋 Выносливость"),
    ("physical", "💪 Физика"),
    ("goalkeeper_skill", "🧤 Вратарский навык"),
    ("goalkeeper_willingness", "🚪 Готовность в ворота"),
]

EDITABLE_FIELD_NAMES = {field for field, _label in EDITABLE_FIELDS}


@dataclass
class Player:
    telegram_id: int
    username: Optional[str]
    name: str
    main_position: str
    second_position: Optional[str]
    speed: int
    dribbling: int
    passing: int
    shooting: int
    defense: int
    stamina: int
    physical: int
    goalkeeper_skill: int
    goalkeeper_willingness: int
    overall_rating: int


def init_db() -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS players (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            name TEXT NOT NULL,
            main_position TEXT NOT NULL,
            second_position TEXT,
            speed INTEGER NOT NULL,
            dribbling INTEGER NOT NULL,
            passing INTEGER NOT NULL,
            shooting INTEGER NOT NULL,
            defense INTEGER NOT NULL,
            stamina INTEGER NOT NULL,
            physical INTEGER NOT NULL,
            goalkeeper_skill INTEGER NOT NULL DEFAULT 0,
            goalkeeper_willingness INTEGER NOT NULL DEFAULT 0,
            overall_rating INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            created_by INTEGER NOT NULL,
            match_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS match_players (
            match_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (match_id, player_id),
            FOREIGN KEY (match_id) REFERENCES matches(id),
            FOREIGN KEY (player_id) REFERENCES players(telegram_id)
        );

        CREATE INDEX IF NOT EXISTS idx_matches_chat_status
        ON matches(chat_id, status);

        CREATE INDEX IF NOT EXISTS idx_match_players_status
        ON match_players(match_id, status);
        """
    )

    ensure_column("matches", "team_count", "INTEGER DEFAULT 2")
    ensure_column("matches", "players_per_team", "INTEGER DEFAULT 0")
    db.commit()


def ensure_column(table: str, column: str, definition: str) -> None:
    """Добавляет колонку в SQLite, если бот обновляется со старой версии."""
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {row["name"] for row in rows}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def now_iso() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


def clamp_rating(value: str) -> int:
    try:
        number = int(value.strip())
    except ValueError as exc:
        raise ValueError("Нужно число от 1 до 100") from exc

    if not 1 <= number <= 100:
        raise ValueError("Нужно число от 1 до 100")
    return number


def normalize_position(value: str, allow_none: bool = False) -> Optional[str]:
    raw = value.strip().lower()

    if allow_none and raw in {"нет", "no", "none", "-", "0"}:
        return None

    mapping = {
        "gk": "GK",
        "goalkeeper": "GK",
        "keeper": "GK",
        "вратарь": "GK",
        "голкипер": "GK",
        "def": "DEF",
        "защитник": "DEF",
        "защ": "DEF",
        "mid": "MID",
        "полузащитник": "MID",
        "центр": "MID",
        "cm": "MID",
        "fwd": "FWD",
        "st": "FWD",
        "нападающий": "FWD",
        "форвард": "FWD",
        "wing": "WING",
        "вингер": "WING",
        "край": "WING",
    }

    if raw not in mapping:
        raise ValueError("Позиция должна быть: GK, DEF, MID, FWD или WING")
    return mapping[raw]


def parse_willingness(value: str) -> int:
    raw = value.strip().lower()
    mapping = {
        "0": 0,
        "никогда": 0,
        "нет": 0,
        "1": 1,
        "только если нет вратаря": 1,
        "если нет вратаря": 1,
        "2": 2,
        "иногда": 2,
        "3": 3,
        "могу": 3,
        "да": 3,
        "4": 4,
        "я вратарь": 4,
        "вратарь": 4,
    }
    if raw not in mapping:
        raise ValueError("Напиши число от 0 до 4")
    return mapping[raw]


def parse_profile_value(field: str, raw_value: str) -> Any:
    """Проверяет и преобразует значение анкеты/редактирования."""
    if field == "name":
        value = raw_value.strip()
        if len(value) < 2:
            raise ValueError("Имя слишком короткое.")
        if len(value) > 40:
            raise ValueError("Имя слишком длинное. Максимум 40 символов.")
        return value

    if field in {"main_position", "second_position"}:
        return normalize_position(raw_value, allow_none=(field == "second_position"))

    if field == "goalkeeper_willingness":
        return parse_willingness(raw_value)

    if field in {
        "speed", "dribbling", "passing", "shooting", "defense",
        "stamina", "physical", "goalkeeper_skill",
    }:
        return clamp_rating(raw_value)

    raise ValueError("Неизвестное поле профиля.")


def editable_field_label(field: str) -> str:
    for item_field, label in EDITABLE_FIELDS:
        if item_field == field:
            return label
    return field


def willingness_text(value: int) -> str:
    return {
        0: "никогда",
        1: "только если нет вратаря",
        2: "иногда",
        3: "могу играть",
        4: "я вратарь",
    }.get(value, "не указано")


def position_text(position: Optional[str]) -> str:
    return {
        "GK": "🧤 Вратарь",
        "DEF": "🛡 Защитник",
        "MID": "🎯 Полузащитник",
        "FWD": "🥅 Нападающий",
        "WING": "⚡ Вингер",
        None: "нет",
    }.get(position, position or "нет")


def calculate_overall(data: dict[str, Any]) -> int:
    position = data["main_position"]

    speed = int(data["speed"])
    dribbling = int(data["dribbling"])
    passing = int(data["passing"])
    shooting = int(data["shooting"])
    defense = int(data["defense"])
    stamina = int(data["stamina"])
    physical = int(data["physical"])
    gk = int(data["goalkeeper_skill"])

    if position == "GK":
        value = gk * 0.70 + physical * 0.10 + stamina * 0.05 + passing * 0.05 + defense * 0.10
    elif position == "DEF":
        value = defense * 0.35 + physical * 0.20 + speed * 0.15 + passing * 0.15 + stamina * 0.10 + shooting * 0.05
    elif position == "MID":
        value = passing * 0.30 + stamina * 0.20 + dribbling * 0.20 + defense * 0.15 + shooting * 0.15
    elif position == "FWD":
        value = shooting * 0.35 + speed * 0.20 + dribbling * 0.20 + passing * 0.10 + physical * 0.10 + defense * 0.05
    elif position == "WING":
        value = speed * 0.30 + dribbling * 0.25 + passing * 0.15 + shooting * 0.15 + stamina * 0.10 + defense * 0.05
    else:
        value = (speed + dribbling + passing + shooting + defense + stamina + physical) / 7

    return round(value)


def save_player(user_id: int, username: Optional[str], data: dict[str, Any]) -> None:
    overall = calculate_overall(data)
    timestamp = now_iso()

    db.execute(
        """
        INSERT INTO players (
            telegram_id, username, name, main_position, second_position,
            speed, dribbling, passing, shooting, defense, stamina, physical,
            goalkeeper_skill, goalkeeper_willingness, overall_rating,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username=excluded.username,
            name=excluded.name,
            main_position=excluded.main_position,
            second_position=excluded.second_position,
            speed=excluded.speed,
            dribbling=excluded.dribbling,
            passing=excluded.passing,
            shooting=excluded.shooting,
            defense=excluded.defense,
            stamina=excluded.stamina,
            physical=excluded.physical,
            goalkeeper_skill=excluded.goalkeeper_skill,
            goalkeeper_willingness=excluded.goalkeeper_willingness,
            overall_rating=excluded.overall_rating,
            updated_at=excluded.updated_at
        """,
        (
            user_id,
            username,
            data["name"],
            data["main_position"],
            data["second_position"],
            data["speed"],
            data["dribbling"],
            data["passing"],
            data["shooting"],
            data["defense"],
            data["stamina"],
            data["physical"],
            data["goalkeeper_skill"],
            data["goalkeeper_willingness"],
            overall,
            timestamp,
            timestamp,
        ),
    )
    db.commit()


def next_fake_player_id() -> int:
    """ID для игрока, созданного админом без Telegram-аккаунта."""
    row = db.execute(
        "SELECT MIN(telegram_id) AS min_id FROM players WHERE telegram_id < 0"
    ).fetchone()
    min_id = row["min_id"] if row else None
    return int(min_id) - 1 if min_id is not None else -1


def player_to_data(player: "Player") -> dict[str, Any]:
    return {
        "name": player.name,
        "main_position": player.main_position,
        "second_position": player.second_position,
        "speed": player.speed,
        "dribbling": player.dribbling,
        "passing": player.passing,
        "shooting": player.shooting,
        "defense": player.defense,
        "stamina": player.stamina,
        "physical": player.physical,
        "goalkeeper_skill": player.goalkeeper_skill,
        "goalkeeper_willingness": player.goalkeeper_willingness,
    }


def update_player_field(player_id: int, field: str, value: Any) -> "Player":
    if field not in EDITABLE_FIELD_NAMES:
        raise ValueError("Это поле нельзя менять.")

    player = get_player(player_id)
    if not player:
        raise ValueError("Игрок не найден.")

    data = player_to_data(player)
    data[field] = value
    overall = calculate_overall(data)
    timestamp = now_iso()

    db.execute(
        f"""
        UPDATE players
        SET {field} = ?, overall_rating = ?, updated_at = ?
        WHERE telegram_id = ?
        """,
        (value, overall, timestamp, player_id),
    )
    db.commit()

    updated = get_player(player_id)
    if not updated:
        raise ValueError("Не удалось обновить игрока.")
    return updated


def row_to_player(row: sqlite3.Row) -> Player:
    return Player(
        telegram_id=row["telegram_id"],
        username=row["username"],
        name=row["name"],
        main_position=row["main_position"],
        second_position=row["second_position"],
        speed=row["speed"],
        dribbling=row["dribbling"],
        passing=row["passing"],
        shooting=row["shooting"],
        defense=row["defense"],
        stamina=row["stamina"],
        physical=row["physical"],
        goalkeeper_skill=row["goalkeeper_skill"],
        goalkeeper_willingness=row["goalkeeper_willingness"],
        overall_rating=row["overall_rating"],
    )


def get_player(user_id: int) -> Optional[Player]:
    row = db.execute(
        "SELECT * FROM players WHERE telegram_id = ?",
        (user_id,),
    ).fetchone()
    return row_to_player(row) if row else None


def get_all_players() -> list[Player]:
    rows = db.execute(
        """
        SELECT *
        FROM players
        ORDER BY overall_rating DESC, name COLLATE NOCASE ASC
        """
    ).fetchall()
    return [row_to_player(row) for row in rows]


def get_players_count() -> int:
    row = db.execute("SELECT COUNT(*) AS cnt FROM players").fetchone()
    return int(row["cnt"])


def format_player_profile(player: Player) -> str:
    username_line = f"🔗 Username: @{player.username}\n" if player.username else ""
    return (
        f"👤 **{player.name}**\n"
        f"{username_line}"
        f"🆔 ID: `{player.telegram_id}`\n"
        f"🎯 Основная позиция: {position_text(player.main_position)}\n"
        f"🔁 Доп. позиция: {position_text(player.second_position)}\n\n"
        f"⚡ Скорость: {player.speed}\n"
        f"🎩 Дриблинг: {player.dribbling}\n"
        f"🎯 Пас: {player.passing}\n"
        f"🥅 Атака/удар: {player.shooting}\n"
        f"🛡 Защита: {player.defense}\n"
        f"🔋 Выносливость: {player.stamina}\n"
        f"💪 Физика: {player.physical}\n\n"
        f"🧤 Умение в воротах: {player.goalkeeper_skill}\n"
        f"🚪 Готовность в ворота: {willingness_text(player.goalkeeper_willingness)}\n\n"
        f"⭐ Общий рейтинг: **{player.overall_rating}**"
    )


async def is_admin(client: Client, chat_id: int, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True

    try:
        member = await client.get_chat_member(chat_id, user_id)
    except Exception:
        return False

    return member.status in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}


async def require_admin(message: Message) -> bool:
    if message.from_user is None:
        return False

    if message.chat.type == ChatType.PRIVATE:
        ok = message.from_user.id in ADMIN_IDS
    else:
        ok = await is_admin(app, message.chat.id, message.from_user.id)

    if not ok:
        await message.reply_text("⛔ Эту команду может использовать только админ.")
    return ok


async def require_admin_callback(query: CallbackQuery) -> bool:
    if query.message is None or query.message.chat is None:
        await query.answer("Не могу проверить права.", show_alert=True)
        return False

    chat = query.message.chat
    if chat.type == ChatType.PRIVATE:
        ok = query.from_user.id in ADMIN_IDS
    else:
        ok = await is_admin(app, chat.id, query.from_user.id)

    if not ok:
        await query.answer("⛔ Только админ может выбирать игроков.", show_alert=True)
    return ok


def get_open_match(chat_id: int) -> Optional[sqlite3.Row]:
    return db.execute(
        """
        SELECT * FROM matches
        WHERE chat_id = ? AND status = 'open'
        ORDER BY id DESC
        LIMIT 1
        """,
        (chat_id,),
    ).fetchone()


def get_match_by_id(match_id: int) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()


def get_match_format(match: sqlite3.Row) -> tuple[int, int]:
    keys = set(match.keys())
    team_count = int(match["team_count"] or 2) if "team_count" in keys else 2
    players_per_team = int(match["players_per_team"] or 0) if "players_per_team" in keys else 0
    return max(2, team_count), max(0, players_per_team)


def set_match_format(match_id: int, team_count: int, players_per_team: int) -> None:
    db.execute(
        "UPDATE matches SET team_count = ?, players_per_team = ? WHERE id = ?",
        (team_count, players_per_team, match_id),
    )
    db.commit()


def create_match(
    chat_id: int,
    created_by: int,
    team_count: int = 2,
    players_per_team: int = 0,
) -> tuple[int, bool]:
    active = get_open_match(chat_id)
    if active:
        return int(active["id"]), False

    cursor = db.execute(
        """
        INSERT INTO matches (
            chat_id, created_by, match_date, status, created_at,
            team_count, players_per_team
        )
        VALUES (?, ?, ?, 'open', ?, ?, ?)
        """,
        (chat_id, created_by, today_str(), now_iso(), team_count, players_per_team),
    )
    db.commit()
    return int(cursor.lastrowid), True


def parse_team_format_from_text(text: str, default_teams: int = 2, default_players: int = 0) -> tuple[int, int, bool]:
    """Возвращает: количество команд, игроков в команде, был ли формат указан."""
    parts = (text or "").split()
    if len(parts) == 1:
        return default_teams, default_players, False

    if len(parts) not in {2, 3}:
        raise ValueError("Формат команды: `/create_match 2 7` или `/teams 3 5`")

    try:
        team_count = int(parts[1])
        players_per_team = int(parts[2]) if len(parts) == 3 else default_players
    except ValueError as exc:
        raise ValueError("Количество команд и игроков должно быть числом.") from exc

    if team_count < 2 or team_count > 6:
        raise ValueError("Количество команд должно быть от 2 до 6.")

    if players_per_team < 0 or players_per_team > 20:
        raise ValueError("Игроков в команде должно быть от 1 до 20. Или 0 для авто.")

    return team_count, players_per_team, True


def format_match_format(match: sqlite3.Row) -> str:
    team_count, players_per_team = get_match_format(match)
    if players_per_team:
        total_needed = team_count * players_per_team
        return f"{team_count} команд × {players_per_team} игроков = нужно {total_needed}"
    return f"{team_count} команд, количество игроков авто"


def set_match_player(match_id: int, player_id: int, status: str = "playing") -> None:
    db.execute(
        """
        INSERT INTO match_players (match_id, player_id, status, joined_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(match_id, player_id) DO UPDATE SET
            status=excluded.status,
            joined_at=excluded.joined_at
        """,
        (match_id, player_id, status, now_iso()),
    )
    db.commit()


def remove_match_player(match_id: int, player_id: int) -> None:
    db.execute(
        "DELETE FROM match_players WHERE match_id = ? AND player_id = ?",
        (match_id, player_id),
    )
    db.commit()


def clear_match_players(match_id: int) -> None:
    db.execute("DELETE FROM match_players WHERE match_id = ?", (match_id,))
    db.commit()


def get_selected_player_ids(match_id: int) -> set[int]:
    rows = db.execute(
        """
        SELECT player_id
        FROM match_players
        WHERE match_id = ? AND status = 'playing'
        """,
        (match_id,),
    ).fetchall()
    return {int(row["player_id"]) for row in rows}


def get_match_players(match_id: int, status: Optional[str] = None) -> list[Player]:
    if status:
        rows = db.execute(
            """
            SELECT p.*
            FROM match_players mp
            JOIN players p ON p.telegram_id = mp.player_id
            WHERE mp.match_id = ? AND mp.status = ?
            ORDER BY p.overall_rating DESC, p.name COLLATE NOCASE ASC
            """,
            (match_id, status),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT p.*, mp.status AS match_status
            FROM match_players mp
            JOIN players p ON p.telegram_id = mp.player_id
            WHERE mp.match_id = ?
            ORDER BY mp.status, p.overall_rating DESC, p.name COLLATE NOCASE ASC
            """,
            (match_id,),
        ).fetchall()

    return [row_to_player(row) for row in rows]


def short_name(player: Player, limit: int = 22) -> str:
    name = player.name.strip()
    if len(name) <= limit:
        return name
    return name[: limit - 1] + "…"


def selection_keyboard(match_id: int, page: int = 0) -> InlineKeyboardMarkup:
    players = get_all_players()
    selected_ids = get_selected_player_ids(match_id)

    total_pages = max(1, (len(players) + PLAYERS_PER_PAGE - 1) // PLAYERS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * PLAYERS_PER_PAGE
    current_players = players[start:start + PLAYERS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for player in current_players:
        mark = "✅" if player.telegram_id in selected_ids else "➕"
        text = (
            f"{mark} {short_name(player)} "
            f"{player.main_position} ⭐{player.overall_rating}"
        )
        buttons.append([
            InlineKeyboardButton(
                text,
                callback_data=f"toggle:{match_id}:{page}:{player.telegram_id}",
            )
        ])

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"selpage:{match_id}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"selpage:{match_id}:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([
        InlineKeyboardButton("📋 Список выбранных", callback_data=f"selected:{match_id}:{page}"),
        InlineKeyboardButton("✅ Готово", callback_data=f"done:{match_id}:{page}"),
    ])

    return InlineKeyboardMarkup(buttons)


def selection_text(match_id: int, page: int = 0) -> str:
    total_players = get_players_count()
    selected_count = len(get_match_players(match_id, "playing"))
    total_pages = max(1, (total_players + PLAYERS_PER_PAGE - 1) // PLAYERS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    match = get_match_by_id(match_id)

    format_line = ""
    if match:
        team_count, players_per_team = get_match_format(match)
        if players_per_team:
            need = team_count * players_per_team
            format_line = (
                f"Формат: **{team_count}×{players_per_team}** — нужно игроков: **{need}**\n"
            )
        else:
            format_line = f"Формат: **{team_count} команд**, игроков в команде авто\n"

    return (
        f"👥 **Выбор игроков на матч #{match_id}**\n\n"
        f"{format_line}"
        f"Всего профилей в боте: **{total_players}**\n"
        f"Админ выбрал на игру: **{selected_count}**\n"
        f"Страница: **{page + 1}/{total_pages}**\n\n"
        "Нажимайте на игроков: ✅ выбран, ➕ не выбран.\n"
        "Обычные игроки сами записаться не могут.\n\n"
        "Формат можно изменить командой: `/teams 2 7`"
    )


def format_players_list(match_id: int) -> str:
    playing = get_match_players(match_id, "playing")

    lines = [f"📋 **Список игроков на матч #{match_id}**\n"]
    lines.append(f"Админ выбрал: **{len(playing)}**")

    if playing:
        for index, player in enumerate(playing, 1):
            username = f" @{player.username}" if player.username else ""
            lines.append(
                f"{index}. {player.name}{username} — {position_text(player.main_position)} — ⭐ {player.overall_rating}"
            )
    else:
        lines.append("Пока никого не выбрали. Админ может написать /select_players")

    return "\n".join(lines)


def format_all_profiles() -> str:
    players = get_all_players()
    if not players:
        return "Пока нет профилей. Игроки должны написать боту в личку: /new_profile"

    lines = [f"👥 **Все профили игроков: {len(players)}**\n"]
    for index, player in enumerate(players, 1):
        username = f" @{player.username}" if player.username else ""
        lines.append(
            f"{index}. {player.name}{username}\n"
            f"   ID: `{player.telegram_id}` | {position_text(player.main_position)} | "
            f"⭐ {player.overall_rating} | 🧤 {player.goalkeeper_skill}"
        )
    return "\n".join(lines)


def edit_players_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    players = get_all_players()
    total_pages = max(1, (len(players) + PLAYERS_PER_PAGE - 1) // PLAYERS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * PLAYERS_PER_PAGE
    current_players = players[start:start + PLAYERS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for player in current_players:
        buttons.append([
            InlineKeyboardButton(
                f"✏️ {short_name(player)} — {player.main_position} ⭐{player.overall_rating}",
                callback_data=f"editpick:{player.telegram_id}:{page}",
            )
        ])

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"editpage:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"editpage:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    return InlineKeyboardMarkup(buttons)


def edit_players_text(page: int = 0) -> str:
    players_count = get_players_count()
    total_pages = max(1, (players_count + PLAYERS_PER_PAGE - 1) // PLAYERS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    return (
        "✏️ **Редактирование игроков**\n\n"
        f"Всего профилей: **{players_count}**\n"
        f"Страница: **{page + 1}/{total_pages}**\n\n"
        "Выберите игрока, потом выберите характеристику."
    )


def edit_fields_keyboard(player_id: int, page: int = 0) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for field, label in EDITABLE_FIELDS:
        buttons.append([
            InlineKeyboardButton(label, callback_data=f"editfield:{player_id}:{field}")
        ])

    buttons.append([
        InlineKeyboardButton("⬅️ К списку игроков", callback_data=f"editpage:{page}")
    ])
    return InlineKeyboardMarkup(buttons)


def edit_player_text(player: Player) -> str:
    return (
        f"✏️ **Изменить игрока**\n\n"
        f"{format_player_profile(player)}\n\n"
        "Выберите, что изменить:"
    )


def find_players_by_query(query: str) -> list[Player]:
    query = query.strip()
    if not query:
        return []

    if query.isdigit():
        player = get_player(int(query))
        return [player] if player else []

    if query.startswith("@"):
        username = query[1:].strip().lower()
        rows = db.execute(
            "SELECT * FROM players WHERE lower(username) = ?",
            (username,),
        ).fetchall()
        return [row_to_player(row) for row in rows]

    # Поиск по имени: точное совпадение сначала, потом похожие.
    exact_rows = db.execute(
        "SELECT * FROM players WHERE lower(name) = ? ORDER BY overall_rating DESC",
        (query.lower(),),
    ).fetchall()
    if exact_rows:
        return [row_to_player(row) for row in exact_rows]

    like_rows = db.execute(
        """
        SELECT *
        FROM players
        WHERE lower(name) LIKE ?
        ORDER BY overall_rating DESC, name COLLATE NOCASE ASC
        LIMIT 10
        """,
        (f"%{query.lower()}%",),
    ).fetchall()
    return [row_to_player(row) for row in like_rows]


def goalkeeper_score(player: Player) -> int:
    # willingness важна: слабый, но готовый игрок лучше сильного, который совсем не хочет.
    willingness_bonus = {
        0: -1000,
        1: 5,
        2: 12,
        3: 20,
        4: 35,
    }[player.goalkeeper_willingness]

    main_bonus = 25 if player.main_position == "GK" else 0
    second_bonus = 10 if player.second_position == "GK" else 0
    return player.goalkeeper_skill + willingness_bonus + main_bonus + second_bonus


def preferred_position(player: Player) -> str:
    if player.main_position == "GK":
        return "GK"
    return player.main_position


def team_metrics(team: list[Player]) -> dict[str, float]:
    if not team:
        return {
            "overall": 0,
            "attack": 0,
            "defense": 0,
            "speed": 0,
            "gk": 0,
            "total": 0,
        }

    size = len(team)
    return {
        "overall": round(sum(p.overall_rating for p in team) / size, 1),
        "attack": round(sum(p.shooting for p in team) / size, 1),
        "defense": round(sum(p.defense for p in team) / size, 1),
        "speed": round(sum(p.speed for p in team) / size, 1),
        "gk": round(max(p.goalkeeper_skill for p in team), 1),
        "total": sum(p.overall_rating for p in team),
    }


def position_counts(team: list[Player]) -> dict[str, int]:
    counts = {"GK": 0, "DEF": 0, "MID": 0, "FWD": 0, "WING": 0}
    for player in team:
        counts[preferred_position(player)] = counts.get(preferred_position(player), 0) + 1
    return counts


def assignment_penalty(team: list[Player], candidate: Player, max_size: int) -> float:
    if len(team) >= max_size:
        return 10_000

    new_team = [*team, candidate]
    metrics = team_metrics(new_team)
    counts = position_counts(new_team)

    # Чем ниже penalty, тем лучше.
    penalty = metrics["total"]

    # Пытаемся не сложить всех нападающих/защитников в одну команду.
    penalty += counts["FWD"] * 3
    penalty += counts["WING"] * 2
    penalty += counts["DEF"] * 2
    penalty += counts["MID"] * 1

    # Если в команде нет защитника, добавление DEF чуть выгоднее.
    if candidate.main_position == "DEF" and position_counts(team)["DEF"] == 0:
        penalty -= 6

    # Если в команде нет атакующего, добавление FWD/WING чуть выгоднее.
    if candidate.main_position in {"FWD", "WING"}:
        old_counts = position_counts(team)
        if old_counts["FWD"] + old_counts["WING"] == 0:
            penalty -= 6

    return penalty


def target_team_sizes(total_players: int, team_count: int, players_per_team: int = 0) -> list[int]:
    if players_per_team > 0:
        return [players_per_team for _ in range(team_count)]

    base = total_players // team_count
    extra = total_players % team_count
    return [base + (1 if index < extra else 0) for index in range(team_count)]


def balance_n_teams(
    players: list[Player],
    team_count: int = 2,
    players_per_team: int = 0,
    shuffle: bool = False,
) -> list[list[Player]]:
    if team_count < 2:
        team_count = 2

    rng = random.Random()
    if not shuffle:
        rng.seed(42)

    players = players[:]
    rng.shuffle(players)

    targets = target_team_sizes(len(players), team_count, players_per_team)
    teams: list[list[Player]] = [[] for _ in range(team_count)]

    # 1) Сначала разводим вратарей/запасных вратарей по разным командам.
    gk_candidates = [
        p for p in players
        if p.goalkeeper_willingness > 0 or p.main_position == "GK" or p.second_position == "GK"
    ]
    gk_candidates.sort(key=goalkeeper_score, reverse=True)

    selected_gks: list[Player] = []
    for candidate in gk_candidates:
        if len(selected_gks) >= team_count:
            break
        if candidate.goalkeeper_willingness == 0 and candidate.main_position != "GK":
            continue
        selected_gks.append(candidate)

    for team_index, goalkeeper in enumerate(selected_gks):
        if team_index < len(teams) and len(teams[team_index]) < targets[team_index]:
            teams[team_index].append(goalkeeper)

    remaining = [p for p in players if p not in selected_gks]

    # 2) Потом распределяем остальных по силе: сильные раньше.
    remaining.sort(
        key=lambda p: (
            p.overall_rating + rng.uniform(-3, 3) if shuffle else p.overall_rating,
            p.shooting,
            p.defense,
        ),
        reverse=True,
    )

    for player in remaining:
        best_team_index = None
        best_penalty = None

        totals = [team_metrics(team)["total"] for team in teams]
        min_total = min(totals) if totals else 0

        for index, team in enumerate(teams):
            if len(team) >= targets[index]:
                continue

            penalty = assignment_penalty(team, player, targets[index])
            penalty += max(0, totals[index] - min_total) * 0.8
            penalty += len(team) * 2.0

            if best_penalty is None or penalty < best_penalty:
                best_penalty = penalty
                best_team_index = index

        if best_team_index is None:
            teams[-1].append(player)
        else:
            teams[best_team_index].append(player)

    # 3) Пара простых обменов между самой сильной и самой слабой командой.
    for _ in range(40):
        totals = [team_metrics(team)["total"] for team in teams]
        strongest_index = max(range(len(teams)), key=lambda i: totals[i])
        weakest_index = min(range(len(teams)), key=lambda i: totals[i])
        current_diff = totals[strongest_index] - totals[weakest_index]

        if current_diff <= 5:
            break

        strongest = teams[strongest_index]
        weakest = teams[weakest_index]
        best_swap = None
        best_diff = current_diff

        for strong_player in strongest:
            for weak_player in weakest:
                if strong_player in selected_gks or weak_player in selected_gks:
                    continue

                new_totals = totals[:]
                new_totals[strongest_index] = (
                    new_totals[strongest_index]
                    - strong_player.overall_rating
                    + weak_player.overall_rating
                )
                new_totals[weakest_index] = (
                    new_totals[weakest_index]
                    - weak_player.overall_rating
                    + strong_player.overall_rating
                )
                new_diff = max(new_totals) - min(new_totals)

                if new_diff < best_diff:
                    best_diff = new_diff
                    best_swap = (strong_player, weak_player)

        if not best_swap:
            break

        strong_player, weak_player = best_swap
        strongest.remove(strong_player)
        strongest.append(weak_player)
        weakest.remove(weak_player)
        weakest.append(strong_player)

    return teams


def balance_teams(players: list[Player], shuffle: bool = False) -> tuple[list[Player], list[Player]]:
    teams = balance_n_teams(players, team_count=2, players_per_team=0, shuffle=shuffle)
    return teams[0], teams[1]


TEAM_NAMES = [
    "🔴 Команда A",
    "🔵 Команда B",
    "🟢 Команда C",
    "🟡 Команда D",
    "🟣 Команда E",
    "⚫ Команда F",
]


def format_team(name: str, team: list[Player]) -> str:
    lines = [f"**{name}**"]
    for index, player in enumerate(team, 1):
        lines.append(
            f"{index}. {player.name} — {position_text(player.main_position)} — ⭐ {player.overall_rating}"
        )
    return "\n".join(lines)


def format_balanced_teams_n(teams: list[list[Player]]) -> str:
    parts = ["⚽ **Деление команд**"]
    for index, team in enumerate(teams):
        name = TEAM_NAMES[index] if index < len(TEAM_NAMES) else f"Команда {index + 1}"
        parts.append(format_team(name, team))
    return "\n\n".join(parts)


def format_balanced_teams(team_a: list[Player], team_b: list[Player]) -> str:
    return format_balanced_teams_n([team_a, team_b])


@app.on_message(filters.command("start"))
async def start_handler(_: Client, message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        text = (
            "⚽ Привет! Я бот для футбольной группы.\n\n"
            "Игроки могут только создать или обновить профиль:\n"
            "/new_profile — создать или обновить FIFA-профиль\n"
            "/profile — посмотреть свой профиль\n\n"
            "Админ может создавать/редактировать игроков:\n"
            "/admin_new_player — создать игрока вручную\n"
            "/edit_player — изменить характеристики игрока\n\n"
            "Матч и список игроков выбирает только админ в группе."
        )
    else:
        text = (
            "⚽ Бот работает.\n\n"
            "Игроки создают профиль в личке бота: /new_profile\n"
            "Матч и список игроков выбирает только админ."
        )
    await message.reply_text(text)


@app.on_message(filters.private & filters.command("new_profile"))
async def new_profile_handler(_: Client, message: Message) -> None:
    user_id = message.from_user.id
    profile_sessions[user_id] = {"step": 0, "data": {}}
    await message.reply_text(
        "Начинаем создание FIFA-профиля ⚽\n"
        "Можно в любой момент написать /cancel.\n\n"
        + PROFILE_STEPS[0][1]
    )


@app.on_message(filters.private & filters.command("cancel"))
async def cancel_handler(_: Client, message: Message) -> None:
    user_id = message.from_user.id
    profile_sessions.pop(user_id, None)
    admin_sessions.pop(user_id, None)
    await message.reply_text("Ок, действие отменил.")


@app.on_message(filters.private & filters.command("profile"))
async def profile_handler(_: Client, message: Message) -> None:
    player = get_player(message.from_user.id)
    if not player:
        await message.reply_text("Профиля пока нет. Создай его командой /new_profile")
        return

    await message.reply_text(format_player_profile(player))


@app.on_message(filters.private & filters.command(["admin_new_player", "new_player"]))
async def admin_new_player_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    user_id = message.from_user.id
    profile_sessions.pop(user_id, None)
    admin_sessions[user_id] = {"mode": "admin_create_player", "step": 0, "data": {}}

    await message.reply_text(
        "➕ **Создание игрока админом**\n\n"
        "Этот игрок будет создан без Telegram-аккаунта. "
        "Потом админ сможет выбрать его на матч как обычного игрока.\n\n"
        "Можно отменить командой /cancel.\n\n"
        + PROFILE_STEPS[0][1]
    )


@app.on_message(filters.command(["edit_player", "edit_players"]))
async def edit_player_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    if get_players_count() == 0:
        await message.reply_text("Пока нет профилей игроков.")
        return

    await message.reply_text(
        edit_players_text(page=0),
        reply_markup=edit_players_keyboard(page=0),
    )


@app.on_message(filters.command("set_player"))
async def set_player_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 4:
        await message.reply_text(
            "Быстрое изменение через команду:\n"
            "`/set_player ID поле значение`\n\n"
            "Пример:\n"
            "`/set_player -1 speed 75`\n"
            "`/set_player 123456789 shooting 80`\n\n"
            "Проще использовать кнопки: /edit_player"
        )
        return

    try:
        player_id = int(parts[1])
    except ValueError:
        await message.reply_text("ID игрока должен быть числом. Посмотреть ID: /profiles")
        return

    field = parts[2].strip()
    raw_value = parts[3].strip()

    if field not in EDITABLE_FIELD_NAMES:
        await message.reply_text(
            "Такого поля нет. Доступные поля:\n"
            + ", ".join(field for field, _label in EDITABLE_FIELDS)
        )
        return

    try:
        value = parse_profile_value(field, raw_value)
        player = update_player_field(player_id, field, value)
    except ValueError as exc:
        await message.reply_text(f"❌ {exc}")
        return

    await message.reply_text("✅ Игрок обновлен:\n\n" + format_player_profile(player))


@app.on_message(filters.private & filters.text & ~filters.command([
    "start", "new_profile", "admin_new_player", "new_player", "edit_player",
    "edit_players", "set_player", "cancel", "profile"
]))
async def profile_text_handler(_: Client, message: Message) -> None:
    user_id = message.from_user.id
    raw_value = message.text or ""

    admin_session = admin_sessions.get(user_id)
    if admin_session:
        mode = admin_session.get("mode")

        if mode == "admin_create_player":
            step_index = admin_session["step"]
            field, _question = PROFILE_STEPS[step_index]

            try:
                value = parse_profile_value(field, raw_value)
            except ValueError as exc:
                await message.reply_text(f"❌ {exc}\n\nПопробуй еще раз.")
                return

            admin_session["data"][field] = value
            admin_session["step"] += 1

            if admin_session["step"] >= len(PROFILE_STEPS):
                fake_id = next_fake_player_id()
                save_player(
                    user_id=fake_id,
                    username=None,
                    data=admin_session["data"],
                )
                admin_sessions.pop(user_id, None)
                player = get_player(fake_id)
                await message.reply_text(
                    "✅ Игрок создан админом!\n\n" + format_player_profile(player)
                )
                return

            _next_field, next_question = PROFILE_STEPS[admin_session["step"]]
            await message.reply_text(next_question)
            return

        if mode == "edit_field":
            player_id = int(admin_session["player_id"])
            field = str(admin_session["field"])

            try:
                value = parse_profile_value(field, raw_value)
                player = update_player_field(player_id, field, value)
            except ValueError as exc:
                await message.reply_text(f"❌ {exc}\n\nПопробуй еще раз или /cancel")
                return

            admin_sessions.pop(user_id, None)
            await message.reply_text(
                f"✅ Изменено: **{editable_field_label(field)}**\n\n"
                + format_player_profile(player)
            )
            return

    session = profile_sessions.get(user_id)
    if not session:
        if raw_value.startswith("/"):
            return
        await message.reply_text("Чтобы создать профиль, напиши /new_profile")
        return

    step_index = session["step"]
    field, _question = PROFILE_STEPS[step_index]

    try:
        value = parse_profile_value(field, raw_value)
    except ValueError as exc:
        await message.reply_text(f"❌ {exc}\n\nПопробуй еще раз.")
        return

    session["data"][field] = value
    session["step"] += 1

    if session["step"] >= len(PROFILE_STEPS):
        save_player(
            user_id=user_id,
            username=message.from_user.username,
            data=session["data"],
        )
        profile_sessions.pop(user_id, None)
        player = get_player(user_id)
        await message.reply_text(
            "✅ Профиль сохранен!\n\n" + format_player_profile(player)
        )
        return

    _next_field, next_question = PROFILE_STEPS[session["step"]]
    await message.reply_text(next_question)


@app.on_message(filters.command(["profiles", "all_players"]))
async def profiles_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return
    await message.reply_text(format_all_profiles())


@app.on_message(filters.group & filters.command("create_match"))
async def create_match_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    try:
        team_count, players_per_team, format_was_set = parse_team_format_from_text(
            message.text or "", default_teams=2, default_players=0
        )
    except ValueError as exc:
        await message.reply_text(f"❌ {exc}\n\nПример: `/create_match 2 7` или `/create_match 3 5`")
        return

    match_id, created = create_match(
        message.chat.id,
        message.from_user.id,
        team_count=team_count,
        players_per_team=players_per_team,
    )

    if not created and format_was_set:
        set_match_format(match_id, team_count, players_per_team)

    match = get_match_by_id(match_id)
    if created:
        header = f"⚽ **Матч #{match_id} создан**"
    else:
        header = f"⚽ **Уже есть открытый матч #{match_id}**"

    await message.reply_text(
        f"{header}\n"
        f"Формат: **{format_match_format(match)}**\n\n"
        "Игроки сами не записываются.\n"
        "Только админ выбирает список игроков из готовых профилей.\n\n"
        "Нажимайте на игроков ниже или используйте команду /select_players.\n"
        "Изменить формат: `/teams 2 7` или `/teams 3 5`",
        reply_markup=selection_keyboard(match_id, page=0),
    )


@app.on_message(filters.group & filters.command(["teams", "match_format"]))
async def teams_format_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    active = get_open_match(message.chat.id)
    if not active:
        await message.reply_text("Пока нет открытого матча. Сначала /create_match")
        return

    try:
        team_count, players_per_team, was_set = parse_team_format_from_text(
            message.text or "", default_teams=2, default_players=0
        )
    except ValueError as exc:
        await message.reply_text(
            f"❌ {exc}\n\n"
            "Примеры:\n"
            "`/teams 2 7` — 2 команды по 7\n"
            "`/teams 3 5` — 3 команды по 5"
        )
        return

    if not was_set:
        await message.reply_text(
            "Напишите формат так:\n"
            "`/teams 2 7` — 2 команды по 7\n"
            "`/teams 3 5` — 3 команды по 5"
        )
        return

    set_match_format(int(active["id"]), team_count, players_per_team)
    active = get_match_by_id(int(active["id"]))
    await message.reply_text(
        f"✅ Формат матча изменен: **{format_match_format(active)}**\n\n"
        "Теперь выберите нужное количество игроков через /select_players."
    )


@app.on_message(filters.group & filters.command("select_players"))
async def select_players_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    active = get_open_match(message.chat.id)
    if not active:
        await message.reply_text("Пока нет открытого матча. Сначала админ должен написать /create_match")
        return

    await message.reply_text(
        selection_text(active["id"], page=0),
        reply_markup=selection_keyboard(active["id"], page=0),
    )


@app.on_message(filters.group & filters.command("players_today"))
async def players_today_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    active = get_open_match(message.chat.id)
    if not active:
        await message.reply_text("Пока нет открытого матча. Админ может написать /create_match")
        return

    await message.reply_text(format_players_list(active["id"]))


@app.on_message(filters.group & filters.command("add_player"))
async def add_player_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    active = get_open_match(message.chat.id)
    if not active:
        await message.reply_text("Пока нет открытого матча. Сначала /create_match")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text(
            "Напиши так:\n"
            "`/add_player ID`\n"
            "или:\n"
            "`/add_player @username`\n"
            "или:\n"
            "`/add_player имя`\n\n"
            "Посмотреть ID можно командой /profiles"
        )
        return

    query = parts[1].strip()
    # Если админ вставил несколько ID через пробел/запятую — добавляем всех.
    tokens = [x.strip() for x in query.replace(",", " ").split() if x.strip()]
    if len(tokens) > 1 and all(t.isdigit() or t.startswith("@") for t in tokens):
        added = []
        not_found = []
        for token in tokens:
            found = find_players_by_query(token)
            if len(found) == 1:
                set_match_player(active["id"], found[0].telegram_id, "playing")
                added.append(found[0].name)
            else:
                not_found.append(token)
        text = ""
        if added:
            text += "✅ Добавлены:\n" + "\n".join(f"• {name}" for name in added)
        if not_found:
            text += "\n\nНе нашел:\n" + "\n".join(f"• {token}" for token in not_found)
        await message.reply_text(text or "Никого не добавил.")
        return

    found_players = find_players_by_query(query)
    if not found_players:
        await message.reply_text("Не нашел такого игрока. Посмотрите список профилей командой /profiles")
        return

    if len(found_players) > 1:
        lines = ["Нашел несколько игроков. Добавьте по ID:"]
        for player in found_players:
            lines.append(f"• {player.name} — ID: `{player.telegram_id}` — ⭐ {player.overall_rating}")
        await message.reply_text("\n".join(lines))
        return

    player = found_players[0]
    set_match_player(active["id"], player.telegram_id, "playing")
    await message.reply_text(f"✅ Добавил на матч: {player.name}")


@app.on_message(filters.group & filters.command("remove_player"))
async def remove_player_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    active = get_open_match(message.chat.id)
    if not active:
        await message.reply_text("Пока нет открытого матча. Сначала /create_match")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text(
            "Напиши так:\n"
            "`/remove_player ID`\n"
            "или:\n"
            "`/remove_player @username`\n"
            "или:\n"
            "`/remove_player имя`"
        )
        return

    query = parts[1].strip()
    found_players = find_players_by_query(query)
    if not found_players:
        await message.reply_text("Не нашел такого игрока.")
        return

    if len(found_players) > 1:
        lines = ["Нашел несколько игроков. Удалите по ID:"]
        for player in found_players:
            lines.append(f"• {player.name} — ID: `{player.telegram_id}` — ⭐ {player.overall_rating}")
        await message.reply_text("\n".join(lines))
        return

    player = found_players[0]
    remove_match_player(active["id"], player.telegram_id)
    await message.reply_text(f"➖ Убрал с матча: {player.name}")


@app.on_message(filters.group & filters.command("clear_players"))
async def clear_players_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    active = get_open_match(message.chat.id)
    if not active:
        await message.reply_text("Открытого матча нет.")
        return

    clear_match_players(active["id"])
    await message.reply_text("🧹 Список игроков на текущий матч очищен.")


def validate_balance_request(match: sqlite3.Row, players_count: int) -> Optional[str]:
    team_count, players_per_team = get_match_format(match)

    if players_count < team_count:
        return f"Нужно минимум {team_count} игроков, чтобы сделать {team_count} команд."

    if players_per_team > 0:
        need = team_count * players_per_team
        if players_count != need:
            return (
                f"Для формата **{team_count}×{players_per_team}** нужно игроков: **{need}**.\n"
                f"Сейчас выбрано: **{players_count}**.\n\n"
                "Добавьте/уберите игроков через /select_players или измените формат, например `/teams 2 7`."
            )

    return None


@app.on_message(filters.group & filters.command("balance"))
async def balance_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    active = get_open_match(message.chat.id)
    if not active:
        await message.reply_text("Пока нет открытого матча. Сначала /create_match")
        return

    players = get_match_players(active["id"], "playing")
    error = validate_balance_request(active, len(players))
    if error:
        await message.reply_text("❌ " + error)
        return

    team_count, players_per_team = get_match_format(active)
    teams = balance_n_teams(players, team_count=team_count, players_per_team=players_per_team, shuffle=False)
    await message.reply_text(format_balanced_teams_n(teams))


@app.on_message(filters.group & filters.command("shuffle"))
async def shuffle_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    active = get_open_match(message.chat.id)
    if not active:
        await message.reply_text("Пока нет открытого матча. Сначала /create_match")
        return

    players = get_match_players(active["id"], "playing")
    error = validate_balance_request(active, len(players))
    if error:
        await message.reply_text("❌ " + error)
        return

    team_count, players_per_team = get_match_format(active)
    teams = balance_n_teams(players, team_count=team_count, players_per_team=players_per_team, shuffle=True)
    await message.reply_text("🔄 Новый вариант:\n\n" + format_balanced_teams_n(teams))


@app.on_message(filters.group & filters.command("reset_match"))
async def reset_match_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    active = get_open_match(message.chat.id)
    if not active:
        await message.reply_text("Открытого матча нет.")
        return

    db.execute(
        "UPDATE matches SET status = 'cancelled' WHERE id = ?",
        (active["id"],),
    )
    db.commit()
    await message.reply_text("♻️ Текущий матч сброшен. Можно создать новый через /create_match")


@app.on_callback_query(filters.regex(r"^(editpage|editpick|editfield):"))
async def admin_edit_callback_handler(_: Client, query: CallbackQuery) -> None:
    if not await require_admin_callback(query):
        return

    data = query.data or ""
    parts = data.split(":")
    action = parts[0]

    if action == "editpage":
        try:
            page = int(parts[1])
        except (IndexError, ValueError):
            page = 0

        try:
            await query.message.edit_text(
                edit_players_text(page=page),
                reply_markup=edit_players_keyboard(page=page),
            )
        except Exception:
            pass
        await query.answer()
        return

    if action == "editpick":
        try:
            player_id = int(parts[1])
            page = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("Ошибка данных игрока.", show_alert=True)
            return

        player = get_player(player_id)
        if not player:
            await query.answer("Игрок не найден.", show_alert=True)
            return

        try:
            await query.message.edit_text(
                edit_player_text(player),
                reply_markup=edit_fields_keyboard(player_id, page=page),
            )
        except Exception:
            pass
        await query.answer()
        return

    if action == "editfield":
        try:
            player_id = int(parts[1])
            field = parts[2]
        except (IndexError, ValueError):
            await query.answer("Ошибка данных поля.", show_alert=True)
            return

        if field not in EDITABLE_FIELD_NAMES:
            await query.answer("Это поле нельзя менять.", show_alert=True)
            return

        player = get_player(player_id)
        if not player:
            await query.answer("Игрок не найден.", show_alert=True)
            return

        admin_sessions[query.from_user.id] = {
            "mode": "edit_field",
            "player_id": player_id,
            "field": field,
        }

        current_value = getattr(player, field)
        await query.message.reply_text(
            f"✏️ Меняем: **{editable_field_label(field)}**\n"
            f"Игрок: **{player.name}**\n"
            f"Сейчас: `{current_value}`\n\n"
            "Отправьте новое значение одним сообщением.\n"
            "Отмена: /cancel"
        )
        await query.answer("Жду новое значение")
        return


@app.on_callback_query(filters.regex(r"^(toggle|selpage|selected|done):"))
async def admin_selection_callback_handler(_: Client, query: CallbackQuery) -> None:
    if not await require_admin_callback(query):
        return

    data = query.data or ""
    parts = data.split(":")
    action = parts[0]

    try:
        match_id = int(parts[1])
    except (IndexError, ValueError):
        await query.answer("Ошибка данных кнопки.", show_alert=True)
        return

    match = db.execute(
        "SELECT * FROM matches WHERE id = ? AND status = 'open'",
        (match_id,),
    ).fetchone()

    if not match:
        await query.answer("Матч уже закрыт или не найден.", show_alert=True)
        return

    if action == "toggle":
        try:
            page = int(parts[2])
            player_id = int(parts[3])
        except (IndexError, ValueError):
            await query.answer("Ошибка данных игрока.", show_alert=True)
            return

        selected_ids = get_selected_player_ids(match_id)
        player = get_player(player_id)
        if not player:
            await query.answer("Профиль игрока не найден.", show_alert=True)
            return

        if player_id in selected_ids:
            remove_match_player(match_id, player_id)
            await query.answer(f"➖ Убрал: {player.name}")
        else:
            set_match_player(match_id, player_id, "playing")
            await query.answer(f"✅ Добавил: {player.name}")

        try:
            await query.message.edit_text(
                selection_text(match_id, page=page),
                reply_markup=selection_keyboard(match_id, page=page),
            )
        except Exception:
            pass
        return

    if action == "selpage":
        try:
            page = int(parts[2])
        except (IndexError, ValueError):
            page = 0

        try:
            await query.message.edit_text(
                selection_text(match_id, page=page),
                reply_markup=selection_keyboard(match_id, page=page),
            )
        except Exception:
            pass
        await query.answer()
        return

    if action == "selected":
        try:
            page = int(parts[2])
        except (IndexError, ValueError):
            page = 0

        await query.message.reply_text(format_players_list(match_id))
        await query.answer()
        try:
            await query.message.edit_text(
                selection_text(match_id, page=page),
                reply_markup=selection_keyboard(match_id, page=page),
            )
        except Exception:
            pass
        return

    if action == "done":
        selected_count = len(get_match_players(match_id, "playing"))
        match = get_match_by_id(match_id)
        format_line = f"Формат: **{format_match_format(match)}**\n" if match else ""
        await query.message.reply_text(
            f"✅ Список игроков на матч #{match_id} сохранен.\n"
            f"{format_line}"
            f"Выбрано игроков: **{selected_count}**\n\n"
            "Теперь админ может написать /balance"
        )
        await query.answer("Готово")
        return


if __name__ == "__main__":
    init_db()
    print("Football Team Bot started...")
    app.run()
