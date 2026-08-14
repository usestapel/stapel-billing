"""Endpoint smokes: success + auth failure + validation failure per view."""

import pytest

from stapel_billing import services
from stapel_billing.conf import billing_settings
from stapel_billing.models import Subscription, TransactionType, Wallet
from stapel_billing.providers.base import PaymentProvider


@pytest.fixture(autouse=True)
def _reset_billing_settings():
    yield
    billing_settings.reload()


class FailingCancelProvider(PaymentProvider):
    name = "failing-cancel"

    def create_checkout_session(self, *, user, package, plan, success_url, cancel_url):
        return ("https://x", "cs_1")

    def create_portal_session(self, *, customer_id, return_url):
        return "https://x/portal"

    def cancel_subscription(self, subscription_id):
        raise RuntimeError("provider is down")

    def verify_webhook(self, payload, signature):
        return {}


FAILING_PATH = f"{FailingCancelProvider.__module__}.{FailingCancelProvider.__qualname__}"


@pytest.mark.django_db
class TestWalletEndpoint:
    def test_wallet_requires_auth(self, api_client):
        assert api_client.get("/billing/api/wallet").status_code in (401, 403)
        assert api_client.patch(
            "/billing/api/wallet", {}, format="json"
        ).status_code in (401, 403)

    def test_patch_persists_all_settings(self, authed_client, user):
        resp = authed_client.patch(
            "/billing/api/wallet",
            {
                "auto_recharge_enabled": True,
                "auto_recharge_threshold": 50,
                "auto_recharge_package": "bulk",
                "low_balance_alert": 25,
            },
            format="json",
        )
        assert resp.status_code == 200
        wallet = Wallet.objects.get(user=user)
        assert wallet.auto_recharge_enabled is True
        assert wallet.auto_recharge_threshold == 50
        assert wallet.auto_recharge_package == "bulk"
        assert wallet.low_balance_alert == 25

    def test_patch_rejects_unknown_package_without_saving(self, authed_client, user):
        resp = authed_client.patch(
            "/billing/api/wallet",
            {"auto_recharge_package": "no-such-package", "low_balance_alert": 9},
            format="json",
        )
        assert resp.status_code == 400
        wallet = Wallet.objects.get(user=user)
        assert wallet.auto_recharge_package is None
        assert wallet.low_balance_alert == 0

    def test_patch_rejects_non_integer_threshold(self, authed_client):
        resp = authed_client.patch(
            "/billing/api/wallet",
            {"auto_recharge_threshold": "lots"},
            format="json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestTransactionListEndpoint:
    def test_requires_auth(self, api_client):
        assert api_client.get("/billing/api/wallet/transactions").status_code in (401, 403)

    def test_lists_transactions_newest_first(self, authed_client, user):
        services.credit(user=user, credits=100, type=TransactionType.CREDIT_PURCHASE)
        services.debit(
            user=user,
            credits=40,
            type=TransactionType.AI_CHARGE,
            description="ai job",
        )
        resp = authed_client.get("/billing/api/wallet/transactions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_more"] is False
        txns = body["transactions"]
        assert len(txns) == 2
        assert txns[0]["type"] == "ai_charge"
        assert txns[0]["credits_delta"] == -40
        assert txns[0]["balance_after"] == 60
        assert txns[0]["description"] == "ai job"
        assert txns[1]["type"] == "credit_purchase"
        assert txns[1]["balance_after"] == 100

    def test_empty_wallet_lists_nothing(self, authed_client):
        resp = authed_client.get("/billing/api/wallet/transactions")
        assert resp.status_code == 200
        assert resp.json()["transactions"] == []


@pytest.mark.django_db
class TestCheckoutValidation:
    def test_requires_auth(self, api_client):
        resp = api_client.post(
            "/billing/api/checkout", {"package": "starter"}, format="json"
        )
        assert resp.status_code in (401, 403)

    def test_rejects_both_package_and_plan(self, authed_client):
        resp = authed_client.post(
            "/billing/api/checkout",
            {"package": "starter", "plan": "pro"},
            format="json",
        )
        assert resp.status_code == 400

    def test_rejects_unknown_plan(self, authed_client):
        resp = authed_client.post(
            "/billing/api/checkout", {"plan": "diamond"}, format="json"
        )
        assert resp.status_code == 400

    def test_plan_checkout_placeholder_when_stripe_disabled(
        self, authed_client, stripe_placeholders_allowed
    ):
        resp = authed_client.post(
            "/billing/api/checkout", {"plan": "pro"}, format="json"
        )
        assert resp.status_code == 200
        assert "plan/pro" in resp.json()["checkout_url"]
        assert resp.json()["session_id"] == "cs_dev_pro"


@pytest.mark.django_db
class TestCustomerPortalEndpoint:
    def test_requires_auth(self, api_client):
        assert api_client.get("/billing/api/portal").status_code in (401, 403)

    def test_portal_without_subscription(
        self, authed_client, stripe_placeholders_allowed
    ):
        resp = authed_client.get("/billing/api/portal")
        assert resp.status_code == 200
        assert resp.json()["portal_url"] == (
            "https://example.invalid/stripe-portal-not-configured"
        )

    def test_portal_with_subscription_but_unconfigured_stripe(
        self, authed_client, user, stripe_placeholders_allowed, settings
    ):
        Subscription.objects.create(user=user, stripe_customer_id="cus_77")
        # A request-supplied return URL has to name an origin the deployment
        # declared (redirects.py); this test is about the placeholder portal
        # URL an unconfigured Stripe returns when the deployment opted into
        # placeholders (BILL-06).
        settings.STAPEL_BILLING = {
            "REDIRECT_ALLOWED_ORIGINS": ["https://back.example"],
            "ALLOW_UNCONFIGURED_PAYMENT_PROVIDER": True,
        }
        resp = authed_client.get(
            "/billing/api/portal", {"return_url": "https://back.example"}
        )
        assert resp.status_code == 200
        assert "example.invalid" in resp.json()["portal_url"]


@pytest.mark.django_db
class TestSubscriptionEndpoint:
    def test_requires_auth(self, api_client):
        assert api_client.get("/billing/api/subscription").status_code in (401, 403)

    def test_get_creates_default_subscription(self, authed_client, user):
        assert not Subscription.objects.filter(user=user).exists()
        resp = authed_client.get("/billing/api/subscription")
        assert resp.status_code == 200
        body = resp.json()
        assert body["plan"] == "free"
        assert body["status"] == "active"
        assert body["cancelled_at"] is None
        assert Subscription.objects.filter(user=user).count() == 1

    def test_get_returns_existing_subscription(self, authed_client, user):
        Subscription.objects.create(user=user, plan="team", status="past_due")
        resp = authed_client.get("/billing/api/subscription")
        assert resp.json()["plan"] == "team"
        assert resp.json()["status"] == "past_due"
        assert Subscription.objects.filter(user=user).count() == 1


@pytest.mark.django_db
class TestSubscriptionCancelEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.post("/billing/api/subscription/cancel")
        assert resp.status_code in (401, 403)

    def test_cancel_without_subscription_404(self, authed_client):
        resp = authed_client.post("/billing/api/subscription/cancel")
        assert resp.status_code == 404

    def test_cancel_local_subscription(self, authed_client, user):
        sub = Subscription.objects.create(user=user, plan="pro")
        resp = authed_client.post("/billing/api/subscription/cancel")
        assert resp.status_code == 200
        assert resp.json()["cancelled_at"] is not None
        sub.refresh_from_db()
        assert sub.cancelled_at is not None

    def test_cancel_with_provider_no_op_when_unconfigured(
        self, authed_client, user, stripe_placeholders_allowed
    ):
        # Default StripeProvider without keys, with dev placeholders opted
        # into: the remote cancel is a no-op. Without that opt-in the cancel
        # refuses instead (BILL-06) — a local "cancelled" while the provider
        # keeps charging is the one answer a cancel must never give.
        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_subscription_id="sub_z"
        )
        resp = authed_client.post("/billing/api/subscription/cancel")
        assert resp.status_code == 200
        sub.refresh_from_db()
        assert sub.cancelled_at is not None

    def test_provider_failure_returns_502_and_keeps_subscription(
        self, authed_client, user, settings
    ):
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": FAILING_PATH}
        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_subscription_id="sub_x"
        )
        resp = authed_client.post("/billing/api/subscription/cancel")
        assert resp.status_code == 502
        sub.refresh_from_db()
        assert sub.cancelled_at is None  # NOT cancelled locally


@pytest.mark.django_db
class TestInternalDebitValidation:
    def _post(self, client, payload, key="test-service-key"):
        return client.post(
            "/billing/api/internal/debit",
            payload,
            format="json",
            HTTP_X_API_KEY=key,
        )

    def test_rejects_non_positive_credits(self, api_client, user, settings):
        settings.SERVICE_API_KEY = "test-service-key"
        resp = self._post(
            api_client, {"user_id": str(user.id), "credits": 0, "type": "ai_charge"}
        )
        assert resp.status_code == 400

    def test_rejects_unknown_transaction_type(self, api_client, user, settings):
        settings.SERVICE_API_KEY = "test-service-key"
        resp = self._post(
            api_client,
            {"user_id": str(user.id), "credits": 5, "type": "not_a_type"},
        )
        assert resp.status_code == 400

    def test_unknown_user_404(self, api_client, settings):
        settings.SERVICE_API_KEY = "test-service-key"
        resp = self._post(
            api_client,
            {
                "user_id": "00000000-0000-0000-0000-000000000000",
                "credits": 5,
                "type": "ai_charge",
            },
        )
        assert resp.status_code == 404

    def test_staff_user_can_debit(self, api_client, user):
        from django.contrib.auth import get_user_model

        staff = get_user_model().objects.create_user(
            username="admin",
            email="admin@example.com",
            password="x",
            is_staff=True,
        )
        api_client.force_authenticate(user=staff)
        services.credit(user=user, credits=50, type=TransactionType.CREDIT_PURCHASE)
        resp = api_client.post(
            "/billing/api/internal/debit",
            {"user_id": str(user.id), "credits": 10, "type": "adjustment"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.json()["balance_after"] == 40


class RecordingProvider(PaymentProvider):
    """Captures the redirect URLs the views pass to the provider."""

    name = "recording"
    last_checkout = None
    last_portal = None

    def create_checkout_session(self, *, user, package, plan, success_url, cancel_url):
        RecordingProvider.last_checkout = {
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        return ("https://x", "cs_1")

    def create_portal_session(self, *, customer_id, return_url):
        RecordingProvider.last_portal = {"return_url": return_url}
        return "https://x/portal"

    def cancel_subscription(self, subscription_id):
        return None

    def verify_webhook(self, payload, signature):
        return {}


RECORDING_PATH = f"{RecordingProvider.__module__}.{RecordingProvider.__qualname__}"


@pytest.mark.django_db
class TestRedirectUrlResolution:
    """request URL → STAPEL_BILLING fallback → FRONTEND_URL — never a
    placeholder domain; nothing configured is a 400, not example.com."""

    def test_checkout_derives_urls_from_frontend_url(self, authed_client, settings):
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": RECORDING_PATH}
        resp = authed_client.post(
            "/billing/api/checkout", {"package": "starter"}, format="json"
        )
        assert resp.status_code == 200
        assert RecordingProvider.last_checkout == {
            "success_url": "https://front.example/billing/success",
            "cancel_url": "https://front.example/billing/cancel",
        }

    def test_settings_fallback_beats_frontend_url(self, authed_client, settings):
        settings.STAPEL_BILLING = {
            "PAYMENT_PROVIDER": RECORDING_PATH,
            "CHECKOUT_SUCCESS_URL": "https://app.example/ok",
            "CHECKOUT_CANCEL_URL": "https://app.example/no",
        }
        resp = authed_client.post(
            "/billing/api/checkout", {"package": "starter"}, format="json"
        )
        assert resp.status_code == 200
        assert RecordingProvider.last_checkout == {
            "success_url": "https://app.example/ok",
            "cancel_url": "https://app.example/no",
        }

    def test_request_urls_beat_everything(self, authed_client, settings):
        settings.STAPEL_BILLING = {
            "PAYMENT_PROVIDER": RECORDING_PATH,
            "CHECKOUT_SUCCESS_URL": "https://app.example/ok",
            # Request-supplied targets win over the fallbacks, but only from
            # an origin the deployment declared (redirects.py).
            "REDIRECT_ALLOWED_ORIGINS": ["https://req.example"],
        }
        resp = authed_client.post(
            "/billing/api/checkout",
            {
                "package": "starter",
                "success_url": "https://req.example/s",
                "cancel_url": "https://req.example/c",
            },
            format="json",
        )
        assert resp.status_code == 200
        assert RecordingProvider.last_checkout == {
            "success_url": "https://req.example/s",
            "cancel_url": "https://req.example/c",
        }

    def test_portal_uses_portal_return_url_fallback(self, authed_client, settings):
        settings.STAPEL_BILLING = {
            "PAYMENT_PROVIDER": RECORDING_PATH,
            "PORTAL_RETURN_URL": "https://app.example/billing",
        }
        resp = authed_client.get("/billing/api/portal")
        assert resp.status_code == 200
        assert RecordingProvider.last_portal == {
            "return_url": "https://app.example/billing"
        }

    def test_checkout_400_when_nothing_configured(
        self, authed_client, settings, monkeypatch
    ):
        monkeypatch.delenv("FRONTEND_URL", raising=False)
        del settings.FRONTEND_URL
        resp = authed_client.post(
            "/billing/api/checkout", {"package": "starter"}, format="json"
        )
        assert resp.status_code == 400
        assert "redirect_url_not_configured" in resp.content.decode()
