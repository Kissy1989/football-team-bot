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

PLAYERS_PER_PAGE = 20


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
    db.commit()


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


def create_match(chat_id: int, created_by: int) -> tuple[int, bool]:
    active = get_open_match(chat_id)
    if active:
        return int(active["id"]), False

    cursor = db.execute(
        """
        INSERT INTO matches (chat_id, created_by, match_date, status, created_at)
        VALUES (?, ?, ?, 'open', ?)
        """,
        (chat_id, created_by, today_str(), now_iso()),
    )
    db.commit()
    return int(cursor.lastrowid), True


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
            f"{player.main_position} ⭐{player.overall_rating} 🧤{player.goalkeeper_skill}"
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

    return (
        f"👥 **Выбор игроков на матч #{match_id}**\n\n"
        f"Всего профилей в боте: **{total_players}**\n"
        f"Админ выбрал на игру: **{selected_count}**\n"
        f"Страница: **{page + 1}/{total_pages}**\n\n"
        "Нажимайте на игроков: ✅ выбран, ➕ не выбран.\n"
        "Обычные игроки сами записаться не могут."
    )


def format_players_list(match_id: int) -> str:
    playing = get_match_players(match_id, "playing")

    lines = [f"📋 **Список игроков на матч #{match_id}**\n"]
    lines.append(f"Админ выбрал: **{len(playing)}**")

    if playing:
        for index, player in enumerate(playing, 1):
            username = f" @{player.username}" if player.username else ""
            lines.append(
                f"{index}. {player.name}{username} — {position_text(player.main_position)} — "
                f"⭐ {player.overall_rating} — 🧤 {player.goalkeeper_skill}"
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


def balance_teams(players: list[Player], shuffle: bool = False) -> tuple[list[Player], list[Player]]:
    if len(players) < 2:
        return players, []

    rng = random.Random()
    if not shuffle:
        rng.seed(42)

    players = players[:]
    rng.shuffle(players)

    team_a: list[Player] = []
    team_b: list[Player] = []

    max_a = (len(players) + 1) // 2
    max_b = len(players) // 2

    # 1) Сначала выбираем вратарей или запасных вратарей.
    gk_candidates = [
        p for p in players
        if p.goalkeeper_willingness > 0 or p.main_position == "GK" or p.second_position == "GK"
    ]
    gk_candidates.sort(key=goalkeeper_score, reverse=True)

    selected_gks: list[Player] = []
    for candidate in gk_candidates:
        if len(selected_gks) == 2:
            break
        if candidate.goalkeeper_willingness == 0 and candidate.main_position != "GK":
            continue
        selected_gks.append(candidate)

    # Если есть 2 подходящих — разводим по разным командам.
    if selected_gks:
        team_a.append(selected_gks[0])
    if len(selected_gks) > 1:
        team_b.append(selected_gks[1])

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
        penalty_a = assignment_penalty(team_a, player, max_a)
        penalty_b = assignment_penalty(team_b, player, max_b)

        # Добавляем поправку: команда с меньшей суммой рейтинга получает преимущество.
        total_a = team_metrics(team_a)["total"]
        total_b = team_metrics(team_b)["total"]
        penalty_a += max(0, total_a - total_b) * 0.8
        penalty_b += max(0, total_b - total_a) * 0.8

        if penalty_a <= penalty_b:
            team_a.append(player)
        else:
            team_b.append(player)

    # 3) Если получилось заметно неравно по силе, пробуем пару простых обменов.
    for _ in range(20):
        total_a = team_metrics(team_a)["total"]
        total_b = team_metrics(team_b)["total"]
        diff = abs(total_a - total_b)

        if diff <= 5:
            break

        stronger = team_a if total_a > total_b else team_b
        weaker = team_b if total_a > total_b else team_a

        best_swap = None
        best_diff = diff

        for a_player in stronger:
            for b_player in weaker:
                # Не меняем выбранных вратарей, чтобы не потерять баланс ворот.
                if a_player in selected_gks or b_player in selected_gks:
                    continue

                new_stronger_total = (
                    total_a - a_player.overall_rating + b_player.overall_rating
                    if stronger is team_a
                    else total_b - a_player.overall_rating + b_player.overall_rating
                )
                new_weaker_total = (
                    total_b - b_player.overall_rating + a_player.overall_rating
                    if weaker is team_b
                    else total_a - b_player.overall_rating + a_player.overall_rating
                )
                new_diff = abs(new_stronger_total - new_weaker_total)

                if new_diff < best_diff:
                    best_diff = new_diff
                    best_swap = (a_player, b_player)

        if not best_swap:
            break

        a_player, b_player = best_swap
        stronger.remove(a_player)
        stronger.append(b_player)
        weaker.remove(b_player)
        weaker.append(a_player)

    return team_a, team_b


def format_team(name: str, team: list[Player]) -> str:
    metrics = team_metrics(team)
    lines = [f"**{name}**"]
    for index, player in enumerate(team, 1):
        gk_mark = " 🧤" if player.main_position == "GK" or player.second_position == "GK" or player.goalkeeper_skill >= 60 else ""
        lines.append(
            f"{index}. {player.name}{gk_mark} — {position_text(player.main_position)} — "
            f"⭐ {player.overall_rating} — 🧤 {player.goalkeeper_skill}"
        )

    lines.append(
        "\n"
        f"Средний рейтинг: **{metrics['overall']}**\n"
        f"Атака: **{metrics['attack']}** | Защита: **{metrics['defense']}** | "
        f"Скорость: **{metrics['speed']}** | Ворота: **{metrics['gk']}**"
    )
    return "\n".join(lines)


def format_balanced_teams(team_a: list[Player], team_b: list[Player]) -> str:
    metrics_a = team_metrics(team_a)
    metrics_b = team_metrics(team_b)
    diff = abs(metrics_a["total"] - metrics_b["total"])

    return (
        "⚽ **Автоматическое деление команд**\n\n"
        f"{format_team('🔴 Команда A', team_a)}\n\n"
        f"{format_team('🔵 Команда B', team_b)}\n\n"
        f"📊 Разница общей силы: **{diff}**\n"
        "🧤 Ворота учтены: бот сначала разводит основных/запасных вратарей по разным командам."
    )


@app.on_message(filters.command("start"))
async def start_handler(_: Client, message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        text = (
            "⚽ Привет! Я бот для футбольной группы.\n\n"
            "Игроки могут только создать или обновить профиль:\n"
            "/new_profile — создать или обновить FIFA-профиль\n"
            "/profile — посмотреть свой профиль\n\n"
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
    await message.reply_text("Ок, анкету отменил.")


@app.on_message(filters.private & filters.command("profile"))
async def profile_handler(_: Client, message: Message) -> None:
    player = get_player(message.from_user.id)
    if not player:
        await message.reply_text("Профиля пока нет. Создай его командой /new_profile")
        return

    await message.reply_text(format_player_profile(player))


@app.on_message(filters.private & filters.text & ~filters.command(["start", "new_profile", "cancel", "profile"]))
async def profile_text_handler(_: Client, message: Message) -> None:
    user_id = message.from_user.id
    session = profile_sessions.get(user_id)
    if not session:
        await message.reply_text("Чтобы создать профиль, напиши /new_profile")
        return

    step_index = session["step"]
    field, _question = PROFILE_STEPS[step_index]
    raw_value = message.text or ""

    try:
        if field == "name":
            value = raw_value.strip()
            if len(value) < 2:
                raise ValueError("Имя слишком короткое.")
        elif field in {"main_position", "second_position"}:
            value = normalize_position(raw_value, allow_none=(field == "second_position"))
        elif field == "goalkeeper_willingness":
            value = parse_willingness(raw_value)
        else:
            value = clamp_rating(raw_value)
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

    match_id, created = create_match(message.chat.id, message.from_user.id)
    if created:
        header = f"⚽ **Матч #{match_id} создан**"
    else:
        header = f"⚽ **Уже есть открытый матч #{match_id}**"

    await message.reply_text(
        f"{header}\n\n"
        "Игроки сами не записываются.\n"
        "Только админ выбирает список игроков из готовых профилей.\n\n"
        "Нажимайте на игроков ниже или используйте команду /select_players.",
        reply_markup=selection_keyboard(match_id, page=0),
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


@app.on_message(filters.group & filters.command("balance"))
async def balance_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    active = get_open_match(message.chat.id)
    if not active:
        await message.reply_text("Пока нет открытого матча. Сначала /create_match")
        return

    players = get_match_players(active["id"], "playing")
    if len(players) < 4:
        await message.reply_text("Нужно хотя бы 4 игрока, чтобы нормально поделить команды.")
        return

    team_a, team_b = balance_teams(players, shuffle=False)
    await message.reply_text(format_balanced_teams(team_a, team_b))


@app.on_message(filters.group & filters.command("shuffle"))
async def shuffle_handler(_: Client, message: Message) -> None:
    if not await require_admin(message):
        return

    active = get_open_match(message.chat.id)
    if not active:
        await message.reply_text("Пока нет открытого матча. Сначала /create_match")
        return

    players = get_match_players(active["id"], "playing")
    if len(players) < 4:
        await message.reply_text("Нужно хотя бы 4 игрока, чтобы нормально поделить команды.")
        return

    team_a, team_b = balance_teams(players, shuffle=True)
    await message.reply_text("🔄 Новый вариант:\n\n" + format_balanced_teams(team_a, team_b))


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
        await query.message.reply_text(
            f"✅ Список игроков на матч #{match_id} сохранен.\n"
            f"Выбрано игроков: **{selected_count}**\n\n"
            "Теперь админ может написать /balance"
        )
        await query.answer("Готово")
        return


if __name__ == "__main__":
    init_db()
    print("Football Team Bot started...")
    app.run()
