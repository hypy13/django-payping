from django.conf import settings
from django.urls import path
from django.utils.module_loading import import_string

from django_payping import views

app_name = "payping"

callback_class = getattr(settings, "PAYPING_CALLBACK_VIEW", None)
if isinstance(callback_class, str):
    callback_class = import_string(callback_class)

urlpatterns = [
    path(
        "callback/",
        callback_class.as_view() if callback_class else views.payment_callback_view,
        name="callback",
    ),
]
