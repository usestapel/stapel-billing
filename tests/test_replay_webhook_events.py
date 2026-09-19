"""Replaying a webhook the provider will never send again.

The incident this closes, measured on a live host: a
``customer.subscription.updated`` carrying ``incomplete_expired`` landed
on a deployment whose ``PendingSubscriptionPeriod.status`` was still
``varchar(16)``. Every delivery raised ``StringDataRightTruncation``, the
view answered 500, and Stripe retried for its three days and stopped. The
column was widened the next day — and nothing re-delivered the event. The
row sat in the log with ``processed_at IS NULL`` and an error from a
defect that no longer existed, and ``billing_invariants`` counted it
forever.

A replay is therefore a repair tool, and its whole safety argument is that
it is NOT a second delivery path: it runs
``services.apply_stored_event``, which is what the view runs. The tests
below pin the four properties that makes it safe to point at production —
a failed event replays, a processed one is a no-op, a stale one is
recorded ignored-stale rather than applied, and two replays do not grant
twice.
"""

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from stapel_billing import services
from stapel_billing.conf import billing_settings
from stapel_billing.models import (
    PendingSubscriptionPeriod,
    StripeWebhookEvent,
    Subscription,
    SubscriptionStatus,
    Transaction,
    Wallet,
)

from .stripe_ids import LONGEST_PROVIDER_STATUS, sid
from .test_webhooks import PROVIDER_PATH, WEBHOOK_URL, _checkout_event

#: 2026-09-18T01:15:52Z — the hour the real event landed.
T = 1789693000
#: 2100-01-01T00:00:00Z
PERIOD_END = 4102444800

SUB = sid("sub", "replay")
CUS = sid("cus", "replay")


@pytest.fixture(autouse=True)
def _json_provider(settings):
    settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": PROVIDER_PATH}
    yield
    billing_settings.reload()


def _post(client, event):
    return client.post(
        WEBHOOK_URL,
        data=json.dumps(event),
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="good",
    )


def _sub_event(seed, *, status, created, sub_id=SUB):
    """A ``customer.subscription.updated`` the way the provider sends one."""
    return {
        "id": sid("evt", seed),
        "type": "customer.subscription.updated",
        "created": created,
        "data": {
            "object": {
                "id": sub_id,
                "customer": CUS,
                "status": status,
                "current_period_end": PERIOD_END,
            }
        },
    }


def _store_failed(event, error="value too long for type character varying(16)"):
    """The row a failed delivery leaves behind: payload kept, error, no mark."""
    return StripeWebhookEvent.objects.create(
        stripe_event_id=event["id"],
        event_type=event["type"],
        payload=event,
        error=error,
    )


@pytest.mark.django_db
class TestReplayOfAFailedEvent:
    def test_a_stored_failure_is_replayed_and_the_error_cleared(self):
        """The incident, end to end: the event applies, the row goes green."""
        event = _sub_event("stash", status=LONGEST_PROVIDER_STATUS, created=T)
        log = _store_failed(event)

        results = services.replay_webhook_events(unprocessed=True)

        assert [r.outcome for r in results] == [services.EVENT_PROCESSED]
        assert results[0].previous_error.startswith("value too long")
        log.refresh_from_db()
        assert log.processed_at is not None
        assert log.error == ""
        assert not log.ignored_stale
        # The payload's whole point: the period it carried is now parked
        # under the provider id, with the RAW status word at full width.
        stash = PendingSubscriptionPeriod.objects.get(stripe_subscription_id=SUB)
        assert stash.status == LONGEST_PROVIDER_STATUS
        assert StripeWebhookEvent.objects.filter(processed_at__isnull=True).count() == 0

    def test_dry_run_runs_no_handler_and_changes_nothing(self):
        event = _sub_event("dry", status=LONGEST_PROVIDER_STATUS, created=T)
        log = _store_failed(event)

        results = services.replay_webhook_events(unprocessed=True, dry_run=True)

        assert [r.outcome for r in results] == ["pending"]
        log.refresh_from_db()
        assert log.processed_at is None
        assert log.error.startswith("value too long")
        assert not PendingSubscriptionPeriod.objects.exists()

    def test_a_named_event_is_replayed_without_touching_the_others(self):
        wanted = _store_failed(_sub_event("named", status="active", created=T))
        other = _store_failed(_sub_event("other", status="active", created=T,
                                         sub_id=sid("sub", "other")))

        results = services.replay_webhook_events(event_ids=[wanted.stripe_event_id])

        assert [r.stripe_event_id for r in results] == [wanted.stripe_event_id]
        wanted.refresh_from_db()
        other.refresh_from_db()
        assert wanted.processed_at is not None
        assert other.processed_at is None

    def test_a_replay_that_fails_again_records_the_new_error_and_exits_nonzero(
        self, monkeypatch, capsys
    ):
        """A repair that did not repair must not read as done."""
        log = _store_failed(_sub_event("boom", status="active", created=T))

        def _boom(evt):
            raise RuntimeError("still broken")

        monkeypatch.setattr(services, "handle_subscription_updated", _boom)
        with pytest.raises(SystemExit):
            call_command("billing_replay_webhook_events", "--unprocessed")
        log.refresh_from_db()
        assert log.processed_at is None
        assert "still broken" in log.error


