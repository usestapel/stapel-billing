"""StripeWebhookView end-to-end matrix.

Uses a JSON test provider (registered via the PAYMENT_PROVIDER dotted
path) so the full view → services → handlers pipeline runs without the
Stripe SDK: the "signature" header selects the verification outcome.
"""

import json

import pytest

from stapel_billing.catalog import CREDIT_PACKAGES_BY_SLUG, PLANS_BY_SLUG
from stapel_billing.conf import billing_settings
from stapel_billing.errors import (
    ERR_400_INVALID_STRIPE_SIGNATURE,
    ERR_400_INVALID_WEBHOOK_PAYLOAD,
)
from stapel_billing.models import (
    StripeWebhookEvent,
    Subscription,
    SubscriptionStatus,
    Transaction,
    Wallet,
)
from stapel_billing.providers.base import PaymentProvider

from .stripe_ids import LONGEST_PROVIDER_STATUS, assert_realistic, sid

WEBHOOK_URL = "/billing/api/webhooks/stripe"


class JsonWebhookProvider(PaymentProvider):
    """Verifies by header value; decodes the JSON body as the event."""

    name = "json-test"

    def create_checkout_session(self, *, user, package, plan, success_url, cancel_url):
        return ("https://json.test/checkout", sid("cs", "json_1"))

    def create_portal_session(self, *, customer_id, return_url):
        return "https://json.test/portal"

    def cancel_subscription(self, subscription_id):
        return None

    def verify_webhook(self, payload, signature):
        if signature == "bad":
            raise ValueError("invalid signature")
        if signature == "crash":
            raise RuntimeError("verification exploded")
        return json.loads(payload)


PROVIDER_PATH = f"{JsonWebhookProvider.__module__}.{JsonWebhookProvider.__qualname__}"


@pytest.fixture(autouse=True)
def _json_provider(settings):
    settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": PROVIDER_PATH}
    yield
    billing_settings.reload()


def _post(client, event, signature="good"):
    return client.post(
        WEBHOOK_URL,
        data=json.dumps(event),
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=signature,
    )


def _checkout_event(user, event_id=sid("evt", "pkg_1"), session_id=sid("cs", "1"), **metadata):
    """A checkout.session.completed shaped the way Stripe actually sends one.

    The session-level fields (mode, payment_status, currency, amount_total)
    are exactly what the grant is reconciled against, so a helper that left
    them out would only ever exercise the refusal path.
    """
    if metadata.get("package"):
        entry = CREDIT_PACKAGES_BY_SLUG[metadata["package"]]
        settled = {"mode": "payment", "amount_total": entry.price_cents}
    elif metadata.get("plan"):
        entry = PLANS_BY_SLUG[metadata["plan"]]
        settled = {"mode": "subscription", "amount_total": entry.price_cents}
    else:
        entry, settled = None, {"mode": "payment", "amount_total": 0}
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "customer": sid("cus", "1"),
                "subscription": sid("sub", "1"),
                "payment_status": "paid",
                "currency": (entry.currency if entry else "USD").lower(),
                "client_reference_id": str(user.id),
                "metadata": {"user_id": str(user.id), **metadata},
                **settled,
            }
        },
    }


@pytest.mark.django_db
class TestWebhookRejections:
    def test_bad_signature_returns_400_and_stores_nothing(self, api_client):
        resp = _post(api_client, {"id": sid("evt", "x"), "type": "t"}, signature="bad")
        assert resp.status_code == 400
        assert ERR_400_INVALID_STRIPE_SIGNATURE in resp.content.decode()
        assert StripeWebhookEvent.objects.count() == 0

    def test_verification_crash_returns_400_payload_error(self, api_client):
        resp = _post(api_client, {"id": sid("evt", "x"), "type": "t"}, signature="crash")
        assert resp.status_code == 400
        assert ERR_400_INVALID_WEBHOOK_PAYLOAD in resp.content.decode()
        assert StripeWebhookEvent.objects.count() == 0

    def test_malformed_payload_missing_id_returns_400(self, api_client):
        resp = _post(api_client, {"type": "checkout.session.completed"})
        assert resp.status_code == 400
        assert ERR_400_INVALID_WEBHOOK_PAYLOAD in resp.content.decode()

    def test_malformed_payload_missing_type_returns_400(self, api_client):
        resp = _post(api_client, {"id": sid("evt", "1")})
        assert resp.status_code == 400
        assert ERR_400_INVALID_WEBHOOK_PAYLOAD in resp.content.decode()
        assert StripeWebhookEvent.objects.count() == 0


