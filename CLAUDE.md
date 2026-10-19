# zagruziplease

Telegram-бот `@zagruzipleasebot` для скачивания видео и музыки (форк yt-dlp-bot): `app_bot` (pyrogram), `app_worker` (yt-dlp), `app_api`, общий пакет `yt_shared`, RabbitMQ + Postgres + Redis в docker compose.

## Рабочий процесс

- Любую законченную правку сразу коммитить, пушить в `main` и деплоить на сервер — не ждать отдельной просьбы. Отдельных веток и PR нет.
- Секреты (`envs/secrets.env`, `app_bot/config.yml`) не в git; новые секреты дописывать и локально, и на сервере.
- Правки в `yt_shared` попадают в образы бота, воркера и API — после них пересобирать всё.

## Сервер

- `ssh fantasyvps` (root@195.133.9.109, ключ `~/.ssh/id_ed25519_fantasyvps`), NL. Каталог `/root/my_projects/zagruziplease`.
- Рядом крутится стек fantasy, он важнее: у наших контейнеров жёсткие лимиты в `docker-compose.fantasyvps.yml`, сборка только через `./scripts/build-low-resource.sh`.
- Деплой:
  ```bash
  ssh fantasyvps 'cd /root/my_projects/zagruziplease && git pull --ff-only origin main && ./scripts/build-low-resource.sh && docker compose -f docker-compose.yml -f docker-compose.fantasyvps.yml up -d'
  ```
- Логи: `docker logs --since 10m zagruziplease_bot` / `zagruziplease_worker`.

Подробности (данные, лимиты, Bunkr, VK Музыка) — в `DEPLOY.md`.
