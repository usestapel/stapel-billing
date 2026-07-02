"""StripeWebhookView end-to-end matrix.

Uses a JSON test provider (registered via the PAYMENT_PROVIDER dotted
path) so the full view → services → handlers pipeline runs without the
Stripe SDK: the "signature" header selects the verification outcome.
"""

import json

import pytest

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


def _checkout_event(user, event_id="evt_pkg_1", **metadata):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "customer": "cus_1",
                "subscription": "sub_1",
                "metadata": {"user_id": str(user.id), **metadata},
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