@pytest.mark.django_db
class TestWebhookProcessing:
    def test_unknown_event_type_is_acked_and_marked_processed(self, api_client):
        resp = _post(api_client, {"id": sid("evt", "odd"), "type": "some.unknown.event"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        log = StripeWebhookEvent.objects.get(stripe_event_id=sid("evt", "odd"))
        assert log.processed_at is not None
        assert log.event_type == "some.unknown.event"

    def test_checkout_completed_grants_package_credits(self, api_client, user):
        resp = _post(api_client, _checkout_event(user, package="starter"))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        wallet = Wallet.objects.get(user=user)
        assert wallet.balance == 500
        txn = Transaction.objects.get(wallet=wallet)
        assert txn.credits_delta == 500
        assert txn.metadata["package"] == "starter"
        log = StripeWebhookEvent.objects.get(stripe_event_id=sid("evt", "pkg_1"))
        assert log.processed_at is not None
        assert log.error == ""

    def test_duplicate_event_short_circuits(self, api_client, user):
        event = _checkout_event(user, package="starter")
        assert _post(api_client, event).status_code == 200
        resp = _post(api_client, event)
        assert resp.status_code == 200
        assert resp.json()["status"] == "duplicate"
        wallet = Wallet.objects.get(user=user)
        assert wallet.balance == 500  # not double-granted
        assert Transaction.objects.filter(wallet=wallet).count() == 1
        assert StripeWebhookEvent.objects.count() == 1

    def test_failed_handler_is_reprocessed_on_retry(
        self, api_client, user, monkeypatch
    ):
        from stapel_billing import services

        event = _checkout_event(user, package="starter", event_id=sid("evt", "retry"))

        original = services.handle_checkout_completed

        def _boom(evt):
            raise RuntimeError("transient handler failure")

        monkeypatch.setattr(services, "handle_checkout_completed", _boom)
        resp = _post(api_client, event)
        assert resp.status_code == 500
        assert resp.json()["status"] == "error"
        log = StripeWebhookEvent.objects.get(stripe_event_id=sid("evt", "retry"))
        assert log.processed_at is None  # NOT marked processed
        assert "transient handler failure" in log.error
        assert not Wallet.objects.filter(user=user).exists()

        # Stripe retries the same event — it must now be processed.
        monkeypatch.setattr(services, "handle_checkout_completed", original)
        resp = _post(api_client, event)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        log.refresh_from_db()
        assert log.processed_at is not None
        assert log.error == ""
        assert Wallet.objects.get(user=user).balance == 500

    def test_checkout_without_user_id_is_ignored_but_acked(self, api_client):
        event = {
            "id": sid("evt", "nouser"),
            "type": "checkout.session.completed",
            "data": {"object": {"id": sid("cs", "1"), "metadata": {"package": "starter"}}},
        }
        resp = _post(api_client, event)
        assert resp.status_code == 200
        assert Wallet.objects.count() == 0

    def test_checkout_with_unknown_user_is_ignored_but_acked(self, api_client):
        event = {
            "id": sid("evt", "ghost"),
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": sid("cs", "1"),
                    "metadata": {
                        "user_id": "00000000-0000-0000-0000-000000000000",
                        "package": "starter",
                    },
                }
            },
        }
        assert _post(api_client, event).status_code == 200
        assert Wallet.objects.count() == 0

    def test_plan_checkout_creates_subscription_and_bonus(self, api_client, user):
        resp = _post(api_client, _checkout_event(user, event_id=sid("evt", "plan"), plan="pro"))
        assert resp.status_code == 200
        sub = Subscription.objects.get(user=user)
        assert sub.plan == "pro"
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.stripe_subscription_id == sid("sub", "1")
        assert Wallet.objects.get(user=user).balance == 300  # pro monthly bonus

    def test_plan_bonus_row_carries_the_amount_charged(self, api_client, user):
        """The row is the ledger's only record that this bundle was paid for.

        Without an amount a subscription purchase is indistinguishable from
        a free signup grant of the same type, so a reader looking for sales
        either misses every subscription or counts every signup.
        """
        event = _checkout_event(user, event_id=sid("evt", "plan_amt"), plan="pro")
        event["data"]["object"]["amount_total"] = 700  # a coupon took some off
        assert _post(api_client, event).status_code == 200
        txn = Transaction.objects.get(wallet__user=user)
        assert txn.type == "subscription_bonus"
        assert txn.amount_cents == 700


