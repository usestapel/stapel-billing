"""A declined renewal on the current provider API, from webhook to letter.

The provider account behind this suite's first consumer runs API version
2026-06-24.dahlia: an invoice names its subscription only under
``parent.subscription_details``, and a charge no longer names its invoice.
Each test here posts the provider's shape to the real webhook view and reads
what was asked of the notifications module, so a handler that cannot read
the shape fails as "nobody was told", not as a unit returning None.

The rule under test: one notice per invoice however often the provider
retries, plus at most one more when the provider stops retrying.
"""
import json
import logging

import pytest

from stapel_billing import services
from stapel_billing.conf import billing_settings
from stapel_billing.models import (
    LotSource,
    ProviderGrant,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
)

from .stripe_ids import sid
from .test_webhooks import PROVIDER_PATH, WEBHOOK_URL

BILLING_PAGE = "https://app.example.com/app/settings/billing"
SUB_ID = sid("sub", "renews")
INVOICE_ID = sid("in", "declined")


@pytest.fixture(autouse=True)
def _host(settings):
    settings.STAPEL_BILLING = {
        **getattr(settings, "STAPEL_BILLING", {}),
        "PAYMENT_PROVIDER": PROVIDER_PATH,
        "BILLING_PAGE_URL": BILLING_PAGE,
    }
    billing_settings.reload()
    yield
    billing_settings.reload()


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, notification_type, **kwargs):
        self.calls.append((notification_type, kwargs))
        return True


@pytest.fixture
def notifier(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr("stapel_core.notifications.request_notification", recorder)
    return recorder


@pytest.fixture
def subscriber(user):
    Subscription.objects.create(
        user=user,
        plan="pro",
        status=SubscriptionStatus.ACTIVE,
        stripe_subscription_id=SUB_ID,
        stripe_customer_id=sid("cus", "renews"),
    )
    return user


@pytest.fixture
def deliver(monkeypatch):
    """Run every ``payment.failed`` subscriber the registry holds, in-process.

    ``emit`` hands the fact to the outbox; this stands in for the relay and
    the consumer, so the subject is still the real subscriber list.
    """
    from stapel_core.comm import action_registry

    emitted = []
    action_registry.subscribe("payment.failed", lambda event: emitted.append(event))

    class _Event:
        def __init__(self, payload, event_id):
            self.payload = payload
            self.event_id = event_id

    def run():
        sent = list(emitted)
        emitted.clear()
        for index, event in enumerate(sent):
            for handler in action_registry.handlers("payment.failed"):
                if handler.__module__.startswith("stapel_billing"):
                    handler(_Event(event.payload, f"bus-{index}"))
        return [event.payload for event in sent]

    return run


def _invoice(*, attempt, next_attempt, **extra):
    """``invoice.payment_failed`` as API 2026-06-24.dahlia shapes it (keys only)."""
    obj = {
        "id": INVOICE_ID,
        "object": "invoice",
        "billing_reason": "subscription_cycle",
        "collection_method": "charge_automatically",
        "status": "open",
        "attempt_count": attempt,
        "attempted": True,
        "next_payment_attempt": next_attempt,
        "amount_due": 900,
        "amount_paid": 0,
        "amount_remaining": 900,
        "currency": "usd",
        "customer": sid("cus", "renews"),
        "hosted_invoice_url": "https://invoice.stripe.com/i/acct_x/test_y",
        "parent": {
            "type": "subscription_details",
            "quote_details": None,
            "subscription_details": {"metadata": {}, "subscription": SUB_ID},
        },
        "lines": {"data": [{
            "id": sid("il", "declined"),
            "parent": {
                "type": "subscription_item_details",
                "invoice_item_details": None,
                "subscription_item_details": {
                    "proration": False,
                    "invoice_item": None,
                    "subscription": SUB_ID,
                    "subscription_item": sid("si", "declined"),
                },
            },
            "period": {"start": 1792152181, "end": 4102444800},
        }]},
    }
    obj.update(extra)
    return obj


def _post(client, event_id, event_type, obj):
    return client.post(
        WEBHOOK_URL,
        data=json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}}),
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="good",
    )


def _letters(notifier):
    return [kwargs for name, kwargs in notifier.calls if name == "billing.payment_failed"]


