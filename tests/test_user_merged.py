"""A merge moves the credits; the ledger explains where they went.

The gap these tests close (stapel-core 0.52.1, ``stapel_core.lifecycle.E001``):
this module subscribed ``user.deleted`` and nothing else, so a guest whose
account was folded into an existing one left their credits in a wallet
nobody can sign in to — money the customer paid for, invisible to them, and
beyond the reach of any erasure they could later request. Nothing raised and
nothing retried.

Two claims carry the release and both are asserted below. The credits move
as LOTS, so an expiring subscription bundle stays expiring instead of
becoming non-expiring cash by changing owner. And the credit happens exactly
ONCE across redeliveries, because a second one is money the deployment never
received.
"""
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_billing import services
from stapel_billing.actions import handle_user_merged
from stapel_billing.models import (
    CreditDebt,
    CreditHold,
    CreditLot,
    HoldStatus,
    LotSource,
    Plan,
    Subscription,
    Transaction,
    TransactionType,
    Wallet,
)


def _event(**payload):
    import types

    return types.SimpleNamespace(payload=payload, event_id="evt-1", service="auth")


def _merge(guest, survivor):
    return _event(from_user_id=str(guest.id), into_user_id=str(survivor.id))


@pytest.fixture
def guest(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username="guest", email="guest@example.com", password="x-1234567890",
    )


@pytest.fixture
def survivor(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username="survivor", email="survivor@example.com", password="x-1234567890",
    )


def _fund(user, credits, *, source=LotSource.PURCHASE, expires_at=None):
    return services.credit(
        user=user,
        credits=credits,
        type=TransactionType.CREDIT_PURCHASE,
        source=source,
        expires_at=expires_at,
    )


@pytest.mark.django_db
class TestCreditsMove:
    def test_balance_lands_on_the_survivor_and_the_guest_wallet_closes(
        self, guest, survivor
    ):
        _fund(guest, 120)
        _fund(survivor, 30)

        handle_user_merged(_merge(guest, survivor))

        survivor_wallet = Wallet.objects.get(user=survivor)
        guest_wallet = Wallet.objects.get(merged_into=survivor_wallet)
        assert survivor_wallet.balance == 150
        assert guest_wallet.balance == 0
        assert guest_wallet.user_id is None
        assert not Wallet.objects.filter(user=guest).exists()

    def test_the_ledger_explains_the_change(self, guest, survivor):
        """Never a silent balance write — a row says where the credits came
        from, and `balance_after` matches the wallet the reader can see."""
        _fund(guest, 120)
        _fund(survivor, 30)

        handle_user_merged(_merge(guest, survivor))

        survivor_wallet = Wallet.objects.get(user=survivor)
        row = Transaction.objects.get(
            wallet=survivor_wallet, type=TransactionType.ADJUSTMENT
        )
        assert row.credits_delta == 120
        assert row.balance_after == survivor_wallet.balance == 150
        assert row.idempotency_key == services.merge_idempotency_key(
            str(guest.id), str(survivor.id)
        )
        assert str(guest.id) not in row.idempotency_key  # not a copy of the ids

    def test_lots_move_and_keep_their_expiry(self, guest, survivor):
        """Summing two balances would turn a dated bundle into cash forever."""
        deadline = timezone.now() + timedelta(days=5)
        _fund(guest, 40, source=LotSource.SUBSCRIPTION, expires_at=deadline)
        _fund(guest, 10)
        _fund(survivor, 5)

        handle_user_merged(_merge(guest, survivor))

        survivor_wallet = Wallet.objects.get(user=survivor)
        lots = CreditLot.objects.filter(wallet=survivor_wallet)
        assert lots.count() == 3
        bundle = lots.get(source=LotSource.SUBSCRIPTION)
        assert bundle.expires_at == deadline
        assert bundle.credits_remaining == 40

    def test_transactions_holds_and_debts_follow_the_credits(self, guest, survivor):
        _fund(guest, 100)
        _fund(survivor, 10)
        held = services.hold(
            user=guest, credits=25, type=TransactionType.AI_CHARGE,
            idempotency_key="job-1",
        )
        # A shortfall the guest ran up: service given before it was paid for.
        services.debit(
            user=guest, credits=500, type=TransactionType.AI_CHARGE,
            allow_partial=True,
        )
        assert CreditDebt.objects.filter(wallet__user=guest).exists()

        handle_user_merged(_merge(guest, survivor))

        survivor_wallet = Wallet.objects.get(user=survivor)
        held.refresh_from_db()
        assert held.wallet_id == survivor_wallet.pk
        assert held.status == HoldStatus.HELD
        assert not Transaction.objects.exclude(wallet=survivor_wallet).exists()
        assert not CreditHold.objects.exclude(wallet=survivor_wallet).exists()
        assert not CreditDebt.objects.exclude(wallet=survivor_wallet).exists()

    def test_a_hold_key_the_survivor_already_uses_does_not_break_the_merge(
        self, guest, survivor
    ):
        """Unique per wallet — an arriving twin is renamed, not refused."""
        _fund(guest, 100)
        _fund(survivor, 100)
        services.hold(
            user=guest, credits=10, type=TransactionType.AI_CHARGE,
            idempotency_key="job-1",
        )
        services.hold(
            user=survivor, credits=10, type=TransactionType.AI_CHARGE,
            idempotency_key="job-1",
        )

        handle_user_merged(_merge(guest, survivor))

        survivor_wallet = Wallet.objects.get(user=survivor)
        assert CreditHold.objects.filter(wallet=survivor_wallet).count() == 2
        assert CreditHold.objects.filter(
            wallet=survivor_wallet, idempotency_key="job-1"
        ).count() == 1

    def test_the_survivor_gets_a_wallet_if_it_had_none(self, guest, survivor):
        _fund(guest, 70)
        assert not Wallet.objects.filter(user=survivor).exists()

        handle_user_merged(_merge(guest, survivor))

        assert Wallet.objects.get(user=survivor).balance == 70


