"""An unconfigured payment provider refuses instead of fabricating (BILL-06).

With an empty ``STRIPE_SECRET_KEY`` the Stripe provider used to answer every
call with a dev placeholder, and nothing anywhere said so:

* ``POST /billing/api/checkout`` returned **200** with a ``cs_dev_*`` session
  id and an ``example.invalid`` URL — a client, or a support agent reading
  the response, has no way to tell that from a real checkout;
* ``GET /billing/api/portal`` returned a portal link to nowhere;
* ``POST /billing/api/subscription/cancel`` returned quietly, so the view
  stamped ``cancelled_at`` and told the user they were cancelled — the one
  answer a cancel must never give while a provider might keep charging.

The placeholder is a developer's convenience, so it now costs an explicit
``ALLOW_UNCONFIGURED_PAYMENT_PROVIDER``; every other deployment gets a
refusal, and ``manage.py check`` refuses the boot outright (E104) rather than
letting the first customer discover it.
"""

import pytest
from django.core import checks as django_checks

from stapel_billing.models import Subscription
from stapel_billing.providers.base import PaymentProvider, ProviderNotConfiguredError
from stapel_billing.providers.stripe import StripeProvider

CHECKOUT_URL = "/billing/api/checkout"
PORTAL_URL = "/billing/api/portal"
CANCEL_URL = "/billing/api/subscription/cancel"


def _run(tag):
    return {issue.id for issue in django_checks.run_checks(tags=[tag])}


class _FakeUser:
    id = "11111111-2222-3333-4444-555555555555"


# ─── The provider itself ────────────────────────────────────


class TestUnconfiguredProviderRefuses:
    def test_package_checkout_raises(self, stripe_disabled):
        with pytest.raises(ProviderNotConfiguredError):
            StripeProvider().create_checkout_session(
                user=_FakeUser(),
                package="starter",
                plan=None,
                success_url="https://s",
                cancel_url="https://c",
            )

    def test_plan_checkout_raises(self, stripe_disabled):
        with pytest.raises(ProviderNotConfiguredError):
            StripeProvider().create_checkout_session(
                user=_FakeUser(),
                package=None,
                plan="pro",
                success_url="https://s",
                cancel_url="https://c",
            )

    def test_portal_raises(self, stripe_disabled):
        with pytest.raises(ProviderNotConfiguredError):
            StripeProvider().create_portal_session(
                customer_id="cus_1", return_url="https://r"
            )

    def test_cancel_raises(self, stripe_disabled):
        with pytest.raises(ProviderNotConfiguredError):
            StripeProvider().cancel_subscription("sub_1")

    def test_is_configured_reports_the_truth(self, stripe_disabled, settings):
        assert StripeProvider().is_configured() is False
        # A provider that cannot introspect its own credentials says so by
        # not overriding the default.
        assert PaymentProvider.is_configured(StripeProvider()) is True


# ─── The HTTP surface ───────────────────────────────────────


@pytest.mark.django_db
class TestMoneyEndpointsDoNotFabricate:
    def test_checkout_answers_502_instead_of_a_fake_session(
        self, authed_client, stripe_disabled
    ):
        resp = authed_client.post(CHECKOUT_URL, {"plan": "pro"}, format="json")
        assert resp.status_code == 502, resp.content
        assert "cs_dev" not in resp.content.decode()

    def test_portal_answers_502_instead_of_a_link_to_nowhere(
        self, authed_client, user, stripe_disabled
    ):
        Subscription.objects.create(user=user, stripe_customer_id="cus_77")
        resp = authed_client.get(PORTAL_URL)
        assert resp.status_code == 502, resp.content
        assert "example.invalid" not in resp.content.decode()

    def test_cancel_does_not_report_a_cancellation_nobody_performed(
        self, authed_client, user, stripe_disabled
    ):
        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_subscription_id="sub_z"
        )
        resp = authed_client.post(CANCEL_URL)
        assert resp.status_code == 502, resp.content
        sub.refresh_from_db()
        assert sub.cancelled_at is None


@pytest.mark.django_db
class TestTheDevHatchIsExplicit:
    def test_opting_in_restores_the_placeholder_checkout(
        self, authed_client, stripe_placeholders_allowed
    ):
        resp = authed_client.post(CHECKOUT_URL, {"plan": "pro"}, format="json")
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "cs_dev_pro"


# ─── The boot check ─────────────────────────────────────────


class TestPaymentProviderCheck:
    def test_unconfigured_provider_fails_the_boot(self, stripe_disabled):
        assert "stapel_billing.E104" in _run(django_checks.Tags.compatibility)

    def test_configured_provider_is_clean(self, settings, monkeypatch):
        monkeypatch.setattr(
            StripeProvider, "is_configured", lambda self: True, raising=True
        )
        assert "stapel_billing.E104" not in _run(django_checks.Tags.compatibility)

    def test_a_provider_that_is_not_a_provider_is_an_error(self, settings):
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": "decimal.Decimal"}
        assert "stapel_billing.E103" in _run(django_checks.Tags.compatibility)

    def test_the_dev_hatch_is_reported_as_the_fabrication_it_is(
        self, stripe_placeholders_allowed
    ):
        issues = _run(django_checks.Tags.compatibility)
        assert "stapel_billing.W104" in issues
        # The hatch is a deliberate choice, so it replaces the refusal.
        assert "stapel_billing.E104" not in issues

    def test_the_check_is_registered(self):
        registered = {
            getattr(check, "__name__", "")
            for check in django_checks.registry.registry.get_checks()
        }
        assert "check_payment_provider_configured" in registered
