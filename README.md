# Optics Copilot

Контент-пайплайн и чат-бот для дистрибьютора оптики и электроники. Пайплайн забирает новости, карточки товаров и PDF, переводит их и публикует в WordPress. Бот отвечает по опубликованным материалам: в Telegram и через `POST /chat`. Админка на Streamlit — очередь ревью, публикация и переключение режима.

## Локальный запуск

Нужны Python 3.12 и [uv](https://docs.astral.sh/uv/).

```bash
uv sync
copy .env.example .env
```

На Linux и macOS вместо `copy` используйте `cp .env.example .env`. Заполните ключи в `.env`.

```bash
uv run pytest
uv run ruff check .
uv run python -m pipeline run
uv run python -m pipeline schedule
uv run uvicorn bot.api:app --host 127.0.0.1 --port 8000
uv run python -m bot.telegram
uv run streamlit run admin/main.py
```

`pipeline run` делает один прогон. `pipeline schedule` повторяет его каждые `PIPELINE_INTERVAL_HOURS` часов. Админка открывается на http://127.0.0.1:8501, чат API — на http://127.0.0.1:8000.

Те же проверки через Makefile, если установлен `make`: `make test` и `make lint`.

## Деплой на один VPS

На сервере нужны Docker и плагин Compose. Порты API и админки в `docker-compose.yml` слушаются только на `127.0.0.1`. Снаружи их отдаёт Caddy или nginx с HTTPS.

```bash
git clone <адрес-репозитория> optics-copilot
cd optics-copilot
cp .env.example .env
make up
make logs
```

`make up` — это `docker compose up -d --build`. Поднимаются четыре сервиса из одного образа:

| Сервис | Что делает |
|---|---|
| `pipeline` | `python -m pipeline schedule` |
| `bot-api` | uvicorn на порту 8000 |
| `telegram-bot` | long polling Telegram |
| `admin` | Streamlit на порту 8501 |

SQLite, Chroma и `pipeline.log` лежат в общем томе `app-data`, внутри контейнеров это каталог `/data`. Compose сам подставляет `DATABASE_URL`, `CHROMA_PERSIST_DIR` и `PIPELINE_LOG_PATH`, поэтому локальные пути из `.env` в контейнер не попадают.

Пример Caddy (`/etc/caddy/Caddyfile`):

```caddy
bot.example.com {
    reverse_proxy 127.0.0.1:8000
}

admin.example.com {
    reverse_proxy 127.0.0.1:8501
}
```

Тот же маршрут в nginx:

```nginx
server {
    server_name bot.example.com;
    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

Сертификат для nginx удобно выпустить через certbot. После правки `.env` перезапустите контейнеры: `docker compose up -d`.

### Мониторинг

`GET /health` у `bot-api` проверяет, что процесс жив и база отвечает, и возвращает `{"status":"ok"}`. Повесьте на публичный адрес внешнюю проверку, например [Uptime Kuma](https://github.com/louislam/uptime-kuma) на том же VPS или бесплатный [UptimeRobot](https://uptimerobot.com/):

`https://bot.example.com/health`, интервал 5 минут, ожидается код 200.

Docker дополнительно проверяет тот же адрес изнутри сервиса `bot-api`.

## Режим работы

`manual` останавливает перевод на ревью. Публикация — кнопка в админке. `auto` после успешного перевода сам создаёт пост в WordPress и кладёт текст в индекс.

Переключается в админке, страница «Очередь ревью», переключатель «Режим пайплайна». Автоматический режим включается отдельной кнопкой. Выбор пишется в базу и переживает перезапуск. `PIPELINE_MODE` в `.env` используется только пока в базе ещё нет этой настройки.

## Логи

```bash
make logs
docker compose logs -f pipeline
docker compose logs -f bot-api
docker compose exec pipeline tail -n 100 /data/pipeline.log
```

`make logs` показывает последние строки всех сервисов и продолжает их печатать. Журнал прогонов пайплайна — файл `/data/pipeline.log` в общем томе. Последние ошибки прогона также видны на странице «Дашборд» в админке.
