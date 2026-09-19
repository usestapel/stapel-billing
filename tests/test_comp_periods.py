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
from stapel_billing.models import (
    CompPeriod,
    Subscription,
    SubscriptionStatus,
    Transaction,
)

from .plans import (
    GROWTH_CREDITS,
    HOST_PLAN,
    SEATS_KEY,
    STARTER_CREDITS,
    UPGRADE_LADDER_SLUGS,
    host_style_plans,
    upgrade_ladder_plans,
)
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


@pytest.mark.django_db
class TestAPlanThisLibraryDoesNotShip:
    """The host's own ladder — the case the shipped enum cannot see.

    A deployment sells ``starter``/``growth``/``scale`` and configures them
    in ``STAPEL_BILLING["PLANS"]``; none of those slugs is in ``models.Plan``
    and none ever will be. Until 0.21.0 the comp gate asked the enum, so
    ``billing_extend_subscription`` answered "unknown plan 'starter'" and the
    command could not comp a single real customer of such a host.
    """

    @pytest.fixture
    def host_catalogue(self, settings):
        settings.STAPEL_BILLING = {
            **getattr(settings, "STAPEL_BILLING", {}),
            "PAYMENT_PROVIDER": PROVIDER_PATH,
            "PLANS": host_style_plans(),
        }
        yield
        billing_settings.reload()

    @pytest.fixture
    def lapsed_host_subscriber(self, user, host_catalogue):
        """A customer on the host's plan whose provider period has ended."""
        return Subscription.objects.create(
            user=user,
            plan=HOST_PLAN,
            status=SubscriptionStatus.CANCELLED,
            stripe_subscription_id=SUB,
            stripe_customer_id=CUS,
            current_period_end=timezone.now() - timedelta(days=1),
        )

    def test_the_slug_is_not_one_of_ours(self):
        from stapel_billing.models import Plan

        assert HOST_PLAN not in Plan.values

    def test_the_dry_run_names_the_plan_and_the_window(
        self, user, lapsed_host_subscriber
    ):
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
        assert f"60 day(s) of {HOST_PLAN}" in printed
        assert "->" in printed  # the window, start -> end
        assert "dry run" in printed
        assert CompPeriod.objects.count() == 0

    def test_apply_writes_the_comp_period(self, user, lapsed_host_subscriber):
        call_command(
            "billing_extend_subscription",
            "--user",
            str(user.id),
            "--days",
            "60",
            "--reason",
            "an outage on our side",
            "--apply",
            stdout=StringIO(),
        )
        comp = CompPeriod.objects.get()
        assert comp.plan == HOST_PLAN
        assert comp.subscription_id == lapsed_host_subscriber.id
        assert comp.ends_at - comp.starts_at == timedelta(days=60)
        assert comp.reason == "an outage on our side"

    def test_the_window_entitles_to_the_HOST_plans_limits(
        self, user, lapsed_host_subscriber
    ):
        """Not the enum's, and not the default plan's: the configured entry."""
        seats = {"user_id": str(user.id), "key": SEATS_KEY, "quantity": 7}
        # Lapsed and uncomped: the default plan governs, which allows 5.
        assert check_entitlement(seats)["allowed"] is False
        assert _allowed(user) is False

        services.extend_subscription(
            subscription=lapsed_host_subscriber,
            days=60,
            reason="an outage on our side",
            actor="operator",
            apply=True,
        )

        answer = check_entitlement(seats)
        assert answer["allowed"] is True
        assert answer["limit"] == 7  # the host entry's ceiling, not free's 5
        assert _allowed(user) is True

    def test_a_slug_in_neither_the_enum_nor_the_catalogue_is_still_refused(
        self, lapsed_host_subscriber
    ):
        with pytest.raises(ValueError) as exc:
            services.extend_subscription(
                subscription=lapsed_host_subscriber,
                days=30,
                reason="goodwill",
                plan="platinum",
                apply=True,
            )
        message = str(exc.value)
        assert "platinum" in message
        # and it says what IS configured, so the operator can fix the typo
        assert HOST_PLAN in message
        assert "pro" in message
        assert CompPeriod.objects.count() == 0

    def test_the_admin_can_offer_the_host_plan_on_the_comp_row(
        self, host_catalogue
    ):
        """The column's choices are the deployment's, so a form accepts it."""
        from django.core.exceptions import ValidationError

        field = CompPeriod._meta.get_field("plan")
        assert HOST_PLAN in [value for value, _label in field.choices]
        field.clean(HOST_PLAN, None)  # a model form validates through this
        with pytest.raises(ValidationError):
            field.clean("platinum", None)