@pytest.mark.django_db
class TestSubscriptionLifecycleEvents:
    def _make_sub(self, user, sub_id=sid("sub", "lc")):
        return Subscription.objects.create(
            user=user, plan="pro", status="active", stripe_subscription_id=sub_id
        )

    def test_invoice_paid_grants_renewal_once(self, api_client, user):
        self._make_sub(user)
        event = {
            "id": sid("evt", "inv"),
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": sid("in", "9"),
                    "subscription": sid("sub", "lc"),
                    "billing_reason": "subscription_cycle",
                    "amount_paid": 1500,
                    "currency": "usd",
                }
            },
        }
        assert _post(api_client, event).status_code == 200
        wallet = Wallet.objects.get(user=user)
        assert wallet.balance == 300
        txn = Transaction.objects.get(wallet=wallet)
        assert txn.metadata["stripe_invoice_id"] == sid("in", "9")
        assert txn.amount_cents == 1500

    def test_invoice_for_initial_checkout_does_not_double_grant(self, api_client, user):
        self._make_sub(user)
        event = {
            "id": sid("evt", "inv_init"),
            "type": "invoice.payment_succeeded",
            "data": {
                "object": {
                    "id": sid("in", "0"),
                    "subscription": sid("sub", "lc"),
                    "billing_reason": "subscription_create",
                }
            },
        }
        assert _post(api_client, event).status_code == 200
        assert not Wallet.objects.filter(user=user).exists()

    def test_invoice_without_subscription_is_noop(self, api_client, user):
        event = {
            "id": sid("evt", "inv_none"),
            "type": "invoice.paid",
            "data": {"object": {"id": sid("in", "1")}},
        }
        assert _post(api_client, event).status_code == 200
        assert Wallet.objects.count() == 0

    def test_subscription_updated_maps_status(self, api_client, user):
        sub = self._make_sub(user)
        event = {
            "id": sid("evt", "upd"),
            "type": "customer.subscription.updated",
            "data": {"object": {"id": sid("sub", "lc"), "status": "past_due"}},
        }
        assert _post(api_client, event).status_code == 200
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.PAST_DUE

    def test_subscription_updated_unknown_status_keeps_current(self, api_client, user):
        sub = self._make_sub(user)
        event = {
            "id": sid("evt", "upd2"),
            "type": "customer.subscription.updated",
            "data": {"object": {"id": sid("sub", "lc"), "status": "weird_new_status"}},
        }
        assert _post(api_client, event).status_code == 200
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE

    def test_subscription_updated_unknown_id_is_noop(self, api_client, user):
        event = {
            "id": sid("evt", "upd3"),
            "type": "customer.subscription.created",
            "data": {"object": {"id": sid("sub", "ghost"), "status": "active"}},
        }
        assert _post(api_client, event).status_code == 200
        assert Subscription.objects.count() == 0

    def test_subscription_deleted_marks_cancelled(self, api_client, user):
        sub = self._make_sub(user)
        event = {
            "id": sid("evt", "del"),
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": sid("sub", "lc")}},
        }
        assert _post(api_client, event).status_code == 200
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.CANCELLED
        assert sub.cancelled_at is not None

    def test_subscription_deleted_unknown_id_is_noop(self, api_client):
        event = {
            "id": sid("evt", "del2"),
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": sid("sub", "ghost")}},
        }
        assert _post(api_client, event).status_code == 200


