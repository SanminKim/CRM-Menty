#!/bin/sh
# При каждом запуске контейнера применяем новые миграции базы
set -e
python manage.py migrate --noinput
# Telegram-бот: если токен задан, сообщаем Telegram адрес сервера. Сбой не мешает запуску CRM
python manage.py telegram_setup || true
exec "$@"
