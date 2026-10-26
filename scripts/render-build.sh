#!/usr/bin/env bash
# Render build step for the Django web service: install deps, collect static
# files, apply migrations, and seed the reference data the storefront needs on
# first boot. Both seed commands are idempotent and never overwrite staff edits.
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput
python manage.py sync_delivery_areas
python manage.py seed_policy_pages
