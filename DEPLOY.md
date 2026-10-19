# Развёртывание zagruziplease на fantasyvps

Сервер: `fantasyvps` (NL, 1 vCPU, 2 ГБ RAM), рядом стек **fantasy** — он важнее, поэтому у нас жёсткие лимиты и низкий `cpu_shares` (`docker-compose.fantasyvps.yml`). Каталог: `/root/my_projects/zagruziplease`. Бот `@zagruzipleasebot` работает через polling — вебхуков, доменов и nginx нет. Прокси не используется: сервер уже вне РФ, Telegram и yt-dlp ходят напрямую.

## Обычный деплой

Код на сервере — только `git pull` с GitHub, без ручного копирования файлов.

```bash
cd /root/my_projects/zagruziplease
git pull --ff-only origin main
./scripts/build-low-resource.sh
docker compose -f docker-compose.yml -f docker-compose.fantasyvps.yml up -d
```

`build-low-resource.sh` собирает образы через `nice`/`ionice` с `BUILDKIT_MAX_PARALLELISM=1`, чтобы не задушить fantasy. В Docker Compose v5 у `build` нет `--parallel` — не передавайте его.

## Секреты (не в git)

- `app_bot/config.yml` — `api_id`, `api_hash`, токен бота, список пользователей (шаблон: `app_bot/config-example.yml`).
- `envs/secrets.env` — `POSTGRES_PASSWORD`, `RABBITMQ_PASSWORD` (шаблон: `envs/secrets.env.example`).

При `git reset --hard` они не пропадут — файлы в `.gitignore`.

## Данные

| Том | Что |
|-----|-----|
| `zagruziplease_pgdata` | Postgres (задачи, файлы, кеш) |
| `zagruziplease_filestorage` | `/filestorage` воркера (обычно пусто: бот шлёт `save_to_storage=False`) |
| `zagruziplease_download_tmp` | Временные файлы yt-dlp — на диске, не tmpfs (RAM мало) |

Rabbitmq и redis — без ценных данных. У RabbitMQ `consumer_timeout` поднят до 6 ч (`rabbitmq/conf.d/`): воркер подтверждает задачу только после загрузки, и при стандартных 30 мин долгие загрузки запускались повторно. Бэкап БД:

```bash
docker exec zagruziplease_postgres pg_dump -U yt -Fc yt > zagruziplease-$(date +%F).dump
```

Порты наружу не открыты: API `127.0.0.1:1984`, Postgres `127.0.0.1:5435`, панель RabbitMQ `127.0.0.1:15672`.

## Лимиты

- **Один файл** yt-dlp: до ~1 GiB (`YTDLP_MAX_FILESIZE_BYTES` в `envs/worker.fantasyvps.env`; `0` — без лимита).
- **Одна загрузка** за раз (`MAX_SIMULTANEOUS_DOWNLOADS=1`).
- **Качество видео** выбирается кнопками в боте (360p / 480p / 720p / 1080p / лучшее / только звук; без выбора через 30 с — 1080p). Лимит файла — 2000 MiB (`YTDLP_MAX_FILESIZE_BYTES`). Принудительный ~360p для длинных роликов теперь только для задач без выбора (API) и по умолчанию выключен: `YTDLP_LONG_VIDEO_SECONDS=600`, `YTDLP_LONG_VIDEO_FORMAT`.
- **RAM/CPU** — `docker-compose.fantasyvps.yml` (`mem_limit`, `cpus`, `cpu_shares`).

## Healthcheck

`scripts/healthcheck-stack.sh` каждые 5 минут (cron, ставится `scripts/install-healthcheck-cron.sh`): перезапуск API при падении `/status`, перезапуск бота, если он завис в `Session.restart()`. Лог: `/var/log/zagruziplease-healthcheck.log`.

## Bunkr (bunkr.si и зеркала)

- Отдельная стадия перед yt-dlp: разбор страницы вида `https://bunkr.<tld>/f/…` (и `/v/`, `/i/`, `/d/`), вызов официального Bunkr API (`apidl.bunkr.ru`) и скачивание уже **прямой** ссылки через yt-dlp.
- Поддерживаются все известные домены из gallery-dl (`bunkr.si`, `.fi`, `.sk`, `.black`, …) плюс любой `bunkr+.<tld>` по шаблону; при ошибке на одном зеркале воркер перебирает другие.
- CDN Bunkr блокирует IP датацентров — при необходимости residential `BUNKR_CDN_PROXY=socks5h://…` в `envs/worker.fantasyvps.env`.

## VK Музыка

Публичного API для музыки у ВК нет, а токены из `oauth.vk.com/authorize` на `audio.*` отвечают «Unknown method». Поэтому бот и воркер притворяются веб-версией ВК: по кукам сессии (`remixsid` и `p`) получают на `login.vk.ru` токен веб-клиента (живёт около часа, обновляется сам), см. `yt_shared/vk_music.py`.

- Куки: залогиниться в ВК в браузере, закрыть браузер, запустить локально `python scripts/vk_music_cookies.py` (по умолчанию Яндекс Браузер; путь к профилю и браузер можно передать аргументами). Скрипт проверит поиск и запишет `VK_MUSIC_COOKIES=...` в `envs/secrets.env` — эту строку скопировать в `envs/secrets.env` на сервере и перезапустить `yt_bot` и `yt_worker`.
- Если ВК пишет «сессия недействительна» — сессию разлогинили (выход из ВК в браузере, «Завершить все сеансы», смена пароля). Повторить шаг с куками.
- Ссылка на плейлист (`vk.com/audio_playlist…`, `vk.com/music/playlist/…`) раскладывается на отдельные задачи по трекам, максимум `VK_MUSIC_MAX_TRACKS` (по умолчанию 200). Ссылки ВК всегда качаются как mp3, качество видео не спрашивается.
- `/music название` — поиск, 10 треков на страницу, сортировка по релевантности или длине, `/musicN` скачивает трек N из последнего поиска в этом чате.

## Удаление после отправки в Telegram

После обработки бот в `SuccessDownloadHandler._cleanup()` вызывает `remove_dir(self._body.media.root_path)` — каталог задачи с исходным видео и временными файлами удаляется в `finally` после загрузки в Telegram. Постоянных копий медиа на сервере нет.

## История

До 15.10.2026 проект назывался **zagruzisuka** и жил на egorvps (рядом kinodolgoletie, mihomo-прокси для обхода блокировок). Старый бот `@zagruzisukabot` заменён новым — `@zagruzipleasebot` (тот же код, новый токен в `app_bot/config.yml`).
