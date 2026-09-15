"""Internal accounts are METERED, not charged (0.15.0).

The product state this closes: a deployment's own staff could not use the
product they were shipping. Paying made them indistinguishable from a
customer in every revenue report; not paying meant an empty wallet, and an
empty wallet meant either a refusal or — with ``allow_partial`` — a
:class:`CreditDebt` for work nobody intends to collect on, which then ate
the next grant.

The shape asserted here: the ledger row is still written, with its type,
its description and its metadata, and ``credits_delta`` is 0. See
:mod:`stapel_billing.internal`.
"""

import pytest

from stapel_billing import internal, services
from stapel_billing.models import (
    CreditDebt,
    CreditHold,
    HoldStatus,
    Transaction,
    TransactionType,
)


@pytest.fixture
def staff(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username="staffer",
        email="staffer@example.com",
        password="staffpass123",
        is_staff=True,
    )


@pytest.fixture
def meter_only(settings):
    settings.STAPEL_BILLING = {"INTERNAL_ACCOUNT_POLICY": "meter_only"}
    return settings


@pytest.mark.django_db
class TestThePolicyIsOffUntilAskedFor:
    def test_default_charges_staff_like_anybody_else(self, staff):
        assert internal.policy() == internal.POLICY_CHARGE
        assert internal.meter_only(staff) is False
        with pytest.raises(services.InsufficientCreditsError):
            services.debit(user=staff, credits=5, type=TransactionType.AI_CHARGE)

    def test_an_unrecognised_policy_keeps_charging(self, settings, staff):
        # A typo in a billing switch must never be the thing that turns the
        # meter off.
        settings.STAPEL_BILLING = {"INTERNAL_ACCOUNT_POLICY": "meter-only"}
        assert internal.policy() == internal.POLICY_CHARGE
        assert internal.meter_only(staff) is False

    def test_an_external_account_is_still_charged_under_the_policy(
        self, meter_only, user
    ):
        assert internal.meter_only(user) is False
        with pytest.raises(services.InsufficientCreditsError):
            services.debit(user=user, credits=5, type=TransactionType.AI_CHARGE)

    def test_a_resolver_that_raises_charges(self, settings, staff):
        settings.STAPEL_BILLING = {
            "INTERNAL_ACCOUNT_POLICY": "meter_only",
            "INTERNAL_ACCOUNT_RESOLVER": (
                "stapel_billing.tests.test_internal_accounts.exploding_resolver"
            ),
        }
        assert internal.is_internal(staff) is False
        assert internal.meter_only(staff) is False


def exploding_resolver(user):  # pragma: no cover — imported by dotted path
    raise RuntimeError("host resolver is broken")


@pytest.mark.django_db
class TestAnEmptyStaffWalletStillWorks:
    def test_debit_writes_a_zero_cost_row_instead_of_refusing(
        self, meter_only, staff
    ):
        wallet = services.get_or_create_wallet(staff)
        assert wallet.balance == 0

        txn = services.debit(
            user=staff,
            credits=12,
            type=TransactionType.TRANSCRIPTION_CHARGE,
            description="Recording processed (3.0 min)",
            metadata={"recording_id": "r-1"},
        )

        assert txn.credits_delta == 0
        assert txn.balance_after == 0
        # The row keeps everything a usage report reads.
        assert txn.type == TransactionType.TRANSCRIPTION_CHARGE
        assert txn.description == "Recording processed (3.0 min)"
        assert txn.metadata["recording_id"] == "r-1"
        # …plus the two facts that make internal usage countable.
        assert txn.metadata[internal.MARKER] is True
        assert txn.metadata[internal.WAIVED] == 12
        wallet.refresh_from_db()
        assert wallet.balance == 0

    def test_no_debt_is_opened(self, meter_only, staff):
        services.debit(
            user=staff,
            credits=40,
            type=TransactionType.TRANSCRIPTION_CHARGE,
            allow_partial=True,
        )
        # The whole point: an internal run must not leave an invoice behind
        # it that the next grant then silently settles.
        assert CreditDebt.objects.count() == 0

    def test_allow_partial_reports_no_shortfall(self, meter_only, staff):
        result = services.debit(
            user=staff,
            credits=40,
            type=TransactionType.TRANSCRIPTION_CHARGE,
            allow_partial=True,
        )
        assert result.debited == 0
        assert result.requested == 40
        # NOT 40: a waived charge is not a shortfall, and reporting one
        # would put every staff run into the "served for free by accident"
        # log the shortfall exists to raise.
        assert result.shortfall == 0
        assert result.debt is None
        assert result.complete is True

    def test_a_retry_under_the_same_key_neither_charges_nor_reports_a_shortfall(
        self, meter_only, staff
    ):
        first = services.debit(
            user=staff,
            credits=9,
            type=TransactionType.TRANSCRIPTION_CHARGE,
            idempotency_key="run-1",
            allow_partial=True,
        )
        second = services.debit(
            user=staff,
            credits=9,
            type=TransactionType.TRANSCRIPTION_CHARGE,
            idempotency_key="run-1",
            allow_partial=True,
        )
        assert Transaction.objects.count() == 1
        assert second.transaction.id == first.transaction.id
        assert second.shortfall == 0
        assert second.debited == 0
        assert second.requested == 9

    def test_credits_the_account_does_have_are_not_spent(self, meter_only, staff):
        services.credit(
            user=staff,
            credits=100,
            type=TransactionType.ADJUSTMENT,
            source="adjustment",
        )
        services.debit(
            user=staff, credits=30, type=TransactionType.TRANSCRIPTION_CHARGE
        )
        wallet = services.get_or_create_wallet(staff)
        wallet.refresh_from_db()
        # Metered, not charged — the balance is untouched either way.
        assert wallet.balance == 100


