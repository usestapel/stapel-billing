"""Serializer seam: a host project can swap serializers by subclassing."""

import pytest
from django.test import override_settings
from django.urls import path

from stapel_billing.serializers import (
    WalletResponseSerializer,
    WalletUpdateRequestSerializer,
)
from stapel_billing.views import WalletView


class BrandedWalletSerializer(WalletResponseSerializer):
    """Host-project serializer adding a computed field."""

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["display_balance"] = f"{instance.balance} credits"
        return data


class BrandedWalletView(WalletView):
    response_serializer_class = BrandedWalletSerializer


urlpatterns = [
    path("custom/wallet", BrandedWalletView.as_view(), name="custom-wallet"),
]


def test_getters_return_the_overridden_classes():
    view = BrandedWalletView()
    assert view.get_response_serializer_class() is BrandedWalletSerializer
    # untouched seam falls back to the parent's default
    assert view.get_request_serializer_class() is WalletUpdateRequestSerializer


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="tests.test_serializer_seams")
def test_subclassed_view_uses_swapped_response_serializer(authed_client):
    resp = authed_client.get("/custom/wallet")
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_balance"] == "0 credits"  # added by the swapped serializer
    assert body["balance"] == 0  # base payload still intact
