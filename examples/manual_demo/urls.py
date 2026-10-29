from django.urls import include, path

from examples.manual_demo import views

urlpatterns = [
    path("", views.checkout_view, name="demo-checkout"),
    path("payping/", include("django_payping.urls")),
]
