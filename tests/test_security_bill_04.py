"""Switches read the environment as text, not as truth (BILL-04).

``AppSettings`` has no per-key type: an environment variable arrives as the
raw string an operator typed, and every switch in ``conf.py`` used to be
consumed as a bare truth value. ``bool("false")`` is ``True``, so

    ALLOW_UNKNOWN_ENTITLEMENT_KEYS=false

*enabled* the paywall hatch, and the same spelling re-armed the BILL-02 open
redirect. The mirror image bit the gate that defaults to on: an empty
``STRICT_CHECKOUT_RECONCILIATION=`` disabled the checkout reconciliation
entirely, because ``bool("")`` is ``False``.

``PAYMENT_PROVIDER`` is worse than a switch — it names the class that handles
money, resolved through ``import_string``. A same-named environment variable
in a shared pod or compose file could swap it, so it is now ``no_env``: the
namespace dict, a flat Django setting or the default, never the environment.

Every test here fails on the pre-coercion library.
"""

import json

import pytest
from stapel_core.django.api.errors import StapelValidationError

from stapel_billing import redirects, services
from stapel_billing.conf import (
    allow_unknown_entitlement_keys,
    allow_unknown_plan_slugs,
    allow_unvalidated_redirect_urls,
    billing_settings,
    strict_checkout_reconciliation,
)
from stapel_billing.models import ProviderGrant, Wallet
from stapel_billing.providers.base import PaymentProvider
from stapel_billing.providers.stripe import StripeProvider

WEBHOOK_URL = "/billing/api/webhooks/stripe"

#: An importable PaymentProvider subclass, so "the environment picked a
#: different money class" is a real swap rather than an ImportError.
class EnvProvider(PaymentProvider):
    name = "env"

    def create_checkout_session(self, *, user, package, plan, success_url, cancel_url):
        return ("https://provider.test/checkout", "cs_env")

    def create_portal_session(self, *, customer_id, return_url):
        return "https://provider.test/portal"

    def cancel_subscription(self, subscription_id):
        return None

    def verify_webhook(self, payload, signature):
        if signature == "bad":
            raise ValueError("invalid signature")
        return json.loads(payload)


PROVIDER_PATH = f"{EnvProvider.__module__}.{EnvProvider.__qualname__}"


@pytest.fixture
def env(monkeypatch):
    """Set switches the way a deployment does — through the environment.

    ``AppSettings`` caches per key and only invalidates on Django's
    ``setting_changed``; an environment variable moves neither, so the cache
    is cleared around each test explicitly.
    """
    billing_settings.reload()
    yield monkeypatch
    billing_settings.reload()


# ─── The hatches that default to off ────────────────────────


TRUTHY = ["1", "true", "TRUE", " true ", "yes", "on"]
FALSY = ["0", "false", "False", "no", "off", "", "maybe", "disabled"]


class TestDefaultOffSwitches:
    @pytest.mark.parametrize("value", FALSY)
    def test_a_non_yes_string_leaves_the_hatch_shut(self, env, value):
        for key in (
            "ALLOW_UNKNOWN_ENTITLEMENT_KEYS",
            "ALLOW_UNVALIDATED_REDIRECT_URLS",
            "ALLOW_UNKNOWN_PLAN_SLUGS",
        ):
            env.setenv(key, value)
        billing_settings.reload()
        assert allow_unknown_entitlement_keys() is False
        assert allow_unvalidated_redirect_urls() is False
        assert allow_unknown_plan_slugs() is False

    @pytest.mark.parametrize("value", TRUTHY)
    def test_a_yes_string_still_opens_it(self, env, value):
        env.setenv("ALLOW_UNKNOWN_ENTITLEMENT_KEYS", value)
        billing_settings.reload()
        assert allow_unknown_entitlement_keys() is True

    def test_a_python_bool_from_the_settings_dict_still_wins(self, settings):
        settings.STAPEL_BILLING = {"ALLOW_UNKNOWN_ENTITLEMENT_KEYS": True}
        assert allow_unknown_entitlement_keys() is True
        settings.STAPEL_BILLING = {"ALLOW_UNKNOWN_ENTITLEMENT_KEYS": False}
        assert allow_unknown_entitlement_keys() is False