# ─── Event ordering (audit minor #10) ───────────────────────
#
# Stripe does not promise event order. `customer.subscription.created` can
# land BEFORE the checkout that creates the local row, and the handler used
# to return silently when it found none — throwing away the only payload
# that carries the billing period. The checkout then granted an UNDATED
# subscription lot, the event that would have dated it was already marked
# processed, and the bundle a cancelled subscriber kept never expired.


def _subscription_event(event_id, type_, *, sub_id=sid("sub", "1"), period_end=None, **obj):
    body = {"id": sub_id, "customer": sid("cus", "1"), "status": "active", **obj}
    if period_end is not None:
        body["current_period_end"] = period_end
    return {"id": event_id, "type": type_, "data": {"object": body}}


@pytest.mark.django_db
class TestSubscriptionEventOrdering:
    #: A period end far enough out that it is a real future deadline.
    PERIOD_END = 4102444800  # 2100-01-01T00:00:00Z

    def test_a_period_that_arrives_before_the_checkout_is_not_lost(
        self, api_client, user
    ):
        from stapel_billing.models import CreditLot, PendingSubscriptionPeriod

        # 1. The subscription event lands FIRST, with no local row to update.
        early = _subscription_event(
            sid("evt", "early"), "customer.subscription.created", period_end=self.PERIOD_END
        )
        assert _post(api_client, early).status_code == 200
        assert Subscription.objects.count() == 0
        assert PendingSubscriptionPeriod.objects.count() == 1

        # 2. The checkout lands second and claims the parked period.
        assert _post(
            api_client, _checkout_event(user, event_id=sid("evt", "co"), plan="pro")
        ).status_code == 200

        sub = Subscription.objects.get()
        assert sub.current_period_end is not None
        assert sub.current_period_end.year == 2100
        # The bundle it granted is DATED — the whole point.
        lot = CreditLot.objects.get(wallet__user=user, source="subscription")
        assert lot.expires_at == sub.current_period_end
        # The stash is consumed, not left to be applied twice.
        assert PendingSubscriptionPeriod.objects.count() == 0

    def test_the_normal_order_still_works_and_stamps_the_lot(self, api_client, user):
        from stapel_billing.models import CreditLot, PendingSubscriptionPeriod

        assert _post(
            api_client, _checkout_event(user, event_id=sid("evt", "co"), plan="pro")
        ).status_code == 200
        # The checkout session carries no period, so the lot starts undated.
        lot = CreditLot.objects.get(wallet__user=user, source="subscription")
        assert lot.expires_at is None

        assert _post(
            api_client,
            _subscription_event(
                sid("evt", "after"),
                "customer.subscription.updated",
                period_end=self.PERIOD_END,
            ),
        ).status_code == 200

        lot.refresh_from_db()
        assert lot.expires_at is not None
        assert PendingSubscriptionPeriod.objects.count() == 0

    def test_a_row_created_by_checkout_without_a_subscription_id_is_adopted(
        self, api_client, user
    ):
        sub = Subscription.objects.create(
            user=user, plan="pro", status=SubscriptionStatus.ACTIVE,
            stripe_customer_id=sid("cus", "1"),
        )
        assert _post(
            api_client,
            _subscription_event(
                sid("evt", "adopt"),
                "customer.subscription.updated",
                period_end=self.PERIOD_END,
            ),
        ).status_code == 200

        sub.refresh_from_db()
        assert sub.stripe_subscription_id == sid("sub", "1")
        assert sub.current_period_end is not None

    def test_the_cancellation_stamps_the_period_onto_an_undated_bundle(
        self, api_client, user
    ):
        from stapel_billing.models import CreditLot

        assert _post(
            api_client, _checkout_event(user, event_id=sid("evt", "co"), plan="pro")
        ).status_code == 200
        lot = CreditLot.objects.get(wallet__user=user, source="subscription")
        assert lot.expires_at is None

        # No update event ever arrived — the cancellation is the last
        # payload that carries a period, and the last chance to date the lot.
        assert _post(
            api_client,
            _subscription_event(
                sid("evt", "del"),
                "customer.subscription.deleted",
                period_end=self.PERIOD_END,
                status="canceled",
            ),
        ).status_code == 200

        lot.refresh_from_db()
        assert lot.expires_at is not None
        sub = Subscription.objects.get()
        assert sub.status == SubscriptionStatus.CANCELLED

    def test_a_deletion_for_an_unknown_subscription_parks_its_period_too(
        self, api_client
    ):
        from stapel_billing.models import PendingSubscriptionPeriod

        assert _post(
            api_client,
            _subscription_event(
                sid("evt", "del_ghost"),
                "customer.subscription.deleted",
                sub_id=sid("sub", "ghost"),
                period_end=self.PERIOD_END,
            ),
        ).status_code == 200
        assert PendingSubscriptionPeriod.objects.get().stripe_subscription_id == (
            sid("sub", "ghost")
        )


