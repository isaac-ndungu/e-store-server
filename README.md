# E-Commerce Platform (e-store)

Single-tenant, API-first, modular e-commerce backend for a Kenyan home
appliance business.

## Stack

- Django 6.1 
- Django REST Framework, django-filter
- PostgreSQL 16, Redis (cache + Celery broker)
- Celery + Celery Beat
- Docker Compose for local infrastructure

## Quick start (local, SQLite)

```bash
python -m venv env
source env/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver
```

## Quick start (Docker, full stack)

```bash
cp .env.example .env
docker compose up --build
```

- Backend: http://localhost:8000
- Postgres: localhost:5432
- Redis: localhost:6379

Create the database schema once inside the running web container:

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

## Settings & environments

All settings live in a single file, `config/settings.py`. `DEBUG` distinguishes
development from production; test mode is auto-detected from `manage.py test`
and swaps in in-memory SQLite, a local-memory cache, and eager Celery so tests
need no external services.

| Variable         | Purpose                                        |
|------------------|------------------------------------------------|
| `DEBUG`          | `True` (dev, default) / `False` (production)   |
| `SECRET_KEY`     | Required when `DEBUG=False`                    |
| `DB_ENGINE` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` / `DB_HOST` / `DB_PORT` | PostgreSQL connection |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | Celery broker/backend (Redis) |
| `REDIS_URL` / `CACHE_REDIS_URL` | Redis client + Django cache   |
| `CORS_ALLOWED_ORIGINS` | Origins allowed to call the API            |
| `CDN_DOMAIN`, `AWS_*` | CDN/object-storage placeholders (later)     |

## API collection (Postman)

- `postman/ecommerce-platform.postman_collection.json`

Import this single file. Its collection-level variables (baseUrl, tokens, test
phones) provide the API environment, so no separate environment file is needed.
Folders are added per implementation step (§12).

## Background tasks

Workers are run by the `worker` and `beat` Compose services, or locally:

```bash
celery -A config worker --loglevel=info
celery -A config beat --loglevel=info
```
