from django.urls import path, include

urlpatterns = [
    path("billing/api/", include("stapel_billing.urls_v1")),
]
