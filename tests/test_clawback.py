"""Refund, dispute and credit-note clawback (0.11.0).

Until 0.11.0 nothing here handled money going the other way: the card was
refunded, the dispute was lost, the invoice was credited — and the credits
those payments bought stayed spendable. Free credits by webhook silence.

The clawback only ever touches the lots the refunded grant created. Taking
the shortfall out of whatever else the wallet holds would pay a refunded
subscription back with the credits the customer bought for cash.
"""

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_billing import services
from stapel_billing.models import (
    CreditDebt,
    CreditLot,
    DebtReason,
    LotSource,
    ProviderGrant,
    Transaction,
    TransactionType,
)

from .test_webhooks import PROVIDER_PATH, WEBHOOK_URL  # noqa: F401


@pytest.fixture(autouse=True)
def _json_provider(settings):
    from stapel_billing.conf import billing_settings

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


def _granted(user, credits=100, *, expires_at=None, **metadata):
    """A grant shaped like the ones the checkout/invoice handlers write."""
    return services.credit(
        user=user,
        credits=credits,
        type=TransactionType.CREDIT_PURCHASE,
        source=LotSource.PURCHASE,
        expires_at=expires_at,
        amount_cents=1000,
        description="Purchase: Test",
        metadata={"stripe_payment_intent": "pi_1", **metadata},
    )


@pytest.mark.django_db
class TestClawBackGrant:
    def test_unspent_credits_come_straight_back_out_of_the_grants_lots(self, user):
        txn = _granted(user, 100)
        result = services.claw_back_grant(txn=txn)

        assert (result.reclaimed, result.outstanding, result.forgiven) == (100, 0, 0)
        assert result.debt is None
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 0
        assert result.transaction.type == TransactionType.REFUND
        assert result.transaction.credits_delta == -100

    def test_what_was_already_spent_becomes_a_debt(self, user):
        txn = _granted(user, 100)
        services.debit(user=user, credits=70, type=TransactionType.AI_CHARGE)

        result = services.claw_back_grant(txn=txn)

        assert (result.reclaimed, result.outstanding) == (30, 70)
        debt = CreditDebt.objects.get()
        assert debt.reason == DebtReason.CLAWBACK
        assert debt.credits_outstanding == 70
        assert debt.transaction_id == result.transaction.id

    def test_other_lots_are_not_raided_to_cover_the_refund(self, user):
        refunded = _granted(user, 50)
        services.credit(
            user=user,
            credits=200,
            type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
            description="A different purchase, not refunded",
        )
        services.debit(user=user, credits=50, type=TransactionType.AI_CHARGE)

        result = services.claw_back_grant(txn=refunded)

        # The 50 spent came out of the refunded grant (same expiry, older),
        # so the whole grant is owed — and the untouched 200 stays put.
        assert result.outstanding == 50
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 200

    def test_credits_that_expired_unspent_are_not_charged_for(self, user):
        txn = _granted(user, 100, expires_at=timezone.now() + timedelta(seconds=60))
        CreditLot.objects.filter(granting_transaction=txn).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        result = services.claw_back_grant(txn=txn)

        # The deployment already took these credits back once, at expiry.
        assert (result.reclaimed, result.forgiven, result.outstanding) == (0, 100, 0)
        assert CreditDebt.objects.count() == 0

    def test_the_debt_is_collected_by_the_next_top_up(self, user):
        txn = _granted(user, 100)
        services.debit(user=user, credits=100, type=TransactionType.AI_CHARGE)
        services.claw_back_grant(txn=txn)

        services.credit(
            user=user,
            credits=120,
            type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
        )
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 20
        assert CreditDebt.objects.get().settled_at is not None


@pytest.mark.django_db
class TestClawbackWebhooks:
    def _refund_event(self, event_id="evt_ref_1", **obj):
        return {
            "id": event_id,
            "type": "charge.refunded",
            "data": {
                "object": {
                    "id": "ch_1",
                    "payment_intent": "pi_1",
                    "amount": 1000,
                    "amount_refunded": 1000,
                    **obj,
                }
            },
        }

    def test_charge_refunded_takes_the_credits_back(self, api_client, user):
        _granted(user, 100)

        assert _post(api_client, self._refund_event()).status_code == 200

        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 0
        assert Transaction.objects.filter(type=TransactionType.REFUND).count() == 1

    def test_a_partial_refund_takes_a_proportional_part(self, api_client, user):
        _granted(user, 100)

        resp = _post(api_client, self._refund_event(amount_refunded=250))
        assert resp.status_code == 200

        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 75

    def test_the_same_event_redelivered_claws_back_once(self, api_client, user):
        _granted(user, 100)
        services.credit(
            user=user,
            credits=100,
            type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
            metadata={"stripe_payment_intent": "pi_other"},
        )

        _post(api_client, self._refund_event())
        # A second delivery of the SAME event id — Stripe retries.
        from stapel_billing.models import StripeWebhookEvent

        StripeWebhookEvent.objects.all().delete()
        _post(api_client, self._refund_event())

        assert Transaction.objects.filter(type=TransactionType.REFUND).count() == 1
        assert ProviderGrant.objects.filter(
            scope=ProviderGrant.SCOPE_CLAWBACK
        ).count() == 1

    def test_a_dispute_claws_back_through_the_charge_it_names(self, api_client, user):
        _granted(user, 60, stripe_charge_id="ch_disputed")
        event = {
            "id": "evt_dispute_1",
            "type": "charge.dispute.created",
            "data": {"object": {"id": "dp_1", "charge": "ch_disputed"}},
        }

        assert _post(api_client, event).status_code == 200
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 0

    def test_a_credit_note_claws_back_through_the_invoice(self, api_client, user):
        _granted(user, 40, stripe_invoice_id="in_9")
        event = {
            "id": "evt_cn_1",
            "type": "credit_note.created",
            "data": {"object": {"id": "cn_1", "invoice": "in_9"}},
        }

        assert _post(api_client, event).status_code == 200
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 0

    def test_an_event_naming_no_grant_of_ours_changes_nothing(self, api_client, user):
        _granted(user, 100)
        event = self._refund_event(event_id="evt_unknown", payment_intent="pi_nope")

        assert _post(api_client, event).status_code == 200
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 100
        # No claim was burned either: the event named nothing to claw back.
        assert ProviderGrant.objects.filter(
            scope=ProviderGrant.SCOPE_CLAWBACK
        ).count() == 0
