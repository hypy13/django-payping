from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class DjangoPayPingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_payping"
    verbose_name = _("PayPing payments")