@pytest.mark.django_db
class TestADeclinedRenewalReachesTheCustomer:
    def test_the_first_failure_sends_one_notice_with_the_billing_page(
        self, api_client, subscriber, notifier, deliver
    ):
        resp = _post(api_client, sid("evt", "f1"), "invoice.payment_failed",
                     _invoice(attempt=1, next_attempt=4102444800))
        assert resp.status_code == 200
        facts = deliver()

        assert len(facts) == 1
        assert facts[0]["invoice_id"] == INVOICE_ID
        assert "final_attempt" not in facts[0]
        letters = _letters(notifier)
        assert len(letters) == 1
        assert letters[0]["user_id"] == str(subscriber.id)
        variables = letters[0]["variables"]
        assert variables["retry_url"] == BILLING_PAGE
        assert variables["amount"] == "$9.00"
        assert "final_attempt" not in variables

    def test_the_providers_retries_do_not_send_more(
        self, api_client, subscriber, notifier, deliver
    ):
        for attempt in (1, 2, 3):
            _post(api_client, sid("evt", f"r{attempt}"), "invoice.payment_failed",
                  _invoice(attempt=attempt, next_attempt=4102444800))
            deliver()

        assert len(_letters(notifier)) == 1

    def test_the_last_attempt_sends_one_more_notice_once(
        self, api_client, subscriber, notifier, deliver
    ):
        _post(api_client, sid("evt", "l1"), "invoice.payment_failed",
              _invoice(attempt=1, next_attempt=4102444800))
        deliver()
        _post(api_client, sid("evt", "l4"), "invoice.payment_failed",
              _invoice(attempt=4, next_attempt=None))
        facts = deliver()
        # The same final event, delivered twice more by the bus.
        from stapel_billing.actions import handle_payment_failed_notification

        class _Again:
            payload = facts[0]
            event_id = "bus-again"

        handle_payment_failed_notification(_Again())
        handle_payment_failed_notification(_Again())

        assert facts[0]["final_attempt"] is True
        letters = _letters(notifier)
        assert len(letters) == 2
        assert "final_attempt" not in letters[0]["variables"]
        assert letters[1]["variables"]["final_attempt"] is True
        assert ProviderGrant.objects.filter(
            provider=ProviderGrant.PROVIDER_NOTIFY,
            external_id=f"{INVOICE_ID}:final",
        ).count() == 1

    def test_a_first_failure_that_is_already_final_sends_one_notice(
        self, api_client, subscriber, notifier, deliver
    ):
        _post(api_client, sid("evt", "o1"), "invoice.payment_failed",
              _invoice(attempt=1, next_attempt=None))
        deliver()

        letters = _letters(notifier)
        assert len(letters) == 1
        assert letters[0]["variables"]["final_attempt"] is True

    @pytest.mark.parametrize("extra", [
        {"collection_method": "send_invoice"},
        {"billing_reason": "subscription_create"},
    ])
    def test_no_final_notice_where_the_provider_never_retries(
        self, api_client, subscriber, notifier, deliver, extra
    ):
        _post(api_client, sid("evt", "n1"), "invoice.payment_failed",
              _invoice(attempt=1, next_attempt=None, **extra))
        facts = deliver()

        assert "final_attempt" not in facts[0]

    def test_a_payload_without_the_field_is_not_called_final(self, subscriber):
        obj = _invoice(attempt=1, next_attempt=None)
        del obj["next_payment_attempt"]

        assert services._is_final_attempt(obj) is False

    def test_a_charge_that_names_its_invoice_sends_nothing_of_its_own(
        self, api_client, subscriber, notifier, deliver
    ):
        _post(api_client, sid("evt", "c1"), "charge.failed", {
            "id": sid("ch", "declined"),
            "invoice": INVOICE_ID,
            "customer": sid("cus", "renews"),
            "payment_intent": sid("pi", "declined"),
            "amount": 900,
            "currency": "usd",
        })

        assert deliver() == []
        assert _letters(notifier) == []


@pytest.mark.django_db
class TestARefundOnTheCurrentApi:
    """A dahlia charge carries its payment intent but no invoice."""

    def _refund(self, client, event_id, **obj):
        return _post(client, event_id, "charge.refunded", {
            "id": sid("ch", event_id),
            "object": "charge",
            "amount": 900,
            "amount_refunded": 900,
            "refunded": True,
            "currency": "usd",
            **obj,
        })

    def test_a_renewal_refund_keeps_the_credits_and_says_so(
        self, api_client, subscriber, caplog
    ):
        services.credit(
            user=subscriber, credits=300, type=TransactionType.SUBSCRIPTION_BONUS,
            source=LotSource.SUBSCRIPTION, amount_cents=900,
            metadata={"stripe_invoice_id": INVOICE_ID, "stripe_subscription_id": SUB_ID},
        )

        with caplog.at_level(logging.WARNING, logger="stapel_billing.services"):
            resp = self._refund(api_client, "rr1", payment_intent=sid("pi", "renewal"))

        assert resp.status_code == 200
        wallet = services.get_or_create_wallet(subscriber)
        wallet.refresh_from_db()
        assert wallet.balance == 300
        assert not Transaction.objects.filter(type=TransactionType.REFUND).exists()
        assert any(
            "credits stay with the customer" in record.getMessage()
            and record.levelno == logging.WARNING
            for record in caplog.records
        )

    def test_a_package_refund_is_still_matched_by_its_payment_intent(
        self, api_client, subscriber
    ):
        services.credit(
            user=subscriber, credits=100, type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE, amount_cents=900,
            metadata={"stripe_payment_intent": sid("pi", "package")},
        )

        resp = self._refund(api_client, "rp1", payment_intent=sid("pi", "package"))

        assert resp.status_code == 200
        wallet = services.get_or_create_wallet(subscriber)
        wallet.refresh_from_db()
        assert wallet.balance == 0
        assert Transaction.objects.filter(type=TransactionType.REFUND).count() == 1
