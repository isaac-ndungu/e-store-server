"""Celery application for the e-commerce platform.

Task workers and the Beat scheduler run as Docker Compose services (see
docker-compose.yml). Celery task modules live in each app (apps/<name>/tasks.py)
and are auto-discovered; their periodic schedules register in beat_schedule.

Contract: every task defined downstream must be idempotent and safe to run twice,
because Celery may redeliver a task after a worker failure (see AGENTS.md).
"""
import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('ecommerce')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
