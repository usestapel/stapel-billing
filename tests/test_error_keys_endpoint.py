"""The error-keys endpoint is actually mounted (not just declared).

``BillingErrorKeysView`` existed since the port but no ``urls*.py`` mounted
it — in *any* stapel library. stapel-translate's ``error_collector`` polls
``/{prefix}/api/v1/error-keys/`` on every service, so the entire endpoint
class was a 404 from Django's URL resolver and the collector silently
harvested nothing. These assertions fail if the route is dropped again.
"""

import pytest
from django.urls import resolve, reverse
from rest_framework.test import APIClient

from stapel_billing.errors import BILLING_ERRORS, BillingErrorKeysView


def test_route_is_mounted_and_resolves_to_the_view():
    path = reverse("error-keys")
    assert path.endswith("/error-keys/")
    assert resolve(path).func.view_class is BillingErrorKeysView


@pytest.mark.django_db
def test_anonymous_is_denied():
    resp = APIClient().get(reverse("error-keys"))
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_staff_gets_the_registry_as_json():
    from django.contrib.auth import get_user_model

    staff = get_user_model().objects.create_user(
        username="errkeys-staff",
        email="errkeys-staff@example.com",
        password="testpass-1234",
        is_staff=True,
    )
    client = APIClient()
    client.force_authenticate(user=staff)

    resp = client.get(reverse("error-keys"))

    assert resp.status_code == 200
    # A view answer, not the URL resolver's HTML 404 — the distinction the
    # collector relies on (stapel_core.django.peers.service_answered).
    assert resp["Content-Type"].split(";")[0] == "application/json"
    payload = resp.json()
    assert isinstance(payload, dict)
    assert set(BILLING_ERRORS) <= set(payload)