# ─── The routing seam ───────────────────────────────────────
#
# Routing used to be an if/elif chain inside StripeWebhookView.post: a host
# that wanted one more event type, or a different reaction to one it already
# had, could only fork the view. These pin the registry that replaced it.

_seen_events = []


def _record_dispute(event):
    _seen_events.append(event["id"])


def _replacement_checkout(event):
    _seen_events.append("replaced:" + event["id"])


@pytest.fixture(autouse=True)
def _clear_seen():
    _seen_events.clear()
    yield
    _seen_events.clear()


@pytest.fixture
def _handlers(settings):
    """Set STRIPE_WEBHOOK_HANDLERS while keeping the JSON test provider."""
    def _apply(mapping):
        settings.STAPEL_BILLING = {
            "PAYMENT_PROVIDER": PROVIDER_PATH,
            "STRIPE_WEBHOOK_HANDLERS": mapping,
        }
        billing_settings.reload()
    yield _apply
    billing_settings.reload()


_HERE = __name__


class TestStripeHandlerRegistry:
    def test_builtins_are_the_types_the_chain_used_to_carry(self):
        from stapel_billing.webhooks import registered_stripe_events

        assert registered_stripe_events() == [
            "charge.dispute.created",
            # 0.14.0 — money that did NOT arrive. Neither grants nor claws
            # back; both exist so a declined card produces a fact, and the
            # payer a letter, instead of a lapsed plan and no explanation.
            "charge.failed",
            "charge.refunded",
            "checkout.session.completed",
            "credit_note.created",
            "customer.subscription.created",
            "customer.subscription.deleted",
            "customer.subscription.updated",
            "invoice.paid",
            "invoice.payment_failed",
            "invoice.payment_succeeded",
        ]

    def test_builtin_paths_all_resolve(self):
        from stapel_billing.webhooks import stripe_handlers

        assert all(callable(h) for h in stripe_handlers().values())

    def test_settings_merge_over_builtins_they_do_not_replace_them(self, _handlers):
        from stapel_billing.webhooks import stripe_handlers

        _handlers({"charge.dispute.created": f"{_HERE}._record_dispute"})
        handlers = stripe_handlers()

        assert handlers["charge.dispute.created"] is _record_dispute
        # The built-ins survive an overlay that never mentioned them.
        assert callable(handlers["checkout.session.completed"])

    def test_a_callable_is_accepted_as_well_as_a_dotted_path(self, _handlers):
        from stapel_billing.webhooks import get_stripe_handler

        _handlers({"charge.dispute.created": _record_dispute})
        assert get_stripe_handler("charge.dispute.created") is _record_dispute

    def test_none_switches_a_builtin_off(self, _handlers):
        from stapel_billing.webhooks import (
            get_stripe_handler,
            registered_stripe_events,
        )

        _handlers({"customer.subscription.deleted": None})
        assert get_stripe_handler("customer.subscription.deleted") is None
        assert "customer.subscription.deleted" not in registered_stripe_events()

    def test_unresolvable_path_refuses_rather_than_dropping_the_payment(
        self, _handlers
    ):
        from django.core.exceptions import ImproperlyConfigured

        from stapel_billing.webhooks import get_stripe_handler

        _handlers({"charge.dispute.created": "myproject.nope.missing"})
        with pytest.raises(ImproperlyConfigured):
            get_stripe_handler("charge.dispute.created")

    def test_bad_registry_entry_is_a_boot_error(self, _handlers):
        from stapel_billing.checks import check_stripe_webhook_handlers

        _handlers({"charge.dispute.created": "myproject.nope.missing"})
        ids = [m.id for m in check_stripe_webhook_handlers(None)]
        assert ids == ["stapel_billing.E106"]

    def test_a_good_registry_passes_the_boot_check(self, _handlers):
        from stapel_billing.checks import check_stripe_webhook_handlers

        _handlers({"charge.dispute.created": f"{_HERE}._record_dispute"})
        assert check_stripe_webhook_handlers(None) == []


