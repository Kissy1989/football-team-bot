# Football Team Bot — Railway version

Telegram-бот для футбольной группы.

## Главное

- Игроки могут только создать или обновить свой профиль в личке бота.
- Матч создает только админ.
- Список игроков на матч выбирает только админ из готовых профилей.
- Админ может создать игрока вручную без Telegram-аккаунта.
- Админ может изменить характеристики любого игрока.
- Админ выбирает формат игры: количество команд и количество игроков в каждой команде.

## Команды игроков

В личке бота:

```text
/start
/new_profile
/profile
/cancel
```

## Команды админа

В группе:

```text
/create_match 2 7
/create_match 3 5
/teams 2 7
/select_players
/players_today
/balance
/shuffle
/reset_match
```

В личке бота или в группе:

```text
/admin_new_player
/edit_player
/profiles
/set_player ID поле значение
```

Примеры:

```text
/create_match 2 7
```

Создает игру: 2 команды по 7 игроков.

```text
/create_match 3 5
```

Создает игру: 3 команды по 5 игроков.

```text
/teams 2 6
```

Меняет формат текущей открытой игры.

```text
/admin_new_player
```

Админ вручную создает игрока, если человек не может/не хочет сам заполнить профиль.

```text
/edit_player
```

Открывает список игроков с кнопками. Админ выбирает игрока, затем характеристику и отправляет новое значение.

```text
/set_player -1 speed 75
```

Быстро меняет поле через команду.

Доступные поля для `/set_player`:

```text
name
main_position
second_position
speed
dribbling
passing
shooting
defense
stamina
physical
goalkeeper_skill
goalkeeper_willingness
```

## Важно про формат матча

Если админ поставил:

```text
/teams 2 7
```

значит бот будет ждать ровно 14 выбранных игроков.

Если выбрано 13 или 15, бот не будет делить команды и попросит добавить/убрать игроков или изменить формат.

## Railway Variables

```env
API_ID=...
API_HASH=...
BOT_TOKEN=...
ADMIN_IDS=123456789,987654321
DB_PATH=/data/football_bot.sqlite3
TIMEZONE=Asia/Tashkent
```

Для сохранения профилей после redeploy нужен Railway Volume с mount path:

```text
/data
```
