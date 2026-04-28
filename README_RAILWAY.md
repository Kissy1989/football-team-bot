# Football Team Bot для Railway ⚽

Это Railway-версия Telegram-бота для футбольной группы.

## Команда запуска на Railway

Railway сам увидит файл `railway.json` и запустит:

```bash
python bot.py
```

## Переменные Railway

В Railway → ваш Service → Variables добавьте:

```env
API_ID=...
API_HASH=...
BOT_TOKEN=...
ADMIN_IDS=...
DB_PATH=/data/football_bot.sqlite3
TIMEZONE=Asia/Tashkent
```

## Volume

Чтобы игроки и матчи не пропали после Redeploy:

1. Откройте ваш сервис в Railway.
2. Перейдите в Settings или Storage/Volumes.
3. Add Volume.
4. Mount Path: `/data`.
5. В Variables поставьте `DB_PATH=/data/football_bot.sqlite3`.

## Команды бота

Игроки в личке:

```text
/start
/new_profile
/profile
/cancel
```

Админ в группе:

```text
/create_match
/players_today
/balance
/shuffle
/reset_match
```