@pytest.mark.django_db
class TestReplayIsIdempotent:
    def test_replaying_a_processed_event_is_a_no_op(self, api_client, user):
        """Named explicitly, so the no-op is a tested answer, not an absence."""
        event = _checkout_event(user, package="starter", event_id=sid("evt", "done"))
        assert _post(api_client, event).status_code == 200
        wallet = Wallet.objects.get(user=user)
        assert wallet.balance == 500
        processed_at = StripeWebhookEvent.objects.get(
            stripe_event_id=event["id"]
        ).processed_at

        results = services.replay_webhook_events(event_ids=[event["id"]])

        assert [r.outcome for r in results] == [services.EVENT_DUPLICATE]
        wallet.refresh_from_db()
        assert wallet.balance == 500
        assert Transaction.objects.filter(wallet=wallet).count() == 1
        log = StripeWebhookEvent.objects.get(stripe_event_id=event["id"])
        assert log.processed_at == processed_at  # not re-stamped

    def test_double_replay_does_not_double_grant(self, user):
        """Two replays of a paid checkout credit the wallet exactly once.

        The grant claim (``ProviderGrant``) is what holds, not the
        ``processed_at`` short-circuit — so the assertion is on the money,
        and the second replay is forced past the short-circuit by clearing
        the mark, the way an operator re-running a half-repaired row would.
        """
        event = _checkout_event(user, package="starter", event_id=sid("evt", "twice"))
        log = _store_failed(event, error="transient")

        first = services.replay_webhook_events(event_ids=[event["id"]])
        assert [r.outcome for r in first] == [services.EVENT_PROCESSED]
        wallet = Wallet.objects.get(user=user)
        assert wallet.balance == 500

        StripeWebhookEvent.objects.filter(pk=log.pk).update(processed_at=None)
        second = services.replay_webhook_events(event_ids=[event["id"]])
        assert [r.outcome for r in second] == [services.EVENT_PROCESSED]

        wallet.refresh_from_db()
        assert wallet.balance == 500
        assert Transaction.objects.filter(wallet=wallet).count() == 1


@pytest.mark.django_db
class TestReplayObeysProviderTime:
    def test_a_stale_replay_is_recorded_ignored_stale_and_applies_nothing(
        self, api_client, user
    ):
        """The guard the replay leans on, exercised through the replay.

        An operator replaying an old failure must not be able to walk a
        live subscription backwards — that is the 0.20.0 incident, and a
        replay is exactly the situation that would re-create it.
        """
        # A live subscription, moved to `active` by a NEWER event.
        checkout = _checkout_event(user, plan="pro", event_id=sid("evt", "st_co"))
        checkout["created"] = T
        checkout["data"]["object"]["subscription"] = SUB
        checkout["data"]["object"]["customer"] = CUS
        assert _post(api_client, checkout).status_code == 200
        assert _post(api_client, _sub_event("st_new", status="active",
                                            created=T + 100)).status_code == 200
        sub = Subscription.objects.get(stripe_subscription_id=SUB)
        assert sub.status == SubscriptionStatus.ACTIVE

        # An OLDER failure, stored and never applied, now replayed.
        log = _store_failed(_sub_event("st_old", status="incomplete", created=T - 100))
        results = services.replay_webhook_events(event_ids=[log.stripe_event_id])

        assert [r.outcome for r in results] == [services.EVENT_IGNORED_STALE]
        log.refresh_from_db()
        assert log.processed_at is not None
        assert log.ignored_stale is True
        assert log.error == ""
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE  # NOT walked backwards


@pytest.mark.django_db
class TestReplayCommandSurface:
    def test_selecting_nothing_refuses_instead_of_replaying_everything(self):
        with pytest.raises(CommandError):
            call_command("billing_replay_webhook_events")

    def test_unprocessed_selects_exactly_what_the_invariant_counts(self):
        failed = _store_failed(_sub_event("inv", status="active", created=T))
        StripeWebhookEvent.objects.create(
            stripe_event_id=sid("evt", "inv_done"),
            event_type="customer.subscription.updated",
            payload=_sub_event("inv_done", status="active", created=T),
            processed_at=timezone.now(),
        )
        selected = services.replayable_events(unprocessed=True)
        assert [e.stripe_event_id for e in selected] == [failed.stripe_event_id]


def test_the_view_and_the_replay_run_the_same_wrapping():
    """Two delivery paths drift; there must be only one.

    The view's job is the signature, the claim and the HTTP answer. The
    lock, the registry, the stale mark and the processed mark belong to
    ``apply_stored_event`` — if the view grows its own copy again, this
    fails.
    """
    import inspect

    from stapel_billing import views

    source = inspect.getsource(views.StripeWebhookView.post)
    assert "services.apply_stored_event" in source
    assert "select_for_update" not in source
    assert "processed_at = " not in source
