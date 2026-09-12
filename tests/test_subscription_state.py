"""Subscription state: the provider's truth, mirrored and answerable.

Written against a defect measured in production, not a hypothetical. On a
live deployment (Stripe ``api_version`` ``2026-06-24.dahlia``):

  * 21 subscription/invoice webhooks received, all processed, none errored;
  * all three paid rows had ``current_period_start`` and
    ``current_period_end`` NULL — because Stripe MOVED those fields onto
    the subscription ITEM and the reader only knew the old place, so it
    found nothing every single time and said nothing about it;
  * two of those rows had a local ``cancelled_at`` and ``status='active'``,
    which is what Stripe really says about a cancel-at-period-end — but
    nothing in the API could express the difference, so the UI could not
    either;
  * 111 free-plan rows read ``plan='free', status='active'``, and a client
    deciding from those offered "Cancel subscription" to every one of them.

Each test below fails on the code that shipped that.
"""

import json

import pytest

from stapel_billing.errors import ERR_409_SUBSCRIPTION_NOT_PAID
from stapel_billing.models import (
    PendingSubscriptionPeriod,
    Subscription,
    SubscriptionStatus,
)
from stapel_billing.providers.base import PaymentProvider

WEBHOOK_URL = "/billing/api/webhooks/stripe"

#: 2100-01-01T00:00:00Z — far enough out to be a real future deadline.
FUTURE = 4102444800
#: 2020-01-01T00:00:00Z — safely in the past.
PAST = 1577836800


def _basil_subscription(
    sub_id="sub_1",
    *,
    status="active",
    period_start=PAST,
    period_end=FUTURE,
    **extra,
):
    """A subscription object as Stripe sends it TODAY.

    The period is on the item and NOWHERE else — copied from the payload
    this deployment actually stored (evt_1UE56yCHorzvrdTvpvPPvbjz). A
    fixture that also puts it at the top level would pass against the
    reader that caused the outage.
    """
    return {
        "id": sub_id,
        "object": "subscription",
        "customer": "cus_1",
        "status": status,
        "cancel_at_period_end": False,
        "items": {
            "object": "list",
            "data": [
                {
                    "id": "si_1",
                    "object": "subscription_item",
                    "current_period_start": period_start,
                    "current_period_end": period_end,
                }
            ],
        },
        **extra,
    }


def _legacy_subscription(sub_id="sub_1", *, period_end=FUTURE, **extra):
    """The pre-basil shape: period at the top level, no items."""
    return {
        "id": sub_id,
        "customer": "cus_1",
        "status": "active",
        "current_period_start": PAST,
        "current_period_end": period_end,
        **extra,
    }


# ─── Provider doubles ───────────────────────────────────────


class RecordingProvider(PaymentProvider):
    """Verifies webhooks by decoding the body; serves a scripted re-read.

    ``subscriptions`` maps a provider id to the object
    :meth:`fetch_subscription` hands back — ``None`` for "gone", or an
    exception instance to raise.
    """

    name = "recording-test"
    subscriptions: dict = {}
    cancelled: list = []

    def create_checkout_session(self, *, user, package, plan, success_url, cancel_url):
        return ("https://rec.test/checkout", "cs_rec_1")

    def create_portal_session(self, *, customer_id, return_url):
        return "https://rec.test/portal"

    def cancel_subscription(self, subscription_id):
        type(self).cancelled.append(subscription_id)

    def fetch_subscription(self, subscription_id):
        value = type(self).subscriptions.get(subscription_id, KeyError(subscription_id))
        if isinstance(value, BaseException):
            raise value
        return value

    def verify_webhook(self, payload, signature):
        return json.loads(payload)


class BlindProvider(RecordingProvider):
    """A provider that never implemented the re-read (the base default)."""

    name = "blind"

    def fetch_subscription(self, subscription_id):
        return PaymentProvider.fetch_subscription(self, subscription_id)