class TestFalseInTheEnvironmentDoesNotOpenTheDoor:
    """The end-to-end half: the refusal each hatch guards stays in force."""

    def test_redirect_allowlist_still_refuses_a_foreign_origin(self, env):
        env.setenv("ALLOW_UNVALIDATED_REDIRECT_URLS", "false")
        billing_settings.reload()
        with pytest.raises(StapelValidationError):
            redirects.resolve(
                "https://evil.example/steal", "CHECKOUT_SUCCESS_URL", "/billing/success"
            )

    @pytest.mark.django_db
    def test_an_undeclared_entitlement_key_is_still_denied(self, env, user):
        from stapel_core.comm import call

        from stapel_billing import entitlements

        env.setenv("ALLOW_UNKNOWN_ENTITLEMENT_KEYS", "false")
        billing_settings.reload()
        entitlements.register()
        result = call(
            "billing.check_entitlement",
            {"user_id": str(user.pk), "key": "recordings.free_minutes"},
        )
        assert result["reason"] == "unknown_key"

    @pytest.mark.django_db
    def test_a_plan_outside_the_catalogue_is_still_denied(self, env, user, settings):
        from stapel_core.comm import call

        from stapel_billing import entitlements
        from stapel_billing.models import Subscription, SubscriptionStatus

        Subscription.objects.create(
            user=user, plan="legacy", status=SubscriptionStatus.ACTIVE
        )
        env.setenv("ALLOW_UNKNOWN_PLAN_SLUGS", "false")
        billing_settings.reload()
        entitlements.register()
        result = call(
            "billing.check_entitlement",
            {"user_id": str(user.pk), "key": "workspaces.org"},
        )
        assert result["reason"] == "unknown_plan"


# ─── The gate that defaults to on ───────────────────────────


def _unpaid_session(user):
    return {
        "id": "evt_env",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_env",
                "mode": "payment",
                # Stripe completes a session whose asynchronous payment has
                # not cleared; granting on it is granting on a promise.
                "payment_status": "unpaid",
                "currency": "usd",
                "amount_total": 500,
                "client_reference_id": str(user.id),
                "metadata": {"user_id": str(user.id), "package": "starter"},
            }
        },
    }


class TestDefaultOnGate:
    @pytest.mark.parametrize("value", ["", "maybe", "1", "true", "yes", "on"])
    def test_a_stray_env_value_cannot_turn_reconciliation_off(self, env, value):
        env.setenv("STRICT_CHECKOUT_RECONCILIATION", value)
        billing_settings.reload()
        assert strict_checkout_reconciliation() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_only_an_explicit_no_relaxes_it(self, env, value):
        env.setenv("STRICT_CHECKOUT_RECONCILIATION", value)
        billing_settings.reload()
        assert strict_checkout_reconciliation() is False

    @pytest.mark.django_db
    def test_an_empty_env_var_does_not_pay_out_an_unsettled_session(
        self, env, api_client, user, settings
    ):
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": PROVIDER_PATH}
        env.setenv("STRICT_CHECKOUT_RECONCILIATION", "")
        billing_settings.reload()
        resp = api_client.post(
            WEBHOOK_URL,
            data=json.dumps(_unpaid_session(user)),
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="good",
        )
        assert resp.status_code == 200
        assert not Wallet.objects.filter(user=user).exists()
        assert not ProviderGrant.objects.exists()


# ─── The class that handles money ───────────────────────────


class TestPaymentProviderIsNotSelectableFromTheEnvironment:
    def test_an_environment_variable_does_not_swap_the_provider(self, env):
        env.setenv("PAYMENT_PROVIDER", PROVIDER_PATH)
        billing_settings.reload()
        assert billing_settings.PAYMENT_PROVIDER is StripeProvider
        assert isinstance(services.get_provider(), StripeProvider)

    def test_the_settings_namespace_still_swaps_it(self, settings):
        # no_env closes one door, not the seam: the documented dotted-path
        # override keeps working.
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": PROVIDER_PATH}
        assert isinstance(services.get_provider(), EnvProvider)
