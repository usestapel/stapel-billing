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

WEBHOOK_URL = "/billing/api/webhooks/stripe"


class JsonWebhookProvider(PaymentProvider):
    """Verifies by header value; decodes the JSON body as the event."""

    name = "json-test"

    def create_checkout_session(self, *, user, package, plan, success_url, cancel_url):
        return ("https://json.test/checkout", "cs_json_1")

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


def _checkout_event(user, event_id="evt_pkg_1", session_id="cs_1", **metadata):
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
                "customer": "cus_1",
                "subscription": "sub_1",
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
        resp = _post(api_client, {"id": "evt_x", "type": "t"}, signature="bad")
        assert resp.status_code == 400
        assert ERR_400_INVALID_STRIPE_SIGNATURE in resp.content.decode()
        assert StripeWebhookEvent.objects.count() == 0

    def test_verification_crash_returns_400_payload_error(self, api_client):
        resp = _post(api_client, {"id": "evt_x", "type": "t"}, signature="crash")
        assert resp.status_code == 400
        assert ERR_400_INVALID_WEBHOOK_PAYLOAD in resp.content.decode()
        assert StripeWebhookEvent.objects.count() == 0

    def test_malformed_payload_missing_id_returns_400(self, api_client):
        resp = _post(api_client, {"type": "checkout.session.completed"})
        assert resp.status_code == 400
        assert ERR_400_INVALID_WEBHOOK_PAYLOAD in resp.content.decode()

    def test_malformed_payload_missing_type_returns_400(self, api_client):
        resp = _post(api_client, {"id": "evt_1"})
        assert resp.status_code == 400
        assert ERR_400_INVALID_WEBHOOK_PAYLOAD in resp.content.decode()
        assert StripeWebhookEvent.objects.count() == 0


