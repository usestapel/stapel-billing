"""Lifecycle state is applied in PROVIDER time, not delivery order.

Measured on a client host running 0.19.0, not imagined. Stripe delivered
``customer.subscription.created`` (object status ``incomplete``, event
``created`` = T) AFTER ``customer.subscription.updated`` (status
``active``, ``created`` = T+2). Both types share
``services.handle_subscription_updated``, which wrote whatever the payload
said in arrival order — so the older ``incomplete`` landed on top of
``active``, the plan stopped entitling, and the paying customer was
bounced to the paywall. Two of six provider-backed subscriptions on that
host were stuck that way, with every webhook received, processed and
green.

Each test here fails on 0.19.0.
"""

import json

import pytest

from stapel_billing import services
from stapel_billing.conf import billing_settings
from stapel_billing.models import (
    CreditLot,
    PendingSubscriptionPeriod,
    StripeWebhookEvent,
    Subscription,
    SubscriptionStatus,
    Transaction,
    Wallet,
)
from stapel_billing.providers.base import PaymentProvider

from .stripe_ids import sid
from .test_webhooks import PROVIDER_PATH, WEBHOOK_URL, _checkout_event

#: 2026-09-15T12:00:00Z. Whole seconds, because Stripe stamps whole seconds.
T = 1789473600
#: 2100-01-01T00:00:00Z
PERIOD_END = 4102444800

SUB = sid("sub", "order")
CUS = sid("cus", "order")


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


def _sub_event(seed, type_, *, status, created, period_end=PERIOD_END, sub_id=SUB):
    obj = {"id": sub_id, "customer": CUS, "status": status}
    if period_end is not None:
        obj["current_period_end"] = period_end
    return {
        "id": sid("evt", seed),
        "type": type_,
        "created": created,
        "data": {"object": obj},
    }


def _checkout(user, *, created):
    event = _checkout_event(user, event_id=sid("evt", "order_checkout"), plan="pro")
    event["created"] = created
    event["data"]["object"]["subscription"] = SUB
    event["data"]["object"]["customer"] = CUS
    return event


def _log(event):
    return StripeWebhookEvent.objects.get(stripe_event_id=event["id"])


@pytest.mark.django_db
class TestOutOfOrderDelivery:
    def test_a_late_created_does_not_undo_a_newer_active(self, api_client, user):
        """The incident, reproduced: `.created(incomplete)` lands last."""
        assert _post(api_client, _checkout(user, created=T - 10)).status_code == 200
        updated = _sub_event(
            "order_updated", "customer.subscription.updated", status="active",
            created=T + 2,
        )
        assert _post(api_client, updated).status_code == 200

        late = _sub_event(
            "order_created", "customer.subscription.created", status="incomplete",
            created=T,
        )
        resp = _post(api_client, late)
        # Acknowledged — Stripe must not retry an event we deliberately drop.
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        sub = Subscription.objects.get(user=user)
        assert sub.status == SubscriptionStatus.ACTIVE
        # Processed AND ignored: the row does not reflect this event on purpose.
        log = _log(late)
        assert log.processed_at is not None
        assert log.ignored_stale is True
        assert _log(updated).ignored_stale is False
        # The plan bonus was granted exactly once by the checkout.
        assert Wallet.objects.get(user=user).balance == 300
        assert Transaction.objects.filter(wallet__user=user).count() == 1

    def test_the_normal_order_still_activates(self, api_client, user):
        assert _post(api_client, _checkout(user, created=T - 10)).status_code == 200
        assert _post(
            api_client,
            _sub_event(
                "order_created2", "customer.subscription.created",
                status="incomplete", created=T,
            ),
        ).status_code == 200
        assert _post(
            api_client,
            _sub_event(
                "order_updated2", "customer.subscription.updated",
                status="active", created=T + 2,
            ),
        ).status_code == 200

        sub = Subscription.objects.get(user=user)
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.last_provider_event_at is not None

    def test_equal_timestamps_let_the_more_advanced_status_win(
        self, api_client, user
    ):
        """Stripe stamps whole seconds; two payloads can tie.

        The lifecycle only runs one way — a subscription becomes active out
        of `incomplete`, never back — so on a tie the more advanced status
        is the later truth (services.LIFECYCLE_RANK).
        """
        assert _post(api_client, _checkout(user, created=T - 10)).status_code == 200
        assert _post(
            api_client,
            _sub_event(
                "tie_updated", "customer.subscription.updated", status="active",
                created=T,
            ),
        ).status_code == 200
        tie = _sub_event(
            "tie_created", "customer.subscription.created", status="incomplete",
            created=T,
        )
        assert _post(api_client, tie).status_code == 200

        assert Subscription.objects.get(user=user).status == SubscriptionStatus.ACTIVE
        assert _log(tie).ignored_stale is True

    def test_a_genuinely_newer_downgrade_still_applies(self, api_client, user):
        """The guard must not become "nothing can ever go down"."""
        assert _post(api_client, _checkout(user, created=T - 10)).status_code == 200
        assert _post(
            api_client,
            _sub_event(
                "down_active", "customer.subscription.updated", status="active",
                created=T,
            ),
        ).status_code == 200
        assert _post(
            api_client,
            _sub_event(
                "down_pastdue", "customer.subscription.updated", status="past_due",
                created=T + 5,
            ),
        ).status_code == 200
        assert Subscription.objects.get(user=user).status == (
            SubscriptionStatus.PAST_DUE
        )

        assert _post(
            api_client,
            _sub_event(
                "down_cancel", "customer.subscription.deleted", status="canceled",
                created=T + 10,
            ),
        ).status_code == 200
        sub = Subscription.objects.get(user=user)
        assert sub.status == SubscriptionStatus.CANCELLED
        assert sub.cancelled_at is not None

    def test_a_stale_deletion_does_not_cancel_a_live_subscription(
        self, api_client, user
    ):
        assert _post(api_client, _checkout(user, created=T - 10)).status_code == 200
        assert _post(
            api_client,
            _sub_event(
                "stale_del_active", "customer.subscription.updated", status="active",
                created=T + 5,
            ),
        ).status_code == 200
        stale = _sub_event(
            "stale_del", "customer.subscription.deleted", status="canceled",
            created=T,
        )
        assert _post(api_client, stale).status_code == 200

        sub = Subscription.objects.get(user=user)
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.cancelled_at is None
        assert _log(stale).ignored_stale is True

    def test_a_stale_checkout_grants_but_does_not_resurrect_the_status(
        self, api_client, user
    ):
        """Money is a separate fact from lifecycle state."""
        Subscription.objects.create(
            user=user,
            plan="pro",
            status=SubscriptionStatus.CANCELLED,
            stripe_subscription_id=SUB,
            stripe_customer_id=CUS,
            last_provider_event_at=services._epoch_to_datetime(T + 100),
        )
        assert _post(api_client, _checkout(user, created=T)).status_code == 200

        sub = Subscription.objects.get(user=user)
        assert sub.status == SubscriptionStatus.CANCELLED
        # The purchase still granted what it paid for.
        assert Wallet.objects.get(user=user).balance == 300
        assert CreditLot.objects.filter(wallet__user=user).count() == 1


