# Football Team Bot — Railway версия

Бот для футбольной группы Telegram.

## Новая логика

1. Игру может создать только админ.
2. Обычные игроки могут только создать/обновить профиль в личке бота.
3. Список игроков на матч выбирает только админ из уже созданных профилей.
4. Админов может быть несколько.

## Команды игроков

В личке бота:

```text
/start
/new_profile
/profile
/cancel
```

## Команды админа в группе

```text
/create_match      — создать матч
/select_players    — выбрать игроков кнопками из всех профилей
/profiles          — список всех профилей
/players_today     — список выбранных игроков на матч
/add_player ID     — добавить игрока вручную
/remove_player ID  — убрать игрока вручную
/clear_players     — очистить список игроков матча
/balance           — поделить выбранных игроков на команды
/shuffle           — новый вариант деления
/reset_match       — закрыть текущий матч
```

## Как выбрать игроков

1. Админ пишет в группе:

```text
/create_match
```

2. Бот показывает список всех игроков с кнопками.

3. Админ нажимает на игроков:
- `➕` — игрок не выбран, нажмите чтобы добавить.
- `✅` — игрок выбран, нажмите чтобы убрать.

4. После выбора админ пишет:

```text
/balance
```

## Несколько админов

В Railway → Variables:

```env
ADMIN_IDS=123456789,987654321,555555555
```

Также бот считает админами тех, кто является админом Telegram-группы.

## Railway Variables

```env
API_ID=ваш_api_id
API_HASH=ваш_api_hash
BOT_TOKEN=токен_бота_от_BotFather
ADMIN_IDS=ваш_telegram_id,telegram_id_второго_админа
DB_PATH=/data/football_bot.sqlite3
TIMEZONE=Asia/Tashkent
```

## Важно для Railway

Подключите Volume с Mount Path:

```text
/data
```

Иначе SQLite-база может пропасть после redeploy.

## Start command

В `railway.json` уже указано:

```json
{
  "deploy": {
    "startCommand": "python bot.py"
  }
}
```