@pytest.mark.django_db
class TestRegistryDrivesTheView:
    """The seam is only real if the VIEW routes through it."""

    def test_a_host_registered_type_is_handled_not_logged_as_unknown(
        self, api_client, _handlers
    ):
        _handlers({"charge.dispute.created": f"{_HERE}._record_dispute"})
        event = {
            "id": sid("evt", "dispute"),
            "type": "charge.dispute.created",
            "data": {"object": {"id": sid("dp", "1")}},
        }

        assert _post(api_client, event).status_code == 200
        assert _seen_events == [sid("evt", "dispute")]
        assert StripeWebhookEvent.objects.get(
            stripe_event_id=sid("evt", "dispute")
        ).processed_at is not None

    def test_an_override_replaces_the_builtin_reaction(
        self, api_client, user, _handlers
    ):
        _handlers({
            "checkout.session.completed": f"{_HERE}._replacement_checkout",
        })
        event = _checkout_event(user, package="starter", event_id=sid("evt", "override"))

        assert _post(api_client, event).status_code == 200
        assert _seen_events == ["replaced:" + sid("evt", "override")]
        # The built-in never ran, so no credits were granted.
        assert not Wallet.objects.filter(user=user).exists()

    def test_an_override_cannot_opt_out_of_the_idempotency_claim(
        self, api_client, _handlers
    ):
        """The guarantees around the handler are the view's, not the host's."""
        _handlers({"charge.dispute.created": f"{_HERE}._record_dispute"})
        event = {
            "id": sid("evt", "dispute_dup"),
            "type": "charge.dispute.created",
            "data": {"object": {"id": sid("dp", "2")}},
        }

        assert _post(api_client, event).status_code == 200
        assert _post(api_client, event).json()["status"] == "duplicate"
        assert _seen_events == [sid("evt", "dispute_dup")]  # ran exactly once

    def test_a_disabled_builtin_is_acked_and_does_nothing(
        self, api_client, user, _handlers
    ):
        _handlers({"customer.subscription.deleted": None})
        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_subscription_id=sid("sub", "off"),
            status=SubscriptionStatus.ACTIVE,
        )
        event = {
            "id": sid("evt", "off"),
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": sid("sub", "off")}},
        }

        assert _post(api_client, event).status_code == 200
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE


# ─── Real-length ids, and the column that could not hold one ───
#
# Every fixture above used to carry a six-character fake ("sub_1"). A real
# Stripe id is a prefix plus ~24 opaque characters, and a real status word
# runs to 18 ('incomplete_expired'). A column too narrow for either passes a
# suite built on fakes and fails on the first live webhook: on Postgres the
# INSERT raises StringDataRightTruncation, the view answers 500, Stripe
# retries for ~3 days, and the subscription behind it never activates. That
# is what `PendingSubscriptionPeriod.status` (varchar(16) until 0.19.1) did
# to a paying customer.


def _lifecycle_subscription_event(
    event_id, type_, *, sub_id, customer_id, status, period_end=None
):
    obj = {"id": sub_id, "customer": customer_id, "status": status}
    if period_end is not None:
        obj["current_period_end"] = period_end
    return {"id": event_id, "type": type_, "data": {"object": obj}}


