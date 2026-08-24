"""Plan bundles granted without a payment provider (0.11.0).

Every credit this module granted before 0.11.0 came out of a Stripe
webhook, so a plan whose bundle is not sold through Stripe granted nothing
at all: a free tier's ``monthly_credits_included`` was a number in a
catalogue that no code path read, and free users started and stayed at 0.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_billing import services
from stapel_billing.models import (
    CreditLot,
    LotSource,
    ProviderGrant,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
)
from stapel_billing.tasks import grant_plan_bundles

FREE_WITH_CREDITS = {
    "PLANS": [
        {
            "slug": "free",
            "name": "Free",
            "price_cents": 0,
            "monthly_credits_included": 25,
            "storage_limit_bytes": 0,
            "description": "A free tier that actually hands over its credits.",
            "entitlements": {},
        },
        {
            "slug": "pro",
            "name": "Pro",
            "price_cents": 1500,
            "monthly_credits_included": 300,
            "storage_limit_bytes": 0,
            "description": "Pro.",
            "entitlements": {},
        },
    ]
}


@pytest.fixture
def free_plan_grants(settings):
    from stapel_billing.conf import billing_settings

    settings.STAPEL_BILLING = {
        **getattr(settings, "STAPEL_BILLING", {}),
        **FREE_WITH_CREDITS,
    }
    billing_settings.reload()
    yield settings
    billing_settings.reload()


def _other_user(username="second"):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=username, email=f"{username}@example.com", password="x"
    )


@pytest.mark.django_db
class TestGrantPlanBundle:
    def test_grants_an_expiring_lot_dated_to_the_period(self, user, free_plan_grants):
        txn = services.grant_plan_bundle(
            user=user, plan_slug="free", period_key="2099-03"
        )

        assert txn.credits_delta == 25
        assert txn.type == TransactionType.SUBSCRIPTION_BONUS
        lot = CreditLot.objects.get(granting_transaction=txn)
        assert lot.source == LotSource.SUBSCRIPTION
        # The bundle dies with the period that pays for it: 2099-03 ends
        # the instant 2099-04 begins.
        assert lot.expires_at.isoformat() == "2099-04-01T00:00:00+00:00"

    def test_is_idempotent_per_wallet_plan_and_period(self, user, free_plan_grants):
        first = services.grant_plan_bundle(
            user=user, plan_slug="free", period_key="2099-03"
        )
        again = services.grant_plan_bundle(
            user=user, plan_slug="free", period_key="2099-03"
        )

        assert first is not None
        assert again is None
        assert Transaction.objects.filter(credits_delta=25).count() == 1
        assert ProviderGrant.objects.filter(
            provider="local", scope=ProviderGrant.SCOPE_PLAN_BUNDLE
        ).count() == 1

    def test_the_next_period_is_a_new_bundle(self, user, free_plan_grants):
        services.grant_plan_bundle(user=user, plan_slug="free", period_key="2099-03")
        services.grant_plan_bundle(user=user, plan_slug="free", period_key="2099-04")
        assert Transaction.objects.filter(credits_delta=25).count() == 2

    def test_a_plan_that_bundles_nothing_grants_nothing(self, user):
        # The stock catalogue's free plan includes 0 credits.
        assert services.grant_plan_bundle(user=user, plan_slug="free") is None
        assert Transaction.objects.count() == 0

    def test_a_past_period_is_refused_rather_than_granted_dead(
        self, user, free_plan_grants
    ):
        assert (
            services.grant_plan_bundle(user=user, plan_slug="free", period_key="2020-01")
            is None
        )
        assert Transaction.objects.count() == 0

    def test_an_unparseable_period_needs_an_explicit_deadline(
        self, user, free_plan_grants
    ):
        assert (
            services.grant_plan_bundle(user=user, plan_slug="free", period_key="week-14")
            is None
        )
        txn = services.grant_plan_bundle(
            user=user,
            plan_slug="free",
            period_key="week-14",
            expires_at=timezone.now() + timedelta(days=7),
        )
        assert txn.credits_delta == 25

    def test_an_unknown_plan_without_explicit_credits_grants_nothing(self, user):
        assert services.grant_plan_bundle(user=user, plan_slug="ghost") is None

    def test_the_bundle_settles_an_outstanding_debt_like_any_other_credit(
        self, user, free_plan_grants
    ):
        services.debit(
            user=user, credits=10, type=TransactionType.AI_CHARGE, allow_partial=True
        )
        services.grant_plan_bundle(user=user, plan_slug="free", period_key="2099-03")
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 15


@pytest.mark.django_db
class TestDefaultEntitlementResolver:
    def test_a_wallet_with_no_subscription_gets_the_default_plan(
        self, user, free_plan_grants
    ):
        services.get_or_create_wallet(user)
        rows = list(services.default_plan_bundle_entitlements())
        assert [(r["user_id"], r["plan"]) for r in rows] == [(user.id, "free")]

    def test_a_stripe_billed_subscriber_is_left_to_the_invoices(
        self, user, free_plan_grants
    ):
        services.get_or_create_wallet(user)
        Subscription.objects.create(
            user=user,
            plan="pro",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_live_1",
        )
        assert list(services.default_plan_bundle_entitlements()) == []

    def test_a_locally_assigned_plan_is_granted_here(self, user, free_plan_grants):
        services.get_or_create_wallet(user)
        Subscription.objects.create(
            user=user, plan="pro", status=SubscriptionStatus.ACTIVE
        )
        rows = list(services.default_plan_bundle_entitlements())
        assert [r["plan"] for r in rows] == ["pro"]

    def test_a_cancelled_subscription_falls_back_to_the_default_plan(
        self, user, free_plan_grants
    ):
        services.get_or_create_wallet(user)
        Subscription.objects.create(
            user=user, plan="pro", status=SubscriptionStatus.CANCELLED
        )
        rows = list(services.default_plan_bundle_entitlements())
        assert [r["plan"] for r in rows] == ["free"]

    def test_a_user_who_never_touched_billing_has_no_wallet_and_is_skipped(
        self, user, free_plan_grants
    ):
        assert list(services.default_plan_bundle_entitlements()) == []


@pytest.mark.django_db
class TestGrantPlanBundlesWorker:
    def test_grants_the_current_period_to_every_entitled_wallet(
        self, user, free_plan_grants
    ):
        second = _other_user()
        services.get_or_create_wallet(user)
        services.get_or_create_wallet(second)

        assert grant_plan_bundles() == 2
        for who in (user, second):
            wallet = services.get_or_create_wallet(who)
            wallet.refresh_from_db()
            assert wallet.balance == 25

    def test_running_it_again_in_the_same_period_grants_nothing(
        self, user, free_plan_grants
    ):
        services.get_or_create_wallet(user)
        assert grant_plan_bundles() == 1
        assert grant_plan_bundles() == 0
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 25

    def test_the_resolver_is_a_seam_the_host_can_replace(
        self, user, free_plan_grants, settings
    ):
        from stapel_billing.conf import billing_settings

        settings.STAPEL_BILLING = {
            **settings.STAPEL_BILLING,
            "PLAN_BUNDLE_ENTITLEMENTS": f"{__name__}._host_resolver",
        }
        billing_settings.reload()
        try:
            services.get_or_create_wallet(user)
            _HOST_ROWS.append({"user_id": user.id, "plan": "pro"})
            assert grant_plan_bundles() == 1
            wallet = services.get_or_create_wallet(user)
            wallet.refresh_from_db()
            # The host's answer won: pro's 300, not the default plan's 25.
            assert wallet.balance == 300
        finally:
            _HOST_ROWS.clear()
            billing_settings.reload()

    def test_one_bad_row_does_not_stop_the_month(self, user, free_plan_grants, settings):
        from stapel_billing.conf import billing_settings

        settings.STAPEL_BILLING = {
            **settings.STAPEL_BILLING,
            "PLAN_BUNDLE_ENTITLEMENTS": f"{__name__}._host_resolver",
        }
        billing_settings.reload()
        try:
            services.get_or_create_wallet(user)
            _HOST_ROWS.extend(
                [
                    {"plan": "pro"},  # no subject
                    {"user_id": user.id},  # no plan
                    ("not", "a", "pair"),
                    {"user_id": user.id, "plan": "free"},
                ]
            )
            assert grant_plan_bundles() == 1
        finally:
            _HOST_ROWS.clear()
            billing_settings.reload()


#: Rows the seam test feeds through the configured resolver.
_HOST_ROWS: list = []


def _host_resolver():
    return list(_HOST_ROWS)