PROVIDER_PATH = f"{RecordingProvider.__module__}.{RecordingProvider.__qualname__}"
BLIND_PROVIDER_PATH = f"{BlindProvider.__module__}.{BlindProvider.__qualname__}"


@pytest.fixture(autouse=True)
def provider(settings):
    from stapel_billing.conf import billing_settings

    RecordingProvider.subscriptions = {}
    RecordingProvider.cancelled = []
    settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": PROVIDER_PATH}
    yield RecordingProvider
    billing_settings.reload()


def _post(client, event):
    return client.post(
        WEBHOOK_URL,
        data=json.dumps(event),
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="good",
    )


def _event(event_id, type_, obj):
    return {"id": event_id, "type": type_, "data": {"object": obj}}


# ─── Where the period lives ─────────────────────────────────


@pytest.mark.django_db
class TestPeriodSource:
    """The production defect: a period the reader could not find."""

    def test_a_period_on_the_item_reaches_the_row(self, api_client, user):
        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_subscription_id="sub_1"
        )
        assert _post(
            api_client,
            _event("evt_1", "customer.subscription.updated", _basil_subscription()),
        ).status_code == 200

        sub.refresh_from_db()
        # This is the assertion that was false in production on all three
        # paid rows, with every webhook green.
        assert sub.current_period_end is not None
        assert sub.current_period_end.year == 2100
        assert sub.current_period_start is not None
        assert sub.current_period_start.year == 2020

    def test_the_pre_basil_shape_still_works(self, api_client, user):
        """One library, deployments pinned either side of the move."""
        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_subscription_id="sub_1"
        )
        assert _post(
            api_client,
            _event("evt_2", "customer.subscription.updated", _legacy_subscription()),
        ).status_code == 200

        sub.refresh_from_db()
        assert sub.current_period_end.year == 2100

    def test_several_items_are_covered_by_the_widest_window(self, api_client, user):
        """Service is owed until the LAST item's period ends.

        Taking the first item would expire the subscription's credits
        while the subscription is still running.
        """
        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_subscription_id="sub_1"
        )
        obj = _basil_subscription()
        obj["items"]["data"].append(
            {
                "id": "si_2",
                "current_period_start": PAST,
                "current_period_end": FUTURE + 86400 * 30,
            }
        )
        assert _post(
            api_client, _event("evt_3", "customer.subscription.updated", obj)
        ).status_code == 200

        sub.refresh_from_db()
        assert sub.current_period_end.timestamp() == FUTURE + 86400 * 30

    def test_a_parked_period_is_read_from_the_item_too(self, api_client, user):
        """The stash is the same reader, and had the same blind spot."""
        assert _post(
            api_client,
            _event("evt_4", "customer.subscription.created", _basil_subscription()),
        ).status_code == 200

        assert Subscription.objects.count() == 0
        pending = PendingSubscriptionPeriod.objects.get()
        assert pending.current_period_end is not None
        assert pending.current_period_end.year == 2100


# ─── Cancellation intent ────────────────────────────────────


