"""Local browser demo. Run on 127.0.0.1:8000."""

import os
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = "local-payment-demo-only"
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
PAYPING_RETURN_URL = os.environ.get("PAYPING_RETURN_URL", "")
if PAYPING_RETURN_URL:
    ALLOWED_HOSTS.append(urlsplit(PAYPING_RETURN_URL).hostname or "")
INSTALLED_APPS = ["django.contrib.contenttypes", "django_payping"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "demo.sqlite3",
    }
}
ROOT_URLCONF = "examples.manual_demo.urls"
MIDDLEWARE = ["django.middleware.csrf.CsrfViewMiddleware"]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "OPTIONS": {"context_processors": ["django.template.context_processors.csrf"]},
    }
]
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
PAYPING_TOKEN = os.environ.get("PAYPING_TOKEN", "")
PAYPING_RESULT_HANDLER = "examples.manual_demo.views.payment_result"
