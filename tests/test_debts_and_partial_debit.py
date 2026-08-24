"""Partial charges, tracked debt, and the read-only affordability check (0.11.0).

The claim under test is that a wallet which cannot cover a charge has three
possible outcomes and the caller picks one: refuse (the unchanged default),
serve and remember (a partial debit plus a debt), or ask first without
touching anything (``can_afford``). Before 0.11.0 only the first existed, so
every consumer that had already done the work had to hand it out free — and
nothing anywhere recorded that it had.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_billing import services
from stapel_billing.models import (
    CreditDebt,
    DebtReason,
    LotSource,
    Transaction,
    TransactionType,
)


def _fund(user, credits, *, expires_at=None, source=LotSource.PURCHASE):
    return services.credit(
        user=user,
        credits=credits,
        type=TransactionType.CREDIT_PURCHASE,
        source=source,
        expires_at=expires_at,
    )


@pytest.mark.django_db
class TestDefaultDebitIsUnchanged:
    """0.10.0 hosts must not notice this release."""

    def test_all_or_nothing_still_raises_and_moves_nothing(self, user):
        _fund(user, 10)
        with pytest.raises(services.InsufficientCreditsError):
            services.debit(user=user, credits=25, type=TransactionType.AI_CHARGE)
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 10
        assert CreditDebt.objects.count() == 0

    def test_a_covered_debit_still_returns_the_transaction(self, user):
        _fund(user, 10)
        txn = services.debit(user=user, credits=4, type=TransactionType.AI_CHARGE)
        assert isinstance(txn, Transaction)
        assert txn.credits_delta == -4
        assert CreditDebt.objects.count() == 0


@pytest.mark.django_db
class TestPartialDebit:
    def test_charges_what_it_can_and_records_the_rest(self, user):
        _fund(user, 10)
        result = services.debit(
            user=user,
            credits=25,
            type=TransactionType.AI_CHARGE,
            allow_partial=True,
        )
        assert (result.requested, result.debited, result.shortfall) == (25, 10, 15)
        assert result.complete is False
        assert result.balance == 0

        debt = CreditDebt.objects.get()
        assert debt.credits_outstanding == 15
        assert debt.reason == DebtReason.PARTIAL_DEBIT
        assert debt.type == TransactionType.AI_CHARGE
        assert debt.transaction_id == result.transaction.id
        assert result.debt_id == str(debt.id)
        # The ledger row says what was actually billed, not what was asked.
        assert result.transaction.credits_delta == -10
        assert result.transaction.metadata["shortfall"] == 15

    def test_an_empty_wallet_still_writes_the_row_that_explains_the_debt(self, user):
        result = services.debit(
            user=user,
            credits=7,
            type=TransactionType.TRANSCRIPTION_CHARGE,
            allow_partial=True,
        )
        assert (result.debited, result.shortfall) == (0, 7)
        assert result.transaction.credits_delta == 0
        assert CreditDebt.objects.get().credits_outstanding == 7

    def test_a_covered_partial_debit_opens_no_debt(self, user):
        _fund(user, 40)
        result = services.debit(
            user=user, credits=25, type=TransactionType.AI_CHARGE, allow_partial=True
        )
        assert result.complete is True
        assert result.debt is None
        assert CreditDebt.objects.count() == 0

    def test_a_retry_is_told_the_same_thing_the_first_call_was(self, user):
        _fund(user, 10)
        first = services.debit(
            user=user,
            credits=25,
            type=TransactionType.AI_CHARGE,
            idempotency_key="job-1",
            allow_partial=True,
        )
        second = services.debit(
            user=user,
            credits=25,
            type=TransactionType.AI_CHARGE,
            idempotency_key="job-1",
            allow_partial=True,
        )
        # Same answer, same debt, and nothing charged twice.
        assert second.transaction.id == first.transaction.id
        assert (second.debited, second.shortfall) == (10, 15)
        assert second.debt_id == first.debt_id
        assert CreditDebt.objects.count() == 1
        assert Transaction.objects.filter(type=TransactionType.AI_CHARGE).count() == 1


@pytest.mark.django_db
class TestDebtCollection:
    def test_the_next_credit_settles_the_debt_before_it_is_spendable(self, user):
        services.debit(
            user=user, credits=20, type=TransactionType.AI_CHARGE, allow_partial=True
        )
        _fund(user, 50)

        debt = CreditDebt.objects.get()
        assert debt.credits_outstanding == 0
        assert debt.settled_at is not None
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 30

        settlement = Transaction.objects.get(metadata__debt_settlement=True)
        assert settlement.credits_delta == -20
        assert settlement.type == TransactionType.AI_CHARGE
        assert settlement.metadata["debt_id"] == str(debt.id)

    def test_a_partial_top_up_pays_down_what_it_can(self, user):
        services.debit(
            user=user, credits=20, type=TransactionType.AI_CHARGE, allow_partial=True
        )
        _fund(user, 8)

        debt = CreditDebt.objects.get()
        assert debt.credits_outstanding == 12
        assert debt.settled_at is None
        wallet = services.get_or_create_wallet(user)
        wallet.refresh_from_db()
        assert wallet.balance == 0

    def test_debts_are_collected_oldest_first(self, user):
        services.debit(
            user=user, credits=10, type=TransactionType.AI_CHARGE, allow_partial=True
        )
        services.debit(
            user=user,
            credits=10,
            type=TransactionType.TRANSCRIPTION_CHARGE,
            allow_partial=True,
        )
        _fund(user, 10)

        older, newer = CreditDebt.objects.order_by("created_at", "id")
        assert (older.credits_outstanding, newer.credits_outstanding) == (0, 10)

    def test_settlement_spends_the_credits_that_were_about_to_die(self, user):
        services.debit(
            user=user, credits=10, type=TransactionType.AI_CHARGE, allow_partial=True
        )
        soon = timezone.now() + timedelta(days=2)
        _fund(user, 10, expires_at=soon, source=LotSource.SUBSCRIPTION)
        _fund(user, 10)  # non-expiring, granted second

        # The expiring lot paid the debt; the cash credits survive.
        live = list(services.live_lots(services.get_or_create_wallet(user)))
        assert [(lot.credits_remaining, lot.expires_at) for lot in live] == [(10, None)]

    def test_a_grant_can_opt_out_of_collecting(self, user):
        services.debit(
            user=user, credits=10, type=TransactionType.AI_CHARGE, allow_partial=True
        )
        _fund_txn = services.credit(
            user=user,
            credits=10,
            type=TransactionType.ADJUSTMENT,
            source=LotSource.ADJUSTMENT,
            settle_debts=False,
        )
        assert _fund_txn.balance_after == 10
        assert CreditDebt.objects.get().credits_outstanding == 10


@pytest.mark.django_db
class TestCanAfford:
    def test_answers_from_the_lots_without_writing_anything(self, user):
        _fund(user, 30)
        before = (
            services.live_lots(services.get_or_create_wallet(user)).count(),
            services.open_holds(services.get_or_create_wallet(user)).count(),
        )

        yes = services.can_afford(user=user, credits=30)
        no = services.can_afford(user=user, credits=31)

        assert (yes.affordable, yes.balance, yes.shortfall) == (True, 30, 0)
        assert (no.affordable, no.balance, no.shortfall) == (False, 30, 1)
        after = (
            services.live_lots(services.get_or_create_wallet(user)).count(),
            services.open_holds(services.get_or_create_wallet(user)).count(),
        )
        assert before == after

    def test_a_user_without_a_wallet_is_not_given_one(self, user):
        from stapel_billing.models import Wallet

        answer = services.can_afford(user=user, credits=1)
        assert answer.affordable is False
        assert Wallet.objects.count() == 0

    def test_expired_lots_do_not_count_even_before_the_sweep(self, user):
        lot_txn = _fund(
            user, 50, expires_at=timezone.now() + timedelta(seconds=30),
            source=LotSource.SUBSCRIPTION,
        )
        from stapel_billing.models import CreditLot

        CreditLot.objects.filter(granting_transaction=lot_txn).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        answer = services.can_afford(user=user, credits=1)
        # The cached balance still says 50; the lots say otherwise, and the
        # lots are what a debit walks.
        assert answer.affordable is False
        assert answer.balance == 0


@pytest.mark.django_db
class TestIdempotencyKeyIsIndexed:
    def test_the_key_is_written_to_the_indexed_column_and_to_metadata(self, user):
        txn = services.credit(
            user=user,
            credits=5,
            type=TransactionType.ADJUSTMENT,
            source=LotSource.ADJUSTMENT,
            idempotency_key="grant-1",
        )
        txn.refresh_from_db()
        assert txn.idempotency_key == "grant-1"
        # Mirrored, not moved: a 0.10.0 reader looks in metadata.
        assert txn.metadata["idempotency_key"] == "grant-1"

    def test_a_pre_0_11_row_still_short_circuits_through_the_json_fallback(self, user):
        _fund(user, 100)
        txn = services.debit(
            user=user,
            credits=5,
            type=TransactionType.AI_CHARGE,
            idempotency_key="legacy-1",
        )
        # Exactly the shape of a row written before the column existed.
        Transaction.objects.filter(pk=txn.pk).update(idempotency_key="")

        again = services.debit(
            user=user,
            credits=5,
            type=TransactionType.AI_CHARGE,
            idempotency_key="legacy-1",
        )
        assert again.id == txn.id
        assert Transaction.objects.filter(type=TransactionType.AI_CHARGE).count() == 1

    def test_the_fallback_can_be_switched_off_once_a_host_has_backfilled(
        self, user, settings
    ):
        from stapel_billing.conf import billing_settings

        _fund(user, 100)
        txn = services.debit(
            user=user,
            credits=5,
            type=TransactionType.AI_CHARGE,
            idempotency_key="legacy-2",
        )
        Transaction.objects.filter(pk=txn.pk).update(idempotency_key="")
        settings.STAPEL_BILLING = {
            **getattr(settings, "STAPEL_BILLING", {}),
            "LEGACY_IDEMPOTENCY_JSON_LOOKUP": False,
        }
        billing_settings.reload()
        try:
            again = services.debit(
                user=user,
                credits=5,
                type=TransactionType.AI_CHARGE,
                idempotency_key="legacy-2",
            )
            # The point of the flag: without the backfill, the old key no
            # longer protects anything. It is off-by-choice, never by default.
            assert again.id != txn.id
        finally:
            billing_settings.reload()

    def test_a_settlement_row_does_not_steal_the_callers_retry_key(self, user):
        """A debt carries the debiting caller's key; its settlement must not.

        If the settlement row wore that key, the caller's next retry would
        short-circuit onto a row that charged for something else.
        """
        services.debit(
            user=user,
            credits=10,
            type=TransactionType.AI_CHARGE,
            idempotency_key="job-9",
            allow_partial=True,
        )
        _fund(user, 10)
        settlement = Transaction.objects.get(metadata__debt_settlement=True)
        assert settlement.idempotency_key == ""
        assert "idempotency_key" not in settlement.metadata