@pytest.mark.django_db
class TestAnUpgradeComp:
    """Raise somebody's plan for a while, without touching the provider.

    The live case: "raise this customer's subscription level until the end
    of her current period so she has more credits" — no Stripe plan change,
    no proration, no charge. Until 0.21.0 a comp only mattered AFTER the
    paid period lapsed, so the one thing asked for was the one thing comp
    time could not do.

    The whole ladder here is the host's (starter < growth < business, none
    of them in ``models.Plan``), because rank comes from the configured
    catalogue's ORDER and never from the shipped enum.
    """

    @pytest.fixture
    def ladder(self, settings):
        settings.STAPEL_BILLING = {
            **getattr(settings, "STAPEL_BILLING", {}),
            "PAYMENT_PROVIDER": PROVIDER_PATH,
            "PLANS": upgrade_ladder_plans(),
        }
        yield
        billing_settings.reload()

    @pytest.fixture
    def period_end(self):
        # Whole seconds: the provider speaks unix timestamps, so a period
        # end with microseconds on it reads back as drift that is not one.
        return (timezone.now() + timedelta(days=20)).replace(microsecond=0)

    @pytest.fixture
    def paying_starter(self, user, ladder, period_end):
        """A live, paying customer on the host's cheapest paid tier."""
        return Subscription.objects.create(
            user=user,
            plan="starter",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id=SUB,
            stripe_customer_id=CUS,
            current_period_end=period_end,
        )

    def _seats(self, user, quantity):
        return check_entitlement(
            {"user_id": str(user.id), "key": SEATS_KEY, "quantity": quantity}
        )

    def _upgrade(self, sub, period_end, **kwargs):
        options = dict(
            subscription=sub,
            plan="growth",
            until=period_end,
            reason="a goodwill upgrade, agreed on the phone",
            actor="operator",
            apply=True,
        )
        options.update(kwargs)
        return services.extend_subscription(**options)

    # ── it governs, and it governs now ──────────────────────────────

    def test_the_ladder_is_the_hosts_own(self):
        from stapel_billing.models import Plan

        for slug in UPGRADE_LADDER_SLUGS:
            assert slug not in Plan.values

    def test_the_window_opens_immediately_not_at_period_end(
        self, paying_starter, period_end
    ):
        before = timezone.now()
        preview = self._upgrade(paying_starter, period_end)
        comp = CompPeriod.objects.get()
        assert before <= comp.starts_at <= timezone.now()
        assert comp.ends_at == period_end
        assert preview.upgrade_from == "starter"

    def test_the_higher_plan_governs_while_it_is_open(
        self, user, paying_starter, period_end
    ):
        assert services.effective_plan(user.id) == "starter"
        assert self._seats(user, 7)["allowed"] is False  # starter allows 3

        self._upgrade(paying_starter, period_end)

        assert services.effective_plan(user.id) == "growth"
        answer = self._seats(user, 7)
        assert answer["allowed"] is True
        assert answer["limit"] == 7
        # and the provider row is untouched: it mirrors what is being paid for
        paying_starter.refresh_from_db()
        assert paying_starter.plan == "starter"

    def test_when_the_window_closes_the_paid_plan_governs_again(
        self, user, paying_starter, period_end
    ):
        self._upgrade(paying_starter, period_end)
        after = period_end + timedelta(seconds=1)
        # No action, no sweep, no webhook: the window simply stops applying.
        assert services.effective_plan(user.id, now=after) == "starter"

    def test_a_comp_on_the_same_plan_still_waits_for_the_period_to_end(
        self, user, paying_starter
    ):
        preview = services.extend_subscription(
            subscription=paying_starter,
            days=30,
            reason="an outage on our side",
            apply=True,
        )
        assert preview.upgrade_from is None
        assert preview.starts_at == paying_starter.current_period_end
        assert services.effective_plan(user.id) == "starter"

    def test_a_comp_on_a_lower_plan_never_downgrades(self, user, ladder, period_end):
        sub = Subscription.objects.create(
            user=user,
            plan="business",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id=SUB,
            current_period_end=period_end,
        )
        services.extend_subscription(
            subscription=sub,
            plan="starter",
            days=30,
            reason="support gave the wrong plan",
            starts="now",
            apply=True,
        )
        # Open, lower-ranked, and therefore irrelevant while business is paid for.
        assert services.effective_plan(user.id) == "business"
        assert self._seats(user, 50)["allowed"] is True

    # ── the credits ────────────────────────────────────────────────

    def test_the_bundle_is_the_difference_and_dies_with_the_window(
        self, user, paying_starter, period_end
    ):
        preview = self._upgrade(paying_starter, period_end, grant_bundle=True)

        assert preview.bundle_baseline_plan == "starter"
        assert preview.bundle_baseline_credits == STARTER_CREDITS
        assert preview.bundle_plan_credits == GROWTH_CREDITS
        assert preview.bundle_credits == GROWTH_CREDITS - STARTER_CREDITS
        assert preview.bundle_expires_at == period_end

        txn = Transaction.objects.get(id=preview.bundle_transaction_id)
        assert txn.credits_delta == GROWTH_CREDITS - STARTER_CREDITS
        assert txn.lot.expires_at == period_end
        assert txn.metadata["comped"] is True
        assert txn.metadata["comp_period_id"] == preview.comp_period_id
        assert txn.metadata["comp_reason"] == preview.reason
        user.wallet.refresh_from_db()
        assert user.wallet.balance == GROWTH_CREDITS - STARTER_CREDITS

    def test_comped_credits_are_not_revenue(
        self, user, paying_starter, period_end
    ):
        preview = self._upgrade(paying_starter, period_end, grant_bundle=True)
        real = services.real_money(Transaction.objects.all())
        assert preview.bundle_transaction_id not in {str(t.id) for t in real}

    def test_a_dry_run_prints_the_arithmetic_and_grants_nothing(
        self, user, paying_starter
    ):
        out = StringIO()
        call_command(
            "billing_extend_subscription",
            "--subscription",
            SUB,
            "--plan",
            "growth",
            "--until-period-end",
            "--grant-bundle",
            "--reason",
            "a goodwill upgrade, agreed on the phone",
            stdout=out,
        )
        printed = out.getvalue()
        assert (
            f"growth {GROWTH_CREDITS} - starter {STARTER_CREDITS} = "
            f"+{GROWTH_CREDITS - STARTER_CREDITS} credits" in printed
        )
        assert "upgrade: starter -> growth" in printed
        assert "no plan change, no proration, no charge" in printed
        assert "dry run" in printed
        assert CompPeriod.objects.count() == 0
        assert Transaction.objects.count() == 0

    def test_the_operators_command_applies_it(self, user, paying_starter, period_end):
        """The exact invocation, end to end."""
        out = StringIO()
        call_command(
            "billing_extend_subscription",
            "--subscription",
            SUB,
            "--plan",
            "growth",
            "--until-period-end",
            "--grant-bundle",
            "--reason",
            "a goodwill upgrade, agreed on the phone",
            "--apply",
            stdout=out,
        )
        comp = CompPeriod.objects.get()
        assert comp.plan == "growth"
        assert comp.ends_at == period_end
        assert services.effective_plan(user.id) == "growth"
        assert user.wallet.balance == GROWTH_CREDITS - STARTER_CREDITS
        assert "written" in out.getvalue()

    def test_it_does_not_grant_the_bundle_twice(
        self, user, paying_starter, period_end
    ):
        self._upgrade(paying_starter, period_end, grant_bundle=True)
        granted = user.wallet.balance

        second = self._upgrade(
            paying_starter,
            period_end + timedelta(days=10),
            grant_bundle=True,
            stack=True,
        )
        assert second.bundle_credits == 0
        assert "has not expired" in second.bundle_note
        assert second.bundle_transaction_id is None
        user.wallet.refresh_from_db()
        assert user.wallet.balance == granted

    def test_an_upgrade_that_bundles_nothing_extra_says_so(
        self, user, ladder, period_end
    ):
        sub = Subscription.objects.create(
            user=user,
            plan="growth",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id=SUB,
            current_period_end=period_end,
        )
        preview = services.extend_subscription(
            subscription=sub,
            plan="growth",
            days=10,
            reason="an outage on our side",
            grant_bundle=True,
        )
        assert preview.bundle_credits == 0
        assert "nothing to add" in preview.bundle_note

    # ── the provider cannot take it away ───────────────────────────

    def test_a_provider_update_does_not_downgrade_the_effective_plan(
        self, api_client, user, paying_starter, period_end
    ):
        self._upgrade(paying_starter, period_end, grant_bundle=True)
        event = {
            "id": sid("evt", "upgrade"),
            "type": "customer.subscription.updated",
            "created": int(timezone.now().timestamp()),
            "data": {
                "object": {
                    "id": SUB,
                    "customer": CUS,
                    "status": "active",
                    "plan": {"nickname": "starter"},
                    "current_period_end": int(period_end.timestamp()),
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
        paying_starter.refresh_from_db()
        assert paying_starter.plan == "starter"  # still the provider's word
        comp = CompPeriod.objects.get()
        assert comp.revoked_at is None
        assert comp.ends_at == period_end  # not shortened
        assert services.effective_plan(user.id) == "growth"

    def test_reconcile_reports_no_drift(self, settings, user, paying_starter, period_end):
        """The effective plan is computed, so there is nothing to reconcile.

        `plan` is not one of `services.RECONCILED_FIELDS` at all — the
        column mirrors the provider and the comp lives beside it — so the
        sweep that repairs drifted rows has nothing to say about a comped
        account, and cannot shorten or erase the window.
        """
        from .test_subscription_state import PROVIDER_PATH as RECONCILABLE
        from .test_subscription_state import RecordingProvider

        self._upgrade(paying_starter, period_end, grant_bundle=True)
        settings.STAPEL_BILLING = {
            **getattr(settings, "STAPEL_BILLING", {}),
            "PAYMENT_PROVIDER": RECONCILABLE,
        }
        billing_settings.reload()
        RecordingProvider.subscriptions = {
            SUB: {
                "id": SUB,
                "object": "subscription",
                "customer": CUS,
                "status": "active",
                "cancel_at_period_end": False,
                "items": {
                    "object": "list",
                    "data": [
                        {
                            "id": sid("si", "comp"),
                            "object": "subscription_item",
                            "current_period_start": int(
                                paying_starter.current_period_start.timestamp()
                            )
                            if paying_starter.current_period_start
                            else None,
                            "current_period_end": int(period_end.timestamp()),
                        }
                    ],
                },
            }
        }
        out = StringIO()
        call_command("billing_reconcile_subscriptions", "--dry-run", stdout=out)
        printed = out.getvalue()
        assert "would fix" not in printed
        assert "0 drifted" in printed
        assert "plan" not in services.RECONCILED_FIELDS

        comp = CompPeriod.objects.get()
        assert comp.revoked_at is None
        assert comp.ends_at == period_end
        assert services.effective_plan(user.id) == "growth"

    def test_it_announces_the_change_so_caches_are_dropped(
        self, user, paying_starter, period_end
    ):
        from stapel_core.comm import action_registry

        received = []
        action_registry.subscribe(
            "subscription.changed", lambda event: received.append(event)
        )
        self._upgrade(paying_starter, period_end)
        payloads = [event.payload for event in received]
        assert payloads, "nothing announced the comp"
        assert payloads[-1]["plan"] == "growth"
        assert payloads[-1]["comped_until"] == period_end.isoformat()
        assert payloads[-1]["current_period_end"] == period_end.isoformat()