@pytest.mark.django_db
class TestCancellationMirroring:
    def test_cancel_at_period_end_is_recorded(self, api_client, user):
        """Stripe keeps status='active'; the flag is the only difference.

        Both production starters looked exactly like an untouched
        subscription without it.
        """
        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_subscription_id="sub_1"
        )
        obj = _basil_subscription(cancel_at_period_end=True, canceled_at=PAST)
        assert _post(
            api_client, _event("evt_5", "customer.subscription.updated", obj)
        ).status_code == 200

        sub.refresh_from_db()
        assert sub.cancel_at_period_end is True
        assert sub.cancelled_at is not None
        assert sub.cancelled_at.year == 2020
        # Still entitled: the customer paid for the rest of the period.
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.is_active is True

    def test_undoing_a_cancellation_clears_the_local_timestamp(
        self, api_client, user
    ):
        sub = Subscription.objects.create(
            user=user,
            plan="pro",
            stripe_subscription_id="sub_1",
            cancel_at_period_end=True,
        )
        sub.cancelled_at = sub.created_at
        sub.save(update_fields=["cancelled_at"])

        assert _post(
            api_client,
            _event("evt_6", "customer.subscription.updated", _basil_subscription()),
        ).status_code == 200

        sub.refresh_from_db()
        assert sub.cancel_at_period_end is False
        assert sub.cancelled_at is None

    def test_deletion_uses_the_providers_own_timestamp(self, api_client, user):
        sub = Subscription.objects.create(
            user=user,
            plan="pro",
            stripe_subscription_id="sub_1",
            cancel_at_period_end=True,
        )
        obj = _basil_subscription(status="canceled", canceled_at=PAST)
        assert _post(
            api_client, _event("evt_7", "customer.subscription.deleted", obj)
        ).status_code == 200

        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.CANCELLED
        assert sub.cancelled_at.year == 2020
        # It has ended — a pending cancellation is no longer pending.
        assert sub.cancel_at_period_end is False
        assert sub.is_active is False


# ─── Status mirroring ───────────────────────────────────────


@pytest.mark.django_db
class TestStatusMirroring:
    @pytest.mark.parametrize(
        "stripe_status,expected",
        [
            ("unpaid", SubscriptionStatus.UNPAID),
            ("incomplete_expired", SubscriptionStatus.INCOMPLETE_EXPIRED),
            ("paused", SubscriptionStatus.PAUSED),
            ("past_due", SubscriptionStatus.PAST_DUE),
            ("trialing", SubscriptionStatus.TRIALING),
        ],
    )
    def test_every_stripe_status_lands(
        self, api_client, user, stripe_status, expected
    ):
        """Unmapped used to mean "keep active and say nothing"."""
        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_subscription_id="sub_1"
        )
        assert _post(
            api_client,
            _event(
                f"evt_{stripe_status}",
                "customer.subscription.updated",
                _basil_subscription(status=stripe_status),
            ),
        ).status_code == 200

        sub.refresh_from_db()
        assert sub.status == expected

    def test_an_unknown_status_is_logged_rather_than_swallowed(
        self, api_client, user, caplog
    ):
        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_subscription_id="sub_1"
        )
        with caplog.at_level("WARNING"):
            assert _post(
                api_client,
                _event(
                    "evt_unknown",
                    "customer.subscription.updated",
                    _basil_subscription(status="quantum_superposed"),
                ),
            ).status_code == 200

        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE  # kept: safest action
        assert "quantum_superposed" in caplog.text  # but no longer silent


# ─── is_paid / is_active ────────────────────────────────────


@pytest.mark.django_db
class TestPaidAndActive:
    def test_the_free_row_is_neither_paid_nor_cancellable(self, user):
        """111 production rows look exactly like this."""
        sub = Subscription.objects.create(user=user)  # plan=free, status=active
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.is_paid is False

    def test_a_paid_plan_without_a_provider_id_is_not_paid(self, user):
        """A failed checkout can leave the plan moved and nothing behind it."""
        sub = Subscription.objects.create(user=user, plan="pro")
        assert sub.is_paid is False

    def test_a_provider_id_on_a_free_plan_is_not_paid(self, user):
        sub = Subscription.objects.create(
            user=user, plan="free", stripe_subscription_id="sub_old"
        )
        assert sub.is_paid is False

    def test_paid_and_active(self, user):
        from django.utils import timezone
        from datetime import timedelta

        sub = Subscription.objects.create(
            user=user,
            plan="pro",
            stripe_subscription_id="sub_1",
            current_period_end=timezone.now() + timedelta(days=5),
        )
        assert sub.is_paid is True
        assert sub.is_active is True

    def test_an_expired_period_is_not_active_whatever_the_status_says(self, user):
        """The exact shape of a renewal webhook this deployment missed."""
        from django.utils import timezone
        from datetime import timedelta

        sub = Subscription.objects.create(
            user=user,
            plan="pro",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_1",
            current_period_end=timezone.now() - timedelta(days=1),
        )
        assert sub.is_paid is True
        assert sub.is_active is False

    def test_no_period_is_trusted_on_status_alone(self, user):
        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_subscription_id="sub_1"
        )
        assert sub.is_active is True

    @pytest.mark.parametrize(
        "status,active",
        [
            (SubscriptionStatus.ACTIVE, True),
            (SubscriptionStatus.TRIALING, True),
            (SubscriptionStatus.PAST_DUE, False),
            (SubscriptionStatus.UNPAID, False),
            (SubscriptionStatus.PAUSED, False),
            (SubscriptionStatus.CANCELLED, False),
            (SubscriptionStatus.INCOMPLETE, False),
            (SubscriptionStatus.INCOMPLETE_EXPIRED, False),
        ],
    )
    def test_only_entitling_statuses_are_active(self, user, status, active):
        sub = Subscription.objects.create(
            user=user, plan="pro", status=status, stripe_subscription_id="sub_1"
        )
        assert sub.is_active is active