@pytest.mark.django_db
class TestWebhookProcessing:
    def test_unknown_event_type_is_acked_and_marked_processed(self, api_client):
        resp = _post(api_client, {"id": "evt_odd", "type": "some.unknown.event"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        log = StripeWebhookEvent.objects.get(stripe_event_id="evt_odd")
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
        log = StripeWebhookEvent.objects.get(stripe_event_id="evt_pkg_1")
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

        event = _checkout_event(user, package="starter", event_id="evt_retry")

        original = services.handle_checkout_completed

        def _boom(evt):
            raise RuntimeError("transient handler failure")

        monkeypatch.setattr(services, "handle_checkout_completed", _boom)
        resp = _post(api_client, event)
        assert resp.status_code == 500
        assert resp.json()["status"] == "error"
        log = StripeWebhookEvent.objects.get(stripe_event_id="evt_retry")
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
            "id": "evt_nouser",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_1", "metadata": {"package": "starter"}}},
        }
        resp = _post(api_client, event)
        assert resp.status_code == 200
        assert Wallet.objects.count() == 0

    def test_checkout_with_unknown_user_is_ignored_but_acked(self, api_client):
        event = {
            "id": "evt_ghost",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_1",
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
        resp = _post(api_client, _checkout_event(user, event_id="evt_plan", plan="pro"))
        assert resp.status_code == 200
        sub = Subscription.objects.get(user=user)
        assert sub.plan == "pro"
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.stripe_subscription_id == "sub_1"
        assert Wallet.objects.get(user=user).balance == 300  # pro monthly bonus


@pytest.mark.django_db
class TestSubscriptionLifecycleEvents:
    def _make_sub(self, user, sub_id="sub_lc"):
        return Subscription.objects.create(
            user=user, plan="pro", status="active", stripe_subscription_id=sub_id
        )

    def test_invoice_paid_grants_renewal_once(self, api_client, user):
        self._make_sub(user)
        event = {
            "id": "evt_inv",
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": "in_9",
                    "subscription": "sub_lc",
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
        assert txn.metadata["stripe_invoice_id"] == "in_9"

    def test_invoice_for_initial_checkout_does_not_double_grant(self, api_client, user):
        self._make_sub(user)
        event = {
            "id": "evt_inv_init",
            "type": "invoice.payment_succeeded",
            "data": {
                "object": {
                    "id": "in_0",
                    "subscription": "sub_lc",
                    "billing_reason": "subscription_create",
                }
            },
        }
        assert _post(api_client, event).status_code == 200
        assert not Wallet.objects.filter(user=user).exists()

    def test_invoice_without_subscription_is_noop(self, api_client, user):
        event = {
            "id": "evt_inv_none",
            "type": "invoice.paid",
            "data": {"object": {"id": "in_1"}},
        }
        assert _post(api_client, event).status_code == 200
        assert Wallet.objects.count() == 0

    def test_subscription_updated_maps_status(self, api_client, user):
        sub = self._make_sub(user)
        event = {
            "id": "evt_upd",
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_lc", "status": "past_due"}},
        }
        assert _post(api_client, event).status_code == 200
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.PAST_DUE

    def test_subscription_updated_unknown_status_keeps_current(self, api_client, user):
        sub = self._make_sub(user)
        event = {
            "id": "evt_upd2",
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_lc", "status": "weird_new_status"}},
        }
        assert _post(api_client, event).status_code == 200
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE

    def test_subscription_updated_unknown_id_is_noop(self, api_client, user):
        event = {
            "id": "evt_upd3",
            "type": "customer.subscription.created",
            "data": {"object": {"id": "sub_ghost", "status": "active"}},
        }
        assert _post(api_client, event).status_code == 200
        assert Subscription.objects.count() == 0

    def test_subscription_deleted_marks_cancelled(self, api_client, user):
        sub = self._make_sub(user)
        event = {
            "id": "evt_del",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_lc"}},
        }
        assert _post(api_client, event).status_code == 200
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.CANCELLED
        assert sub.cancelled_at is not None

    def test_subscription_deleted_unknown_id_is_noop(self, api_client):
        event = {
            "id": "evt_del2",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_ghost"}},
        }
        assert _post(api_client, event).status_code == 200


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
            "checkout.session.completed",
            "customer.subscription.created",
            "customer.subscription.deleted",
            "customer.subscription.updated",
            "invoice.paid",
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
            "id": "evt_dispute",
            "type": "charge.dispute.created",
            "data": {"object": {"id": "dp_1"}},
        }

        assert _post(api_client, event).status_code == 200
        assert _seen_events == ["evt_dispute"]
        assert StripeWebhookEvent.objects.get(
            stripe_event_id="evt_dispute"
        ).processed_at is not None

    def test_an_override_replaces_the_builtin_reaction(
        self, api_client, user, _handlers
    ):
        _handlers({
            "checkout.session.completed": f"{_HERE}._replacement_checkout",
        })
        event = _checkout_event(user, package="starter", event_id="evt_override")

        assert _post(api_client, event).status_code == 200
        assert _seen_events == ["replaced:evt_override"]
        # The built-in never ran, so no credits were granted.
        assert not Wallet.objects.filter(user=user).exists()

    def test_an_override_cannot_opt_out_of_the_idempotency_claim(
        self, api_client, _handlers
    ):
        """The guarantees around the handler are the view's, not the host's."""
        _handlers({"charge.dispute.created": f"{_HERE}._record_dispute"})
        event = {
            "id": "evt_dispute_dup",
            "type": "charge.dispute.created",
            "data": {"object": {"id": "dp_2"}},
        }

        assert _post(api_client, event).status_code == 200
        assert _post(api_client, event).json()["status"] == "duplicate"
        assert _seen_events == ["evt_dispute_dup"]  # ran exactly once

    def test_a_disabled_builtin_is_acked_and_does_nothing(
        self, api_client, user, _handlers
    ):
        _handlers({"customer.subscription.deleted": None})
        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_subscription_id="sub_off",
            status=SubscriptionStatus.ACTIVE,
        )
        event = {
            "id": "evt_off",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_off"}},
        }

        assert _post(api_client, event).status_code == 200
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.ACTIVE