@pytest.mark.django_db
class TestExactlyOnce:
    def test_two_deliveries_credit_the_survivor_once(self, guest, survivor):
        """The claim the release rests on: at-least-once must not double-pay."""
        _fund(guest, 120)
        _fund(survivor, 30)
        event = _merge(guest, survivor)

        handle_user_merged(event)
        first = Wallet.objects.get(user=survivor).balance
        handle_user_merged(event)
        handle_user_merged(event)

        survivor_wallet = Wallet.objects.get(user=survivor)
        assert first == 150
        assert survivor_wallet.balance == 150
        assert Transaction.objects.filter(
            wallet=survivor_wallet, type=TransactionType.ADJUSTMENT
        ).count() == 1
        assert CreditLot.objects.filter(wallet=survivor_wallet).count() == 2

    def test_a_redelivery_reports_zero_rows(self, guest, survivor):
        _fund(guest, 120)
        _fund(survivor, 30)

        first = services.merge_wallets(
            from_user_id=str(guest.id), into_user_id=str(survivor.id)
        )
        again = services.merge_wallets(
            from_user_id=str(guest.id), into_user_id=str(survivor.id)
        )

        assert first["credits"] == 120
        assert first["wallets"] == 1
        assert again == {
            "wallets": 0,
            "credits": 0,
            "credit_lots": 0,
            "transactions": 0,
            "credit_holds": 0,
            "credit_debts": 0,
            "subscriptions": 0,
        }


@pytest.mark.django_db
class TestSubscriptions:
    def test_the_survivors_plan_wins(self, guest, survivor):
        Subscription.objects.create(user=guest, plan=Plan.FREE)
        Subscription.objects.create(user=survivor, plan=Plan.PRO)

        handle_user_merged(_merge(guest, survivor))

        assert Subscription.objects.get(user=survivor).plan == Plan.PRO
        assert not Subscription.objects.filter(user=guest).exists()

    def test_a_plan_nobody_else_has_moves_rather_than_evaporating(
        self, guest, survivor
    ):
        Subscription.objects.create(user=guest, plan=Plan.PRO)

        handle_user_merged(_merge(guest, survivor))

        assert Subscription.objects.get(user=survivor).plan == Plan.PRO


@pytest.mark.django_db
class TestBadInputAndStrangers:
    def test_users_this_app_holds_nothing_for_do_nothing(self, guest, survivor):
        """No wallet either side — a quiet no-op, not an error."""
        handle_user_merged(_merge(guest, survivor))

        assert Wallet.objects.count() == 0
        assert Transaction.objects.count() == 0

    @pytest.mark.parametrize(
        "payload",
        [
            {"from_user_id": "not-a-uuid", "into_user_id": "also-not-a-uuid"},
            {"from_user_id": "not-a-uuid", "into_user_id": str(uuid.uuid4())},
            {"from_user_id": str(uuid.uuid4())},
            {"into_user_id": str(uuid.uuid4())},
            {},
            {"from_user_id": "", "into_user_id": str(uuid.uuid4())},
        ],
    )
    def test_malformed_payload_does_not_raise(self, payload, guest):
        """A raise here means the bus redelivers a poison message forever."""
        _fund(guest, 50)

        handle_user_merged(_event(**payload))

        wallet = Wallet.objects.get(user=guest)
        assert wallet.balance == 50
        assert wallet.merged_into_id is None

    def test_one_account_named_twice_is_dropped(self, guest):
        _fund(guest, 50)

        handle_user_merged(
            _event(from_user_id=str(guest.id), into_user_id=str(guest.id))
        )

        wallet = Wallet.objects.get(user=guest)
        assert wallet.balance == 50
        assert wallet.merged_into_id is None


def test_lifecycle_pair_check_is_green():
    """The regression gate: this app answers both halves of the life cycle.

    ``stapel_core.lifecycle.E001`` reports an app that handles
    ``user.deleted`` and registers no ``user.merged`` handler. It is what
    would have caught the silence in the first place, so it stays wired to
    the suite rather than to a one-time audit.
    """
    from stapel_core.comm.lifecycle_checks import check_lifecycle_pairs
    from stapel_core.comm.registry import action_registry

    # Asserted first, because the check reads the registry: a process with
    # no subscribers at all would report [] too, and a green gate that is
    # blind to the thing it gates proves nothing.
    subscribed = {
        getattr(h, "__module__", "") for h in action_registry.handlers("user.merged")
    }
    assert "stapel_billing.actions" in subscribed
    assert check_lifecycle_pairs() == []
