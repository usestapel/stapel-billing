"""StripeProvider: SDK delegation, refusals, and the opt-in dev placeholders."""

import sys
import types

import pytest

from stapel_billing.conf import billing_settings
from stapel_billing.providers.stripe import StripeProvider


@pytest.fixture(autouse=True)
def _reset_billing_settings():
    yield
    billing_settings.reload()


class _FakeUser:
    id = "11111111-2222-3333-4444-555555555555"


class _FakeStripe(types.ModuleType):
    """Minimal stand-in for the stripe SDK capturing calls."""

    def __init__(self):
        super().__init__("stripe")
        self.api_key = None
        self.calls = {}

        fake = self

        class _CheckoutSession:
            @staticmethod
            def create(**kwargs):
                fake.calls["checkout"] = kwargs
                return {"url": "https://stripe.test/checkout", "id": "cs_live_1"}

        class _PortalSession:
            @staticmethod
            def create(**kwargs):
                fake.calls["portal"] = kwargs
                return {"url": "https://stripe.test/portal"}

        class _Subscription:
            @staticmethod
            def modify(sub_id, **kwargs):
                fake.calls["modify"] = (sub_id, kwargs)

        class _Webhook:
            @staticmethod
            def construct_event(payload, signature, secret):
                fake.calls["construct"] = (payload, signature, secret)
                return {"id": "evt_sdk", "type": "sdk.event"}

        self.checkout = types.SimpleNamespace(Session=_CheckoutSession)
        self.billing_portal = types.SimpleNamespace(Session=_PortalSession)
        self.Subscription = _Subscription
        self.Webhook = _Webhook


@pytest.fixture
def fake_stripe(monkeypatch, settings):
    fake = _FakeStripe()
    monkeypatch.setitem(sys.modules, "stripe", fake)
    settings.STRIPE_SECRET_KEY = "sk_test_fake"
    settings.STRIPE_WEBHOOK_SECRET = "whsec_fake"
    return fake


class TestPlaceholderPaths:
    """Stripe unconfigured *and* dev placeholders explicitly opted into.

    Without ``ALLOW_UNCONFIGURED_PAYMENT_PROVIDER`` every one of these paths
    refuses instead (BILL-06, tests/test_security_bill_06.py).
    """

    def test_package_checkout_placeholder(self, stripe_placeholders_allowed):
        url, session_id = StripeProvider().create_checkout_session(
            user=_FakeUser(),
            package="starter",
            plan=None,
            success_url="https://s",
            cancel_url="https://c",
        )
        assert url == "https://example.invalid/stripe-not-configured/package/starter"
        assert session_id == "cs_dev_starter"

    def test_plan_checkout_placeholder(self, stripe_placeholders_allowed):
        url, session_id = StripeProvider().create_checkout_session(
            user=_FakeUser(),
            package=None,
            plan="pro",
            success_url="https://s",
            cancel_url="https://c",
        )
        assert url == "https://example.invalid/stripe-not-configured/plan/pro"
        assert session_id == "cs_dev_pro"

    def test_portal_placeholder(self, stripe_placeholders_allowed):
        url = StripeProvider().create_portal_session(
            customer_id="cus_1", return_url="https://r"
        )
        assert url == "https://example.invalid/stripe-portal-not-configured"

    def test_cancel_is_noop(self, stripe_placeholders_allowed):
        assert StripeProvider().cancel_subscription("sub_1") is None

    def test_verify_webhook_raises_when_unconfigured(self, stripe_disabled):
        with pytest.raises(ValueError):
            StripeProvider().verify_webhook(b"{}", "sig")


class TestConfiguredSdkDelegation:
    def test_package_checkout_via_sdk(self, fake_stripe):
        url, session_id = StripeProvider().create_checkout_session(
            user=_FakeUser(),
            package="starter",
            plan=None,
            success_url="https://s",
            cancel_url="https://c",
        )
        assert (url, session_id) == ("https://stripe.test/checkout", "cs_live_1")
        call = fake_stripe.calls["checkout"]
        assert call["mode"] == "payment"
        assert call["metadata"] == {"user_id": _FakeUser.id, "package": "starter"}
        assert call["line_items"][0]["price_data"]["unit_amount"] == 500
        assert fake_stripe.api_key == "sk_test_fake"

    def test_plan_checkout_via_sdk(self, fake_stripe):
        url, _ = StripeProvider().create_checkout_session(
            user=_FakeUser(),
            package=None,
            plan="pro",
            success_url="https://s",
            cancel_url="https://c",
        )
        assert url == "https://stripe.test/checkout"
        call = fake_stripe.calls["checkout"]
        assert call["mode"] == "subscription"
        assert call["line_items"][0]["price_data"]["recurring"] == {"interval": "month"}
        assert call["line_items"][0]["price_data"]["unit_amount"] == 1500

    def test_portal_via_sdk(self, fake_stripe):
        url = StripeProvider().create_portal_session(
            customer_id="cus_9", return_url="https://r"
        )
        assert url == "https://stripe.test/portal"
        assert fake_stripe.calls["portal"] == {
            "customer": "cus_9",
            "return_url": "https://r",
        }

    def test_portal_without_customer_id_falls_back(self, fake_stripe):
        url = StripeProvider().create_portal_session(
            customer_id="", return_url="https://r"
        )
        assert url == "https://example.invalid/stripe-portal-not-configured"
        assert "portal" not in fake_stripe.calls

    def test_cancel_via_sdk(self, fake_stripe):
        StripeProvider().cancel_subscription("sub_9")
        assert fake_stripe.calls["modify"] == ("sub_9", {"cancel_at_period_end": True})

    def test_verify_webhook_via_sdk(self, fake_stripe):
        event = StripeProvider().verify_webhook(b'{"id": "evt"}', "sig-header")
        assert event == {"id": "evt_sdk", "type": "sdk.event"}
        assert fake_stripe.calls["construct"] == (
            b'{"id": "evt"}',
            "sig-header",
            "whsec_fake",
        )