@pytest.mark.django_db
class TestSubscriptionPayload:
    def test_the_free_plan_answer_says_it_is_not_paid(self, authed_client):
        resp = authed_client.get("/billing/api/subscription")
        assert resp.status_code == 200
        body = resp.json()
        assert body["plan"] == "free"
        assert body["status"] == "active"
        # The three fields a client renders from. Without them the only
        # honest reading of the two above is "subscribed", which is wrong.
        #
        # `is_paid` is the one that excludes this row; `is_active` answers a
        # different question (does the STATUS entitle, and is the period
        # still running) and says yes here, because a free row's status is
        # `active` and it has no period to have run out of. That is why an
        # affordance takes BOTH — reading either alone is the bug.
        assert body["is_paid"] is False
        assert body["is_active"] is True
        assert body["cancel_at_period_end"] is False

    def test_a_leaving_subscriber_is_distinguishable(self, authed_client, user):
        from django.utils import timezone
        from datetime import timedelta

        ends = timezone.now() + timedelta(days=12)
        Subscription.objects.create(
            user=user,
            plan="pro",
            stripe_subscription_id="sub_1",
            current_period_end=ends,
            cancel_at_period_end=True,
            cancelled_at=timezone.now(),
        )
        body = authed_client.get("/billing/api/subscription").json()
        assert body["is_paid"] is True
        assert body["is_active"] is True
        assert body["cancel_at_period_end"] is True
        assert body["current_period_end"] is not None


# ─── Cancel ─────────────────────────────────────────────────


@pytest.mark.django_db
class TestCancelRefusal:
    def test_cancelling_the_free_plan_is_refused_by_name(self, authed_client, user):
        sub = Subscription.objects.create(user=user)
        resp = authed_client.post("/billing/api/subscription/cancel")
        assert resp.status_code == 409
        assert ERR_409_SUBSCRIPTION_NOT_PAID in resp.content.decode()
        sub.refresh_from_db()
        # And it did NOT stamp a cancellation on an account that never paid.
        assert sub.cancelled_at is None
        assert RecordingProvider.cancelled == []

    def test_a_paid_plan_with_no_provider_object_is_refused(
        self, authed_client, user
    ):
        Subscription.objects.create(user=user, plan="pro")
        resp = authed_client.post("/billing/api/subscription/cancel")
        assert resp.status_code == 409
        assert RecordingProvider.cancelled == []

    def test_a_real_cancel_leaves_the_period_the_customer_paid_for(
        self, authed_client, user
    ):
        from django.utils import timezone
        from datetime import timedelta

        sub = Subscription.objects.create(
            user=user,
            plan="pro",
            stripe_subscription_id="sub_1",
            current_period_end=timezone.now() + timedelta(days=9),
        )
        resp = authed_client.post("/billing/api/subscription/cancel")
        assert resp.status_code == 200
        assert RecordingProvider.cancelled == ["sub_1"]

        body = resp.json()
        assert body["cancel_at_period_end"] is True
        assert body["cancelled_at"] is not None
        # Still entitled — the provider cancels AT PERIOD END.
        assert body["is_active"] is True
        assert body["status"] == "active"

        sub.refresh_from_db()
        assert sub.cancel_at_period_end is True
        assert sub.status == SubscriptionStatus.ACTIVE