@pytest.mark.django_db
class TestTheStashIsOrderedToo:
    def test_an_older_event_does_not_replace_a_newer_parked_period(
        self, api_client, user
    ):
        newer = _sub_event(
            "stash_new", "customer.subscription.updated", status="active",
            created=T + 2,
        )
        older = _sub_event(
            "stash_old", "customer.subscription.created", status="incomplete",
            created=T,
        )
        assert _post(api_client, newer).status_code == 200
        assert _post(api_client, older).status_code == 200
        assert Subscription.objects.count() == 0

        pending = PendingSubscriptionPeriod.objects.get()
        assert pending.status == "active"
        assert _log(older).ignored_stale is True

        # And the checkout that lands afterwards claims the NEWER truth.
        assert _post(api_client, _checkout(user, created=T + 3)).status_code == 200
        sub = Subscription.objects.get(user=user)
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.current_period_end is not None
        assert PendingSubscriptionPeriod.objects.count() == 0


class FetchingProvider(PaymentProvider):
    """A provider whose `fetch_subscription` answers from a dict."""

    name = "fetching-test"
    subscriptions: dict = {}

    def create_checkout_session(self, *, user, package, plan, success_url, cancel_url):
        return ("https://fetch.test/checkout", sid("cs", "fetch"))

    def create_portal_session(self, *, customer_id, return_url):
        return "https://fetch.test/portal"

    def cancel_subscription(self, subscription_id):
        return None

    def verify_webhook(self, payload, signature):
        return json.loads(payload)

    def fetch_subscription(self, subscription_id):
        return self.subscriptions.get(subscription_id)


FETCHING_PATH = f"{FetchingProvider.__module__}.{FetchingProvider.__qualname__}"


@pytest.mark.django_db
class TestReconcileOwnsTheClockToo:
    def test_a_stale_event_cannot_undo_a_reconcile(self, api_client, user, settings):
        """The repair has to survive the event that arrives a second later."""
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": FETCHING_PATH}
        billing_settings.reload()
        Subscription.objects.create(
            user=user,
            plan="pro",
            status=SubscriptionStatus.INCOMPLETE,
            stripe_subscription_id=SUB,
            stripe_customer_id=CUS,
        )
        FetchingProvider.subscriptions = {
            SUB: {"id": SUB, "status": "active", "current_period_end": PERIOD_END}
        }

        (result,) = services.reconcile_subscriptions()
        assert result.applied is True
        sub = Subscription.objects.get(user=user)
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.last_provider_event_at is not None

        # An event created long before the reconcile, delivered after it.
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": FETCHING_PATH}
        billing_settings.reload()
        stale = _sub_event(
            "reconciled_stale", "customer.subscription.updated",
            status="incomplete", created=T,
        )
        assert _post(api_client, stale).status_code == 200

        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE
        assert _log(stale).ignored_stale is True

    def test_a_reconcile_that_changes_nothing_still_stamps_the_clock(
        self, user, settings
    ):
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": FETCHING_PATH}
        billing_settings.reload()
        Subscription.objects.create(
            user=user,
            plan="pro",
            status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id=SUB,
        )
        FetchingProvider.subscriptions = {SUB: {"id": SUB, "status": "active"}}

        (result,) = services.reconcile_subscriptions()
        assert result.changed == ()
        assert Subscription.objects.get(user=user).last_provider_event_at is not None

    def test_a_dry_run_stamps_nothing(self, user, settings):
        settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": FETCHING_PATH}
        billing_settings.reload()
        Subscription.objects.create(
            user=user,
            plan="pro",
            status=SubscriptionStatus.INCOMPLETE,
            stripe_subscription_id=SUB,
        )
        FetchingProvider.subscriptions = {SUB: {"id": SUB, "status": "active"}}

        services.reconcile_subscriptions(dry_run=True)
        sub = Subscription.objects.get(user=user)
        assert sub.status == SubscriptionStatus.INCOMPLETE
        assert sub.last_provider_event_at is None
