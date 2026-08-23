"""API tests for stapel-billing."""

import pytest

from stapel_billing import services
from stapel_billing.models import LotSource, Transaction, TransactionType, Wallet


@pytest.mark.django_db
class TestWallet:
    def test_get_wallet_creates_lazily(self, authed_client, user):
        assert not Wallet.objects.filter(user=user).exists()
        resp = authed_client.get("/billing/api/wallet")
        assert resp.status_code == 200
        assert resp.json()["balance"] == 0
        assert Wallet.objects.filter(user=user).exists()

    def test_patch_settings(self, authed_client, user):
        resp = authed_client.patch(
            "/billing/api/wallet",
            {
                "auto_recharge_enabled": True,
                "auto_recharge_threshold": 100,
                "auto_recharge_package": "starter",
                "low_balance_alert": 200,
            },
            format="json",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["auto_recharge_enabled"] is True
        assert data["auto_recharge_package"] == "starter"

    def test_invalid_auto_recharge_package(self, authed_client, user):
        resp = authed_client.patch(
            "/billing/api/wallet",
            {"auto_recharge_package": "nonexistent"},
            format="json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestWalletService:
    def test_credit_and_debit(self, user):
        services.credit(
            user=user,
            credits=100,
            type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
        )
        services.debit(
            user=user, credits=30, type=TransactionType.TRANSCRIPTION_CHARGE
        )
        wallet = Wallet.objects.get(user=user)
        assert wallet.balance == 70
        assert Transaction.objects.filter(wallet=wallet).count() == 2

    def test_debit_insufficient(self, user):
        services.credit(
            user=user,
            credits=10,
            type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
        )
        with pytest.raises(services.InsufficientCreditsError):
            services.debit(
                user=user, credits=100, type=TransactionType.TRANSCRIPTION_CHARGE
            )


@pytest.mark.django_db
class TestCatalog:
    def test_catalog_lists_packages_and_plans(self, api_client):
        resp = api_client.get("/billing/api/products")
        assert resp.status_code == 200
        body = resp.json()
        slugs = {p["slug"] for p in body["packages"]}
        assert slugs >= {"starter", "standard", "bulk"}
        plan_slugs = {p["slug"] for p in body["plans"]}
        assert plan_slugs >= {"free", "pro", "team", "enterprise"}


@pytest.mark.django_db
class TestCheckout:
    def test_checkout_placeholder_when_stripe_disabled(
        self, authed_client, user, stripe_placeholders_allowed
    ):
        # The redirect targets ride on the deployment's own frontend origin:
        # request-supplied ones are allowlisted (redirects.py), and this test
        # is about the placeholder checkout URL, not about that gate.
        resp = authed_client.post(
            "/billing/api/checkout",
            {
                "package": "starter",
                "success_url": "https://front.example/ok",
                "cancel_url": "https://front.example/no",
            },
            format="json",
        )
        assert resp.status_code == 200
        assert "example.invalid" in resp.json()["checkout_url"]

    def test_checkout_requires_one_target(self, authed_client, user):
        resp = authed_client.post("/billing/api/checkout", {}, format="json")
        assert resp.status_code == 400


@pytest.mark.django_db
class TestInternalDebit:
    def test_service_can_debit(self, api_client, user, settings):
        settings.SERVICE_API_KEY = "test-service-key"
        services.credit(
            user=user,
            credits=500,
            type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
        )
        resp = api_client.post(
            "/billing/api/internal/debit",
            {
                "user_id": str(user.id),
                "credits": 100,
                "type": "transcription_charge",
                "description": "test",
            },
            format="json",
            HTTP_X_API_KEY="test-service-key",
        )
        assert resp.status_code == 200, resp.content
        assert resp.json()["balance_after"] == 400

    def test_service_debit_insufficient(self, api_client, user, settings):
        settings.SERVICE_API_KEY = "test-service-key"
        services.credit(
            user=user,
            credits=10,
            type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
        )
        resp = api_client.post(
            "/billing/api/internal/debit",
            {
                "user_id": str(user.id),
                "credits": 100,
                "type": "transcription_charge",
            },
            format="json",
            HTTP_X_API_KEY="test-service-key",
        )
        assert resp.status_code == 402

    def test_no_api_key_rejected(self, api_client, user, settings):
        settings.SERVICE_API_KEY = "test-service-key"
        resp = api_client.post(
            "/billing/api/internal/debit",
            {"user_id": str(user.id), "credits": 1, "type": "ai_charge"},
            format="json",
        )
        assert resp.status_code in (401, 403)
