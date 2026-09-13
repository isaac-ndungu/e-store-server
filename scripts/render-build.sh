#!/usr/bin/env bash
# Render build step for the Django web service: install deps, collect static
# files, and apply migrations against the Neon database.
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput
