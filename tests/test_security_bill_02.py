"""Redirect allowlist, checkout reconciliation and grant uniqueness.

Every test here fails on the pre-hardening library: the redirect ones
because any origin was forwarded to the provider verbatim, the checkout
ones because the grant followed provider metadata without looking at what
the session actually says, and the uniqueness ones because "has this
invoice been credited?" was a read-then-write over JSON metadata.
"""

import json

import pytest
from django.db import transaction

from stapel_billing import services
from stapel_billing.errors import (
    ERR_400_REDIRECT_URL_NOT_ALLOWED,
    ERR_400_REDIRECT_URL_NOT_CONFIGURED,
)
from stapel_billing.models import (
    ProviderGrant,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    Wallet,
)
from stapel_billing.providers.base import PaymentProvider
from stapel_billing.redirects import origin_of

CHECKOUT_URL = "/billing/api/checkout"
PORTAL_URL = "/billing/api/portal"
WEBHOOK_URL = "/billing/api/webhooks/stripe"


class RecordingProvider(PaymentProvider):
    """Captures the redirect URLs handed to the provider."""

    name = "recording"
    seen: dict = {}

    def create_checkout_session(self, *, user, package, plan, success_url, cancel_url):
        RecordingProvider.seen = {"success_url": success_url, "cancel_url": cancel_url}
        return ("https://provider.test/checkout", "cs_rec")

    def create_portal_session(self, *, customer_id, return_url):
        RecordingProvider.seen = {"return_url": return_url}
        return "https://provider.test/portal"

    def cancel_subscription(self, subscription_id):
        return None

    def verify_webhook(self, payload, signature):
        if signature == "bad":
            raise ValueError("invalid signature")
        return json.loads(payload)


PROVIDER_PATH = f"{RecordingProvider.__module__}.{RecordingProvider.__qualname__}"


@pytest.fixture
def recording_provider(settings):
    settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": PROVIDER_PATH}
    RecordingProvider.seen = {}
    return RecordingProvider


# ─── Redirect origin allowlist ──────────────────────────────


class TestOriginParsing:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://app.example.com/billing/ok", "https://app.example.com"),
            ("https://APP.example.com:443/x", "https://app.example.com"),
            ("http://localhost:3000/x", "http://localhost:3000"),
            ("https://app.example.com:8443/", "https://app.example.com:8443"),
        ],
    )
    def test_normalises_origin(self, url, expected):
        assert origin_of(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "/relative/path",
            "//evil.example/x",
            "javascript:alert(1)",
            "data:text/html,<script>",
            "https:/\\evil.example",
            "https://good.example\\@evil.example",
        ],
    )
    def test_refuses_anything_without_a_real_http_origin(self, url):
        assert origin_of(url) is None


@pytest.mark.django_db
class TestCheckoutRedirectAllowlist:
    def _checkout(self, client, **body):
        return client.post(CHECKOUT_URL, {"package": "starter", **body}, format="json")

    def test_foreign_origin_is_refused(self, authed_client, recording_provider):
        resp = self._checkout(
            authed_client, success_url="https://evil.example/thanks"
        )
        assert resp.status_code == 400
        assert ERR_400_REDIRECT_URL_NOT_ALLOWED in resp.content.decode()
        assert recording_provider.seen == {}

    def test_cancel_url_is_checked_too(self, authed_client, recording_provider):
        resp = self._checkout(
            authed_client,
            success_url="https://front.example/ok",
            cancel_url="https://evil.example/no",
        )
        assert resp.status_code == 400
        assert ERR_400_REDIRECT_URL_NOT_ALLOWED in resp.content.decode()

    def test_frontend_origin_is_accepted(self, authed_client, recording_provider):
        # FRONTEND_URL is https://front.example in the test settings, so a
        # single-frontend deployment needs no extra configuration.
        resp = self._checkout(authed_client, success_url="https://front.example/ok")
        assert resp.status_code == 200
        assert recording_provider.seen["success_url"] == "https://front.example/ok"

    def test_declared_origin_is_accepted(self, authed_client, settings):
        settings.STAPEL_BILLING = {
            "PAYMENT_PROVIDER": PROVIDER_PATH,
            "REDIRECT_ALLOWED_ORIGINS": ["https://partner.example"],
        }
        resp = self._checkout(authed_client, success_url="https://partner.example/ok")
        assert resp.status_code == 200
        assert RecordingProvider.seen["success_url"] == "https://partner.example/ok"

    def test_escape_hatch_restores_the_old_behaviour(self, authed_client, settings):
        settings.STAPEL_BILLING = {
            "PAYMENT_PROVIDER": PROVIDER_PATH,
            "ALLOW_UNVALIDATED_REDIRECT_URLS": True,
        }
        resp = self._checkout(authed_client, success_url="https://evil.example/thanks")
        assert resp.status_code == 200
        assert RecordingProvider.seen["success_url"] == "https://evil.example/thanks"

    def test_no_url_still_falls_back_to_configuration(
        self, authed_client, recording_provider
    ):
        resp = self._checkout(authed_client)
        assert resp.status_code == 200
        assert RecordingProvider.seen["success_url"].startswith("https://front.example")

    def test_unconfigured_deployment_still_reports_the_config_error(
        self, authed_client, settings
    ):
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": PROVIDER_PATH}
        settings.FRONTEND_URL = ""
        resp = self._checkout(authed_client)
        assert resp.status_code == 400
        assert ERR_400_REDIRECT_URL_NOT_CONFIGURED in resp.content.decode()


@pytest.mark.django_db
class TestPortalRedirectAllowlist:
    def test_foreign_return_url_is_refused(self, authed_client, recording_provider):
        resp = authed_client.get(PORTAL_URL, {"return_url": "https://evil.example/x"})
        assert resp.status_code == 400
        assert ERR_400_REDIRECT_URL_NOT_ALLOWED in resp.content.decode()
        assert recording_provider.seen == {}

    def test_frontend_return_url_is_accepted(self, authed_client, recording_provider):
        resp = authed_client.get(PORTAL_URL, {"return_url": "https://front.example/b"})
        assert resp.status_code == 200
        assert recording_provider.seen["return_url"] == "https://front.example/b"


# ─── Checkout reconciliation ────────────────────────────────


def _session(user, **overrides):
    obj = {
        "id": "cs_sec",
        "mode": "payment",
        "payment_status": "paid",
        "currency": "usd",
        "amount_total": 500,
        "client_reference_id": str(user.id),
        "metadata": {"user_id": str(user.id), "package": "starter"},
    }
    obj.update(overrides)
    return {
        "id": "evt_sec",
        "type": "checkout.session.completed",
        "data": {"object": obj},
    }


def _post(client, event, signature="good"):
    return client.post(
        WEBHOOK_URL,
        data=json.dumps(event),
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=signature,
    )


@pytest.mark.django_db
class TestCheckoutReconciliation:
    """The starter package is 500 credits for 500 cents (USD)."""

    @pytest.fixture(autouse=True)
    def _provider(self, settings):
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": PROVIDER_PATH}

    def test_settled_session_grants(self, api_client, user):
        assert _post(api_client, _session(user)).status_code == 200
        assert Wallet.objects.get(user=user).balance == 500

    @pytest.mark.parametrize(
        "override",
        [
            {"payment_status": "unpaid"},
            {"payment_status": None},
            {"mode": "subscription"},
            {"mode": None},
            {"currency": "eur"},
            {"amount_total": 1},
            {"amount_total": None},
            {"amount_total": "500"},
        ],
        ids=[
            "async-payment-not-cleared",
            "payment-status-absent",
            "wrong-mode",
            "mode-absent",
            "wrong-currency",
            "underpaid",
            "amount-absent",
            "amount-not-an-integer",
        ],
    )
    def test_session_that_contradicts_the_catalog_grants_nothing(
        self, api_client, user, override
    ):
        assert _post(api_client, _session(user, **override)).status_code == 200
        assert not Wallet.objects.filter(user=user).exists()
        assert not ProviderGrant.objects.exists()

    def test_coupon_discount_still_grants(self, api_client, user):
        event = _session(
            user,
            amount_total=400,
            total_details={"amount_discount": 100},
        )
        assert _post(api_client, event).status_code == 200
        assert Wallet.objects.get(user=user).balance == 500

    def test_owner_mismatch_grants_nothing(self, api_client, user):
        event = _session(
            user, client_reference_id="00000000-0000-0000-0000-000000000001"
        )
        assert _post(api_client, event).status_code == 200
        assert not Wallet.objects.filter(user=user).exists()

    def test_relaxed_mode_is_opt_in(self, api_client, user, settings):
        settings.STAPEL_BILLING = {
            "PAYMENT_PROVIDER": PROVIDER_PATH,
            "STRICT_CHECKOUT_RECONCILIATION": False,
        }
        assert _post(api_client, _session(user, payment_status="unpaid")).status_code == 200
        assert Wallet.objects.get(user=user).balance == 500

    def test_one_session_grants_once_across_distinct_events(self, api_client, user):
        first = _session(user)
        second = _session(user)
        second["id"] = "evt_sec_2"  # a different event describing one session
        assert _post(api_client, first).status_code == 200
        assert _post(api_client, second).status_code == 200
        assert Wallet.objects.get(user=user).balance == 500
        assert ProviderGrant.objects.filter(scope="checkout_session").count() == 1


# ─── Grant uniqueness ───────────────────────────────────────


def _invoice_event(event_id, invoice_id="in_sec", subscription_id="sub_sec"):
    return {
        "id": event_id,
        "type": "invoice.paid",
        "data": {
            "object": {
                "id": invoice_id,
                "subscription": subscription_id,
                "billing_reason": "subscription_cycle",
                "amount_paid": 1500,
                "currency": "usd",
            }
        },
    }


@pytest.mark.django_db
class TestInvoiceGrantUniqueness:
    @pytest.fixture(autouse=True)
    def _provider(self, settings):
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": PROVIDER_PATH}

    def test_two_events_for_one_invoice_credit_once(self, api_client, user):
        Subscription.objects.create(
            user=user,
            plan="pro",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_sec",
        )
        # invoice.paid and invoice.payment_succeeded carry distinct event
        # ids, so the per-event lock does not see them as the same delivery.
        assert _post(api_client, _invoice_event("evt_a")).status_code == 200
        second = _invoice_event("evt_b")
        second["type"] = "invoice.payment_succeeded"
        assert _post(api_client, second).status_code == 200
        wallet = Wallet.objects.get(user=user)
        assert wallet.balance == 300
        assert (
            Transaction.objects.filter(
                wallet=wallet, type=TransactionType.SUBSCRIPTION_BONUS
            ).count()
            == 1
        )

    @pytest.mark.parametrize("status", ["open", "draft", "void", "uncollectible"])
    def test_unsettled_invoice_grants_nothing(self, api_client, user, status):
        Subscription.objects.create(
            user=user,
            plan="pro",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_sec",
        )
        event = _invoice_event(f"evt_{status}")
        event["data"]["object"]["status"] = status
        assert _post(api_client, event).status_code == 200
        assert not Wallet.objects.filter(user=user).exists()
        assert not ProviderGrant.objects.exists()

    def test_paid_invoice_still_grants(self, api_client, user):
        Subscription.objects.create(
            user=user,
            plan="pro",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_sec",
        )
        event = _invoice_event("evt_paid")
        event["data"]["object"]["status"] = "paid"
        assert _post(api_client, event).status_code == 200
        assert Wallet.objects.get(user=user).balance == 300

    def test_invoice_without_an_id_is_not_granted(self, api_client, user):
        Subscription.objects.create(
            user=user,
            plan="pro",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_sec",
        )
        event = _invoice_event("evt_noid")
        event["data"]["object"].pop("id")
        assert _post(api_client, event).status_code == 200
        assert not Wallet.objects.filter(user=user).exists()


@pytest.mark.django_db
class TestInterleavedInvoiceGrant:
    """The defect is a race, so the test has to interleave the two workers.

    The old guard read the ledger for ``metadata.stripe_invoice_id`` and
    then credited. Two deliveries that both read before either wrote — the
    ordinary shape of concurrent webhook workers — both saw "not granted".
    Re-entering the handler from inside ``credit`` reproduces exactly that
    interleaving without threads, and therefore without a timing race in
    the test itself.
    """

    def test_a_second_delivery_that_reads_before_the_first_writes(
        self, user, monkeypatch
    ):
        Subscription.objects.create(
            user=user,
            plan="pro",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_int",
        )
        first = _invoice_event("evt_1", invoice_id="in_int", subscription_id="sub_int")
        second = _invoice_event("evt_2", invoice_id="in_int", subscription_id="sub_int")
        original_credit = services.credit
        reentered = []

        def credit_but_let_the_other_worker_in(**kwargs):
            if not reentered:
                reentered.append(True)
                services.handle_invoice_paid(second)
            return original_credit(**kwargs)

        monkeypatch.setattr(services, "credit", credit_but_let_the_other_worker_in)
        with transaction.atomic():
            services.handle_invoice_paid(first)

        assert reentered, "the interleaving never happened — test is not proving anything"
        wallet = Wallet.objects.get(user=user)
        assert wallet.balance == 300
        assert (
            Transaction.objects.filter(
                wallet=wallet, type=TransactionType.SUBSCRIPTION_BONUS
            ).count()
            == 1
        )
        assert ProviderGrant.objects.filter(scope="invoice").count() == 1


@pytest.mark.django_db
class TestSubscriptionProviderIdUniqueness:
    def test_two_subscriptions_cannot_share_a_provider_subscription_id(
        self, user, django_user_model
    ):
        other = django_user_model.objects.create_user(
            username="other", email="other@example.com", password="x"
        )
        Subscription.objects.create(user=user, stripe_subscription_id="sub_dup")
        with pytest.raises(Exception) as exc:
            Subscription.objects.create(user=other, stripe_subscription_id="sub_dup")
        assert "unique" in str(exc.value).lower()

    def test_two_subscriptions_cannot_share_a_provider_customer_id(
        self, user, django_user_model
    ):
        other = django_user_model.objects.create_user(
            username="other2", email="other2@example.com", password="x"
        )
        Subscription.objects.create(user=user, stripe_customer_id="cus_dup")
        with pytest.raises(Exception) as exc:
            Subscription.objects.create(user=other, stripe_customer_id="cus_dup")
        assert "unique" in str(exc.value).lower()

    def test_rows_without_a_provider_id_do_not_collide(self, user, django_user_model):
        other = django_user_model.objects.create_user(
            username="other3", email="other3@example.com", password="x"
        )
        Subscription.objects.create(user=user)
        Subscription.objects.create(user=other, stripe_customer_id="")
        assert Subscription.objects.count() == 2
