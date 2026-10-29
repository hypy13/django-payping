from django.urls import include, path

urlpatterns = [path("payping/", include("django_payping.urls"))]