@pytest.mark.django_db
class TestThePreflightAgreesWithTheCharge:
    def test_can_afford_says_yes_on_an_empty_internal_wallet(
        self, meter_only, staff
    ):
        answer = services.can_afford(user=staff, credits=500)
        assert answer.affordable is True
        assert answer.shortfall == 0
        # It does not lie about the balance, only about whether the charge
        # will go through — which it will, at zero.
        assert answer.balance == 0

    def test_can_afford_still_refuses_an_external_empty_wallet(
        self, meter_only, user
    ):
        answer = services.can_afford(user=user, credits=500)
        assert answer.affordable is False
        assert answer.shortfall == 500


@pytest.mark.django_db
class TestReservationsAreWaivedToo:
    def test_a_hold_on_an_empty_internal_wallet_succeeds_and_reserves_nothing(
        self, meter_only, staff
    ):
        held = services.hold(
            user=staff,
            credits=50,
            type=TransactionType.AI_CHARGE,
            idempotency_key="ask-1",
        )
        assert held.status == HoldStatus.HELD
        assert held.credits == 0
        assert held.allocations.count() == 0
        assert held.metadata[internal.WAIVED] == 50

    def test_capture_bills_it_at_zero_and_records_the_real_cost(
        self, meter_only, staff
    ):
        held = services.hold(
            user=staff,
            credits=50,
            type=TransactionType.AI_CHARGE,
            description="Ask AI",
            idempotency_key="ask-2",
        )
        txn = services.capture(hold_id=held.id, actual_credits=37)

        assert txn.credits_delta == 0
        assert txn.type == TransactionType.AI_CHARGE
        assert txn.description == "Ask AI"
        assert txn.metadata[internal.MARKER] is True
        # The capture knows the real number; the hold only had an estimate.
        assert txn.metadata[internal.WAIVED] == 37
        held.refresh_from_db()
        assert held.status == HoldStatus.CAPTURED

    def test_releasing_a_waived_hold_conjures_no_credits(self, meter_only, staff):
        held = services.hold(
            user=staff,
            credits=50,
            type=TransactionType.AI_CHARGE,
            idempotency_key="ask-3",
        )
        services.release(hold_id=held.id)
        wallet = services.get_or_create_wallet(staff)
        wallet.refresh_from_db()
        assert wallet.balance == 0
        assert CreditHold.objects.get().status == HoldStatus.RELEASED


@pytest.mark.django_db
class TestReportingStaysAnswerable:
    def test_internal_usage_is_countable_from_the_ledger(self, meter_only, staff):
        for minutes in (3, 5, 11):
            services.debit(
                user=staff,
                credits=minutes,
                type=TransactionType.TRANSCRIPTION_CHARGE,
                idempotency_key=f"run-{minutes}",
            )
        waived = [
            t.metadata[internal.WAIVED]
            for t in Transaction.objects.all()
            if internal.was_waived(t.metadata)
        ]
        # "What did our own testing consume this month" — a query that has
        # no answer at all if the debit had simply been skipped.
        assert sorted(waived) == [3, 5, 11]

    def test_revenue_sums_are_unchanged_by_internal_runs(self, meter_only, staff):
        services.debit(
            user=staff, credits=99, type=TransactionType.TRANSCRIPTION_CHARGE
        )
        spent = sum(
            t.credits_delta for t in Transaction.objects.all() if t.credits_delta < 0
        )
        assert spent == 0
