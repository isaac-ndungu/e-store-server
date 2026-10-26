# E-Commerce Platform (e-store)

Single-tenant, API-first, modular e-commerce backend for a Kenyan home
appliance business.

## Stack

- Django 6.1 
- Django REST Framework, django-filter
- PostgreSQL 16, Redis (cache + Celery broker)
- Celery + Celery Beat
- Docker Compose for local infrastructure

## Quick start (local, PostgreSQL)

```bash
python -m venv env
source env/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
# Fill in DATABASE_URL (or the DB_* variables) and REDIS_URL.
python manage.py migrate
python manage.py runserver
```

The test runner swaps in in-memory SQLite unless `TEST_USE_POSTGRES=True` is
set. Set that flag to exercise the row-locking and concurrency paths against
PostgreSQL before deploying.

`requirements.txt` is the production set (what Docker and Render install);
`requirements-dev.txt` adds lint/format/audit/load-test tooling on top.

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
| `DB_ENGINE` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` / `DB_HOST` / `DB_PORT` | PostgreSQL connection (local dev / Compose) |
| `DATABASE_URL` | Single-URL Postgres connection (Neon pooled string); takes precedence over `DB_*` when set |
| `RENDER_EXTERNAL_HOSTNAME` | Auto-set by Render; trusted for hosts/CSRF without listing it in `ALLOWED_HOSTS` |
| `REDIS_URL` | General-purpose Redis handle; `CACHE_REDIS_URL` / `CELERY_*` fall back to it when unset |
| `CACHE_REDIS_URL` / `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | Django cache + Celery broker/backend (unset = use `REDIS_URL`) |
| `FRONTEND_URL` / `RESET_LINK_BASE` | Storefront URL + password-reset link target |
| `WHATSAPP_BUSINESS_NUMBER` / `ORDER_INTAKE_EMAIL` | Assisted-sales hand-off contact points |
| `CORS_ALLOWED_ORIGINS` / `CORS_ALLOW_CREDENTIALS` / `CSRF_TRUSTED_ORIGINS` | Browser origins allowed to call the API |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_USE_TLS` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` / `EMAIL_BACKEND` / `DEFAULT_FROM_EMAIL` | SMTP delivery + from-address |
| `SMS_PROVIDER` / `AT_USERNAME` / `AT_API_KEY` / `SMS_SENDER_ID` | Africa's Talking SMS sending |
| `NOTIFICATIONS_CALLBACK_IPS` / `NOTIFICATIONS_CALLBACK_SECRET` | SMS delivery-report callback trust |
| `SENTRY_DSN` / `SENTRY_TRACES_SAMPLE_RATE` | Error tracking (empty = disabled) |
| `CDN_DOMAIN`, `AWS_*` | CDN/object-storage placeholders (later)     |

## Deploying (Render + Neon)

1. Create a Neon project and copy the **pooled** connection string
   (`postgresql://...?sslmode=require`).
2. Push this repo to GitHub and create a new **Blueprint** on Render pointing
   at `render.yaml` (or create the services manually).
3. In the Render dashboard, set `DATABASE_URL` (Neon string) on the web,
   worker, and beat services, plus `FRONTEND_URL` / `CORS_ALLOWED_ORIGINS`
   pointing at the storefront. `SECRET_KEY` is generated; copy the same value
   to the worker and beat services. The beat service is required for periodic
   maintenance, smart-collection refreshes, and abandoned-cart sweeps.
4. Deploy. The build runs migrations, collects static files, and seeds the
   delivery areas and policy pages. Health is checked at `/health/`. API docs
   are at `/api/swagger/`, but require a staff login outside development.

The frontend only needs the web service URL as its API base URL
(e.g. `https://e-store-api.onrender.com`).

## Production data bootstrap

Start production from an empty PostgreSQL database and let migrations create
the schema. Never copy a local `db.sqlite3`, a development database dump, or
the `media/` folder to the server.

After migrations, load only the reference data the storefront needs on day
one:

```bash
python manage.py sync_delivery_areas
python manage.py seed_policy_pages
python manage.py createsuperuser
```

`SiteConfig` is created with safe defaults the first time it is read; fill in
the business name, KRA PIN, contact details, social links, and upload the logo
through the staff console. Both seed commands are idempotent and leave staff
edits untouched. Catalogue data is intentionally empty: enter real categories,
brands, and products through the staff console after launch.

Load-test data can be removed from a development database without touching
real rows:

```bash
python manage.py seed_load_test_data --wipe-only
```

Before deploying, run the production checks and a fresh-database rehearsal in
an environment that points at a disposable PostgreSQL database:

```bash
python manage.py check --deploy
python manage.py makemigrations --check --dry-run
python manage.py migrate
TEST_USE_POSTGRES=True python manage.py test
```

Production settings must include `DEBUG=False`, a fresh random `SECRET_KEY`,
an explicit `ALLOWED_HOSTS`, `FRONTEND_URL`, `CORS_ALLOWED_ORIGINS`, and
`CSRF_TRUSTED_ORIGINS` for cookie-based admin sessions. Set a real
`EMAIL_BACKEND` if password-reset mail must leave the server; the default is
console output outside tests.

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
