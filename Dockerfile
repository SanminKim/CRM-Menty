FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN chmod +x docker/entrypoint.sh \
    && useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000
ENTRYPOINT ["docker/entrypoint.sh"]
# Потоки вместо отдельных процессов: медленный запрос не занимает весь сервер. Журнал запросов ведёт Caddy,
# у gunicorn он выключен, чтобы токен формы заявок из адреса не попадал в журнал.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--worker-class", "gthread", "--threads", "4", "--timeout", "30"]
