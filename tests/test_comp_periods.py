"""Comp time: subscription the operator gives, and the provider cannot take back.

"Put them back on Pro for a month, on us" used to be done by editing
``Subscription.current_period_end``. That column mirrors the provider: the
next subscription webhook re-reads it from Stripe and the gift disappears,
usually days later, usually noticed by the customer first.

A comp period is its own row. These pin the three things that make it
worth having: it entitles after the provider's period lapses, a webhook
cannot erase it, and it never grows by accident.
"""

import json
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from stapel_billing import services
from stapel_billing.conf import billing_settings
from stapel_billing.entitlements import check_entitlement
from stapel_billing.models import CompPeriod, Subscription, SubscriptionStatus

from .stripe_ids import sid
from .test_webhooks import PROVIDER_PATH, WEBHOOK_URL

SUB = sid("sub", "comp")
CUS = sid("cus", "comp")

#: A pro-only key from the shipped plan ladder (catalog.DEFAULT_PLANS):
#: free says False, pro says True.
PRO_KEY = "workspaces.org"


@pytest.fixture(autouse=True)
def _json_provider(settings):
    settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": PROVIDER_PATH}
    yield
    billing_settings.reload()


@pytest.fixture
def lapsed_subscription(user):
    """A pro subscriber whose provider period has ended and was cancelled."""
    return Subscription.objects.create(
        user=user,
        plan="pro",
        status=SubscriptionStatus.CANCELLED,
        stripe_subscription_id=SUB,
        stripe_customer_id=CUS,
        current_period_end=timezone.now() - timedelta(days=1),
    )


def _allowed(user) -> bool:
    return check_entitlement({"user_id": str(user.id), "key": PRO_KEY})["allowed"]


@pytest.mark.django_db
class TestCompTimeEntitles:
    def test_a_lapsed_subscriber_loses_the_plan_without_one(
        self, user, lapsed_subscription
    ):
        assert _allowed(user) is False

    def test_the_comp_window_carries_the_plan_after_the_period_lapses(
        self, user, lapsed_subscription
    ):
        services.extend_subscription(
            subscription=lapsed_subscription,
            days=60,
            reason="an outage on our side",
            actor="operator",
            apply=True,
        )
        assert _allowed(user) is True

    def test_it_expires_on_its_own(self, user, lapsed_subscription):
        preview = services.extend_subscription(
            subscription=lapsed_subscription,
            days=1,
            reason="one day",
            actor="operator",
            apply=True,
        )
        comp = CompPeriod.objects.get(id=preview.comp_period_id)
        comp.starts_at = timezone.now() - timedelta(days=5)
        comp.ends_at = timezone.now() - timedelta(days=4)
        comp.save(update_fields=["starts_at", "ends_at"])
        assert _allowed(user) is False

    def test_a_revoked_window_stops_entitling(self, user, lapsed_subscription):
        preview = services.extend_subscription(
            subscription=lapsed_subscription,
            days=30,
            reason="goodwill",
            apply=True,
        )
        assert _allowed(user) is True
        CompPeriod.objects.filter(id=preview.comp_period_id).update(
            revoked_at=timezone.now()
        )
        assert _allowed(user) is False

    def test_a_live_provider_subscription_still_governs(self, user):
        """Comp time is what carries an account AFTER the provider stops."""
        sub = Subscription.objects.create(
            user=user, plan="pro", status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id=SUB,
        )
        services.extend_subscription(
            subscription=sub, days=30, reason="goodwill", apply=True
        )
        assert _allowed(user) is True  # from the paid plan, either way


@pytest.mark.django_db
class TestTheProviderCannotEraseIt:
    def test_a_subscription_webhook_does_not_touch_the_comp_window(
        self, api_client, user, lapsed_subscription
    ):
        services.extend_subscription(
            subscription=lapsed_subscription,
            days=30,
            reason="an outage on our side",
            actor="operator",
            apply=True,
        )
        event = {
            "id": sid("evt", "comp"),
            "type": "customer.subscription.updated",
            "created": int(timezone.now().timestamp()),
            "data": {
                "object": {
                    "id": SUB,
                    "customer": CUS,
                    "status": "canceled",
                    "current_period_end": int(
                        (timezone.now() - timedelta(days=1)).timestamp()
                    ),
                }
            },
        }
        resp = api_client.post(
            WEBHOOK_URL,
            data=json.dumps(event),
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="good",
        )
        assert resp.status_code == 200
        lapsed_subscription.refresh_from_db()
        assert lapsed_subscription.status == SubscriptionStatus.CANCELLED
        # The window is a row of its own — nothing the provider says reaches it.
        assert CompPeriod.objects.count() == 1
        assert _allowed(user) is True


