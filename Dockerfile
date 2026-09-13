# Backend image for the e-commerce platform.
# Build once, reuse for the web, worker, and beat services.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /code

# System deps needed to build wheels (psycopg, etc.).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better layer caching. Production-only deps keep
# the runtime image lean; dev tooling lives in requirements-dev.txt.
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Copy the application.
COPY . .

# Compile static assets at build time so the image is self-sufficient at boot.
# Dummy values satisfy settings import (no DB hit for collectstatic).
RUN SECRET_KEY=build-only DEBUG=False DB_ENGINE=django.db.backends.postgresql \
    DB_NAME=build DB_USER=build DB_PASSWORD=build DB_HOST=localhost DB_PORT=5432 \
    python manage.py collectstatic --noinput

EXPOSE 8000

# Render supplies $PORT; default to 8000 for Compose/VPS runs.
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 3"]