# ─── Reconciliation ─────────────────────────────────────────


@pytest.mark.django_db
class TestReconcileSubscriptions:
    def _drifted(self, user, **kwargs):
        """A row exactly as production left it: active, no period."""
        return Subscription.objects.create(
            user=user,
            plan="starter",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_1",
            current_period_start=None,
            current_period_end=None,
            **kwargs,
        )

    def test_a_dry_run_reports_and_writes_nothing(self, user):
        from stapel_billing import services

        sub = self._drifted(user)
        RecordingProvider.subscriptions = {
            "sub_1": _basil_subscription(cancel_at_period_end=True, canceled_at=PAST)
        }

        results = services.reconcile_subscriptions(dry_run=True)
        assert len(results) == 1
        result = results[0]
        assert result.applied is False
        assert set(result.changed) == {
            "current_period_start",
            "current_period_end",
            "cancel_at_period_end",
            "cancelled_at",
        }
        assert result.before["current_period_end"] is None
        assert result.after["current_period_end"].year == 2100

        sub.refresh_from_db()
        assert sub.current_period_end is None
        assert sub.cancel_at_period_end is False

    def test_the_real_run_repairs_the_row(self, user):
        from stapel_billing import services

        sub = self._drifted(user)
        RecordingProvider.subscriptions = {
            "sub_1": _basil_subscription(cancel_at_period_end=True, canceled_at=PAST)
        }

        (result,) = services.reconcile_subscriptions()
        assert result.applied is True

        sub.refresh_from_db()
        assert sub.current_period_end.year == 2100
        assert sub.current_period_start.year == 2020
        assert sub.cancel_at_period_end is True
        assert sub.cancelled_at.year == 2020
        assert sub.status == SubscriptionStatus.ACTIVE

    def test_it_is_idempotent(self, user):
        from stapel_billing import services

        self._drifted(user)
        RecordingProvider.subscriptions = {"sub_1": _basil_subscription()}

        first = services.reconcile_subscriptions()
        assert first[0].changed
        second = services.reconcile_subscriptions()
        assert second[0].changed == ()
        assert second[0].applied is False

    def test_it_dates_the_bundle_the_missing_period_left_undated(self, user):
        """Repairing the dates is only worth it if the lots move with them."""
        from stapel_billing import services
        from stapel_billing.models import CreditLot, LotSource

        sub = self._drifted(user)
        txn = services.credit(
            user=user,
            credits=100,
            type="subscription_bonus",
            source=LotSource.SUBSCRIPTION,
            expires_at=None,
            metadata={"stripe_subscription_id": sub.stripe_subscription_id},
        )
        lot = CreditLot.objects.get(granting_transaction=txn)
        assert lot.expires_at is None

        RecordingProvider.subscriptions = {"sub_1": _basil_subscription()}
        services.reconcile_subscriptions()

        lot.refresh_from_db()
        assert lot.expires_at is not None
        assert lot.expires_at.year == 2100

    def test_free_rows_are_not_examined(self, user):
        from stapel_billing import services

        Subscription.objects.create(user=user)  # free, no provider id
        assert services.reconcile_subscriptions() == []

    def test_a_subscription_gone_at_the_provider_is_reported_not_guessed(
        self, user
    ):
        from stapel_billing import services

        sub = self._drifted(user)
        RecordingProvider.subscriptions = {"sub_1": None}

        (result,) = services.reconcile_subscriptions()
        assert result.missing is True
        assert result.applied is False
        sub.refresh_from_db()
        # Cancelling it here would be guessing at WHY it is gone.
        assert sub.status == SubscriptionStatus.ACTIVE

    def test_one_unreadable_row_does_not_abandon_the_rest(self, user, django_user_model):
        from stapel_billing import services

        self._drifted(user)
        other = django_user_model.objects.create_user(
            username="second", email="second@example.com", password="x"
        )
        Subscription.objects.create(
            user=other, plan="starter", stripe_subscription_id="sub_2"
        )
        RecordingProvider.subscriptions = {
            "sub_1": RuntimeError("stripe is down"),
            "sub_2": _basil_subscription("sub_2"),
        }

        results = services.reconcile_subscriptions()
        by_id = {r.stripe_subscription_id: r for r in results}
        assert "stripe is down" in by_id["sub_1"].error
        assert by_id["sub_2"].applied is True

    def test_it_can_be_narrowed_to_named_subscriptions(self, user, django_user_model):
        from stapel_billing import services

        self._drifted(user)
        other = django_user_model.objects.create_user(
            username="third", email="third@example.com", password="x"
        )
        Subscription.objects.create(
            user=other, plan="starter", stripe_subscription_id="sub_2"
        )
        RecordingProvider.subscriptions = {"sub_2": _basil_subscription("sub_2")}

        results = services.reconcile_subscriptions(stripe_subscription_ids=["sub_2"])
        assert [r.stripe_subscription_id for r in results] == ["sub_2"]

    def test_a_provider_that_cannot_re_read_says_so_per_row(self, user, settings):
        """The default is NotImplementedError, not a false all-clear."""
        from stapel_billing import services
        from stapel_billing.conf import billing_settings

        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": BLIND_PROVIDER_PATH}
        billing_settings.reload()
        self._drifted(user)

        (result,) = services.reconcile_subscriptions()
        assert "NotImplementedError" in result.error
        assert result.applied is False


