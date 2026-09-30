#!/bin/sh
# При каждом запуске контейнера применяем новые миграции базы
set -e
python manage.py migrate --noinput
exec "$@"