@pytest.mark.django_db
class TestTheLifecycleWithRealLengthIds:
    """checkout → subscription.created/updated → invoice.paid, full length."""

    PERIOD_END = 4102444800  # 2100-01-01T00:00:00Z

    SUB = sid("sub", "lifecycle")
    CUS = sid("cus", "lifecycle")
    INV = sid("in", "lifecycle")

    def _deliver(self, api_client, user):
        """The four events Stripe sends when somebody subscribes."""
        for prefix, value in (("sub", self.SUB), ("cus", self.CUS), ("in", self.INV)):
            assert_realistic(value, prefix)

        checkout = _checkout_event(
            user, event_id=sid("evt", "lifecycle_checkout"), plan="pro"
        )
        checkout["data"]["object"]["subscription"] = self.SUB
        checkout["data"]["object"]["customer"] = self.CUS

        created = _lifecycle_subscription_event(
            sid("evt", "lifecycle_created"),
            "customer.subscription.created",
            sub_id=self.SUB,
            customer_id=self.CUS,
            status="incomplete",
        )
        updated = _lifecycle_subscription_event(
            sid("evt", "lifecycle_updated"),
            "customer.subscription.updated",
            sub_id=self.SUB,
            customer_id=self.CUS,
            status="active",
            period_end=self.PERIOD_END,
        )
        invoice = {
            "id": sid("evt", "lifecycle_invoice"),
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": self.INV,
                    "subscription": self.SUB,
                    "billing_reason": "subscription_cycle",
                    "amount_paid": 1500,
                    "currency": "usd",
                }
            },
        }
        return [checkout, created, updated, invoice]

    def test_the_subscription_ends_active_with_its_credits(self, api_client, user):
        for event in self._deliver(api_client, user):
            resp = _post(api_client, event)
            assert resp.status_code == 200, (event["type"], resp.content)
            assert resp.json()["status"] == "ok"

        sub = Subscription.objects.get(user=user)
        assert sub.stripe_subscription_id == self.SUB
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.current_period_end is not None
        # The plan bonus from the checkout plus the renewal from the invoice.
        wallet = Wallet.objects.get(user=user)
        assert wallet.balance == 600
        assert Transaction.objects.filter(wallet=wallet).count() == 2

    def test_every_event_redelivered_grants_nothing_twice(self, api_client, user):
        """Stripe's 3-day retry after a 500 must not double-grant once fixed."""
        events = self._deliver(api_client, user)
        for event in events:
            assert _post(api_client, event).status_code == 200
        wallet = Wallet.objects.get(user=user)
        assert wallet.balance == 600

        for event in events:
            resp = _post(api_client, event)
            assert resp.status_code == 200
            assert resp.json()["status"] == "duplicate"

        wallet.refresh_from_db()
        assert wallet.balance == 600
        assert Transaction.objects.filter(wallet=wallet).count() == 2
        assert Subscription.objects.get(user=user).status == SubscriptionStatus.ACTIVE


@pytest.mark.django_db
class TestProviderStatusWidth:
    """The defect itself: a parked period carrying the longest status word."""

    def test_the_stash_holds_the_longest_status_the_provider_sends(
        self, api_client, user
    ):
        from stapel_billing.models import PendingSubscriptionPeriod

        sub_id = sid("sub", "truncation")
        event = _lifecycle_subscription_event(
            sid("evt", "truncation"),
            "customer.subscription.created",
            sub_id=sub_id,
            customer_id=sid("cus", "truncation"),
            status=LONGEST_PROVIDER_STATUS,
            period_end=4102444800,
        )
        # No local row yet — this is the path that parks the period, and the
        # INSERT that used to die.
        resp = _post(api_client, event)
        assert resp.status_code == 200, resp.content

        pending = PendingSubscriptionPeriod.objects.get()
        pending.refresh_from_db()
        assert pending.stripe_subscription_id == sub_id
        assert pending.status == LONGEST_PROVIDER_STATUS

    def test_the_column_is_wide_enough_for_it(self):
        """The assertion that is red on SQLite too.

        SQLite ignores varchar length, so the round-trip above passes even
        on the broken column — which is precisely how this shipped. The
        width is therefore asserted directly, and
        tests/test_truncation_postgres.py proves it against a real server.
        """
        from stapel_billing.models import PendingSubscriptionPeriod

        field = PendingSubscriptionPeriod._meta.get_field("status")
        assert field.max_length >= len(LONGEST_PROVIDER_STATUS), (
            "PendingSubscriptionPeriod.status cannot hold "
            f"{LONGEST_PROVIDER_STATUS!r} ({len(LONGEST_PROVIDER_STATUS)} "
            f"chars) — max_length={field.max_length}"
        )
