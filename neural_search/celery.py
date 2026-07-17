"""Celery application for the Neural Search project."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "neural_search.settings")

app = Celery("neural_search")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
