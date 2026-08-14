"""Entitlement vocabulary + the deploy-time checks that back it (BILL-01).

The runtime half (an undeclared key denies instead of granting) lives in
tests/test_entitlements.py. What this module proves is the other half: the
mechanism is actually WIRED — ``AppConfig.ready()`` imports ``checks.py``,
so Django's own registry runs these checks, and a plan with a typo'd
entitlement key fails ``manage.py check`` instead of quietly changing a
paywall. A check nobody registers is a check nobody runs.
"""

import pytest
from django.core import checks as django_checks

from stapel_billing.entitlements import declared_keys

#: A plan ladder whose only difference from the shipped one is a misspelled
#: feature key — the exact accident BILL-01 describes.
TYPO_PLANS = [
    {
        "slug": "free",
        "name": "Free",
        "price_cents": 0,
        "monthly_credits_included": 0,
        "storage_limit_bytes": 0,
        "description": "",
        "entitlements": {"workspaces.member.max": 5},
    },
]


def _run(tag):
    return {issue.id for issue in django_checks.run_checks(tags=[tag])}


class TestChecksAreRegistered:
    def test_ready_registered_both_billing_checks(self):
        registered = {
            getattr(check, "__name__", "")
            for check in django_checks.registry.registry.get_checks()
        }
        assert {"check_plan_entitlement_keys", "check_redirect_allowlist"} <= registered


class TestEntitlementKeyCheck:
    def test_shipped_catalogue_is_clean(self):
        assert "stapel_billing.E101" not in _run(django_checks.Tags.compatibility)

    def test_typo_in_a_configured_plan_is_an_error(self, settings):
        settings.STAPEL_BILLING = {"PLANS": TYPO_PLANS}
        assert "stapel_billing.E101" in _run(django_checks.Tags.compatibility)

    def test_declaring_the_key_makes_it_legitimate(self, settings):
        settings.STAPEL_BILLING = {
            "PLANS": TYPO_PLANS,
            "ENTITLEMENT_KEYS": ["workspaces.member.max"],
        }
        assert "stapel_billing.E101" not in _run(django_checks.Tags.compatibility)
        assert "workspaces.member.max" in declared_keys()

    def test_escape_hatch_silences_the_check_too(self, settings):
        # The runtime deny and the boot refusal are one decision: a host
        # that opts back into the permissive behaviour must not be stopped
        # at deploy for the keys it has chosen to accept.
        settings.STAPEL_BILLING = {
            "PLANS": TYPO_PLANS,
            "ALLOW_UNKNOWN_ENTITLEMENT_KEYS": True,
        }
        assert "stapel_billing.E101" not in _run(django_checks.Tags.compatibility)

    def test_escape_hatch_is_reported_as_the_paywall_hole_it_is(self, settings):
        # Silencing the error is not the same as saying nothing: the
        # redirect hatch names itself on every boot (W102), and the one
        # that decides who gets paid features must too.
        settings.STAPEL_BILLING = {"ALLOW_UNKNOWN_ENTITLEMENT_KEYS": True}
        assert "stapel_billing.W101" in _run(django_checks.Tags.compatibility)

    def test_a_deployment_that_did_not_open_it_is_not_warned(self, settings):
        settings.STAPEL_BILLING = {}
        assert "stapel_billing.W101" not in _run(django_checks.Tags.compatibility)


class TestRedirectAllowlistCheck:
    def test_configured_frontend_needs_no_warning(self, settings):
        # FRONTEND_URL is https://front.example in the test settings.
        settings.STAPEL_BILLING = {}
        assert "stapel_billing.W103" not in _run(django_checks.Tags.security)

    def test_deployment_without_any_origin_is_warned(self, settings, monkeypatch):
        monkeypatch.delenv("FRONTEND_URL", raising=False)
        settings.FRONTEND_URL = ""
        settings.STAPEL_BILLING = {}
        assert "stapel_billing.W103" in _run(django_checks.Tags.security)

    def test_escape_hatch_is_reported_as_the_open_redirect_it_is(self, settings):
        settings.STAPEL_BILLING = {"ALLOW_UNVALIDATED_REDIRECT_URLS": True}
        assert "stapel_billing.W102" in _run(django_checks.Tags.security)


@pytest.mark.django_db
class TestUnknownKeyDoesNotGrantThroughComm:
    """The deny reaches callers through the comm Function, not just the
    Python helper — that is the surface every other module gates on."""

    def test_call_denies_an_undeclared_key(self, user):
        from stapel_core.comm import call

        from stapel_billing import entitlements

        entitlements.register()
        result = call(
            "billing.check_entitlement",
            {"user_id": str(user.pk), "key": "recordings.free_minutes", "quantity": 1},
        )
        assert result == {
            "allowed": False,
            "limit": None,
            "reason": "unknown_key",
        }