@pytest.mark.django_db
class TestReconcileCommand:
    def test_the_dry_run_prints_before_and_after_and_writes_nothing(self, user):
        from io import StringIO
        from django.core.management import call_command

        sub = Subscription.objects.create(
            user=user, plan="starter", stripe_subscription_id="sub_1"
        )
        RecordingProvider.subscriptions = {"sub_1": _basil_subscription()}

        out = StringIO()
        call_command("billing_reconcile_subscriptions", "--dry-run", stdout=out)
        printed = out.getvalue()

        assert "sub_1" in printed
        assert "current_period_end: NULL -> " in printed
        assert "dry run — nothing was written" in printed
        sub.refresh_from_db()
        assert sub.current_period_end is None

    def test_the_real_run_writes_and_the_second_is_clean(self, user):
        from io import StringIO
        from django.core.management import call_command

        sub = Subscription.objects.create(
            user=user, plan="starter", stripe_subscription_id="sub_1"
        )
        RecordingProvider.subscriptions = {"sub_1": _basil_subscription()}

        call_command("billing_reconcile_subscriptions", stdout=StringIO())
        sub.refresh_from_db()
        assert sub.current_period_end.year == 2100

        out = StringIO()
        call_command("billing_reconcile_subscriptions", stdout=StringIO(), stderr=out)
        again = StringIO()
        call_command("billing_reconcile_subscriptions", stdout=again)
        assert "0 repaired" in again.getvalue()

    def test_an_unreadable_row_exits_non_zero(self, user):
        from io import StringIO
        from django.core.management import call_command

        Subscription.objects.create(
            user=user, plan="starter", stripe_subscription_id="sub_1"
        )
        RecordingProvider.subscriptions = {"sub_1": RuntimeError("stripe is down")}

        with pytest.raises(SystemExit):
            call_command(
                "billing_reconcile_subscriptions", stdout=StringIO(), stderr=StringIO()
            )
