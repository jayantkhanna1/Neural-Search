"""WSGI config for the Neural Search project."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "neural_search.settings")

application = get_wsgi_application()
