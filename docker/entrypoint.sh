#!/bin/sh
# При каждом запуске контейнера применяем новые миграции базы
set -e
python manage.py migrate --noinput
# Оплаченные раньше платежи запоминают своё направление; повторный запуск ничего не меняет
python manage.py pin_payments || true
# Telegram-бот: если токен задан, сообщаем Telegram адрес сервера. Сбой не мешает запуску CRM
python manage.py telegram_setup || true
exec "$@"
