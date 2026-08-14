"""A plan slug the catalogue does not contain grants nothing (BILL-03).

The unknown-*key* half of BILL-01 closed the case where a caller names a
feature nobody declared. This is its twin on the other axis: the *plan* the
user resolves to is missing from ``STAPEL_BILLING["PLANS"]``, so there is no
entitlement map to consult at all — and the pre-hardening reading of that
("no ceiling configured, go ahead") turned two routine configuration edits
into a silent, plan-wide paywall bypass:

* a host renames or retires a plan while live ``Subscription`` rows still
  carry the old slug — those subscribers keep an ACTIVE subscription and
  become unlimited;
* a host ships its own ladder (starter/growth/scale) without a ``"free"``
  entry, while ``Subscription.plan``'s model default is still ``"free"`` —
  which is what *every* user without a subscription row resolves to.

The vocabulary and the test shape are the ones BILL-01 established:
``allowed=False`` with a structural ``reason``, proven through the comm
Function callers actually gate on, plus the deploy-time check that stops the
second case from ever reaching runtime.
"""

import pytest
from django.core import checks as django_checks

#: A host ladder that is perfectly valid on its own and simply does not use
#: the shipped slugs — no "free", no "pro". Exactly what a host writes when
#: it names its own plans.
RENAMED_PLANS = [
    {
        "slug": "growth",
        "name": "Growth",
        "price_cents": 1900,
        "monthly_credits_included": 100,
        "storage_limit_bytes": 0,
        "description": "",
        "entitlements": {"workspaces.members.max": 3},
    },
]

#: The key both the shipped ladder and the host ladder above declare, so the
#: answer under test is about the *plan*, never about the key vocabulary.
KEY = "workspaces.members.max"


def _run(tag):
    return {issue.id for issue in django_checks.run_checks(tags=[tag])}


def _check(user, quantity=1):
    from stapel_core.comm import call

    from stapel_billing import entitlements

    entitlements.register()
    return call(
        "billing.check_entitlement",
        {"user_id": str(user.pk), "key": KEY, "quantity": quantity},
    )


@pytest.mark.django_db
class TestUnknownPlanDoesNotGrantThroughComm:
    """The deny reaches callers through the comm Function, not just the
    Python helper — that is the surface every other module gates on."""

    def test_default_plan_outside_the_host_ladder_denies(self, user, settings):
        # No Subscription row: the user resolves to Subscription.plan's model
        # default ("free"), which this ladder does not contain.
        settings.STAPEL_BILLING = {"PLANS": RENAMED_PLANS}
        assert _check(user) == {
            "allowed": False,
            "limit": None,
            "reason": "unknown_plan",
        }

    def test_retired_plan_on_a_live_subscription_denies(self, user, settings):
        from stapel_billing.models import Subscription, SubscriptionStatus

        Subscription.objects.create(
            user=user, plan="pro", status=SubscriptionStatus.ACTIVE
        )
        settings.STAPEL_BILLING = {"PLANS": RENAMED_PLANS}
        assert _check(user) == {
            "allowed": False,
            "limit": None,
            "reason": "unknown_plan",
        }

    def test_a_plan_the_catalogue_knows_still_answers_from_it(self, user, settings):
        from stapel_billing.models import Subscription, SubscriptionStatus

        Subscription.objects.create(
            user=user, plan="growth", status=SubscriptionStatus.ACTIVE
        )
        settings.STAPEL_BILLING = {"PLANS": RENAMED_PLANS}
        assert _check(user, quantity=3) == {
            "allowed": True,
            "limit": 3,
            "reason": None,
        }
        assert _check(user, quantity=4)["reason"] == "limit_exceeded"

    def test_escape_hatch_restores_the_permissive_answer(self, user, settings):
        # A host that needs time to migrate its subscribers opts back in
        # explicitly; the safe value is the default.
        settings.STAPEL_BILLING = {
            "PLANS": RENAMED_PLANS,
            "ALLOW_UNKNOWN_PLAN_SLUGS": True,
        }
        assert _check(user) == {"allowed": True, "limit": None, "reason": None}


class TestDefaultPlanCheck:
    def test_shipped_catalogue_is_clean(self):
        assert "stapel_billing.E102" not in _run(django_checks.Tags.compatibility)

    def test_ladder_without_the_default_plan_is_an_error(self, settings):
        settings.STAPEL_BILLING = {"PLANS": RENAMED_PLANS}
        assert "stapel_billing.E102" in _run(django_checks.Tags.compatibility)

    def test_declaring_the_default_slug_makes_the_ladder_valid(self, settings):
        settings.STAPEL_BILLING = {
            "PLANS": [*RENAMED_PLANS, {**RENAMED_PLANS[0], "slug": "free"}]
        }
        assert "stapel_billing.E102" not in _run(django_checks.Tags.compatibility)

    def test_escape_hatch_silences_the_check_too(self, settings):
        # The runtime deny and the boot refusal are one decision (BILL-01's
        # E101 hatch reads the same way).
        settings.STAPEL_BILLING = {
            "PLANS": RENAMED_PLANS,
            "ALLOW_UNKNOWN_PLAN_SLUGS": True,
        }
        assert "stapel_billing.E102" not in _run(django_checks.Tags.compatibility)

    def test_the_check_is_registered(self):
        registered = {
            getattr(check, "__name__", "")
            for check in django_checks.registry.registry.get_checks()
        }
        assert "check_default_plan_slug" in registered