@pytest.mark.django_db
class TestItNeverGrowsByAccident:
    def test_a_second_grant_is_refused_unless_asked_for(
        self, user, lapsed_subscription
    ):
        services.extend_subscription(
            subscription=lapsed_subscription, days=30, reason="first", apply=True
        )
        with pytest.raises(ValueError) as exc:
            services.extend_subscription(
                subscription=lapsed_subscription, days=30, reason="second", apply=True
            )
        assert "stack" in str(exc.value)
        assert CompPeriod.objects.count() == 1

    def test_stacking_starts_where_the_existing_window_ends(
        self, user, lapsed_subscription
    ):
        first = services.extend_subscription(
            subscription=lapsed_subscription, days=30, reason="first", apply=True
        )
        second = services.extend_subscription(
            subscription=lapsed_subscription,
            days=30,
            reason="second",
            stack=True,
            apply=True,
        )
        assert second.stacked_on == first.comp_period_id
        assert second.starts_at == first.ends_at
        assert CompPeriod.objects.count() == 2

    def test_a_dry_run_writes_nothing(self, user, lapsed_subscription):
        preview = services.extend_subscription(
            subscription=lapsed_subscription, days=30, reason="thinking about it"
        )
        assert preview.applied is False
        assert preview.comp_period_id is None
        assert CompPeriod.objects.count() == 0
        assert _allowed(user) is False

    def test_the_window_opens_when_the_paid_period_closes(self, user):
        """Comp days are added to the service, not spent underneath it."""
        ends = timezone.now() + timedelta(days=10)
        sub = Subscription.objects.create(
            user=user,
            plan="pro",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id=SUB,
            current_period_end=ends,
        )
        preview = services.extend_subscription(
            subscription=sub, days=30, reason="goodwill", apply=True
        )
        assert preview.starts_at == ends
        assert preview.ends_at == ends + timedelta(days=30)

    def test_a_reason_is_required(self, lapsed_subscription):
        with pytest.raises(ValueError):
            services.extend_subscription(
                subscription=lapsed_subscription, days=30, reason="   ", apply=True
            )

    def test_zero_days_is_not_a_grant(self, lapsed_subscription):
        with pytest.raises(ValueError):
            services.extend_subscription(
                subscription=lapsed_subscription, days=0, reason="nothing", apply=True
            )

    def test_the_default_plan_has_to_be_named_explicitly(self, user):
        sub = Subscription.objects.create(user=user, plan="free")
        with pytest.raises(ValueError) as exc:
            services.extend_subscription(
                subscription=sub, days=30, reason="goodwill", apply=True
            )
        assert "default plan" in str(exc.value)
        preview = services.extend_subscription(
            subscription=sub, days=30, reason="goodwill", plan="pro", apply=True
        )
        assert preview.plan == "pro"
        assert _allowed(user) is True


@pytest.mark.django_db
class TestTheCommand:
    def test_dry_run_by_default(self, user, lapsed_subscription):
        out = StringIO()
        call_command(
            "billing_extend_subscription",
            "--user",
            str(user.id),
            "--days",
            "60",
            "--reason",
            "an outage on our side",
            stdout=out,
        )
        printed = out.getvalue()
        assert "would grant 60 day(s) of pro" in printed
        assert "nothing was written" in printed
        assert CompPeriod.objects.count() == 0

    def test_apply_writes_the_window_and_the_audit(self, user, lapsed_subscription):
        out = StringIO()
        call_command(
            "billing_extend_subscription",
            "--user",
            str(user.id),
            "--days",
            "60",
            "--reason",
            "an outage on our side",
            "--actor",
            "operator",
            "--apply",
            stdout=out,
        )
        comp = CompPeriod.objects.get()
        assert comp.plan == "pro"
        assert comp.reason == "an outage on our side"
        assert comp.granted_by == "operator"
        assert (comp.ends_at - comp.starts_at).days == 60
        assert str(comp.id) in out.getvalue()
        assert _allowed(user) is True

    def test_it_finds_the_row_by_provider_subscription_id(
        self, user, lapsed_subscription
    ):
        out = StringIO()
        call_command(
            "billing_extend_subscription",
            "--subscription",
            SUB,
            "--days",
            "7",
            "--reason",
            "goodwill",
            "--apply",
            stdout=out,
        )
        assert CompPeriod.objects.get().subscription_id == lapsed_subscription.id

    def test_an_unknown_target_is_an_error_not_a_silent_no_op(self, user):
        with pytest.raises(CommandError):
            call_command(
                "billing_extend_subscription",
                "--subscription",
                sid("sub", "ghost"),
                "--days",
                "7",
                "--reason",
                "goodwill",
                stdout=StringIO(),
            )

    def test_stacking_must_be_asked_for_on_the_command_line_too(
        self, user, lapsed_subscription
    ):
        call_command(
            "billing_extend_subscription",
            "--user",
            str(user.id),
            "--days",
            "30",
            "--reason",
            "first",
            "--apply",
            stdout=StringIO(),
        )
        with pytest.raises(CommandError):
            call_command(
                "billing_extend_subscription",
                "--user",
                str(user.id),
                "--days",
                "30",
                "--reason",
                "second",
                "--apply",
                stdout=StringIO(),
            )
        call_command(
            "billing_extend_subscription",
            "--user",
            str(user.id),
            "--days",
            "30",
            "--reason",
            "second",
            "--stack",
            "--apply",
            stdout=StringIO(),
        )
        assert CompPeriod.objects.count() == 2


@pytest.mark.django_db
class TestTheAdminActionIsGated:
    def test_the_action_asks_for_its_own_permission(self):
        from django.contrib import admin as django_admin

        from stapel_billing.admin import SubscriptionAdmin

        model_admin = SubscriptionAdmin(Subscription, django_admin.site)
        action = model_admin.extend_subscription
        assert list(action.allowed_permissions) == ["extend_subscription"]

    def test_a_user_without_the_permission_is_refused(self, user):
        from django.contrib import admin as django_admin

        from stapel_billing.admin import SubscriptionAdmin

        model_admin = SubscriptionAdmin(Subscription, django_admin.site)

        class _Request:
            pass

        request = _Request()
        request.user = user
        assert model_admin.has_extend_subscription_permission(request) is False
