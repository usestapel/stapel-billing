"""``billing.credit`` — the service-to-service grant (wrapper over services.credit).

A peer that rewards a user (a referral bonus) delivers at least once, so the
key is required and a replay must answer with the first transaction and
never credit twice — including when the duplicates race each other, which
only a locking database can show (the Postgres test at the bottom).
"""

import os
import threading
import uuid
from datetime import timedelta

import pytest
from django.db import connection, connections
from django.test import override_settings
from django.utils import timezone
from stapel_core.comm import call
from stapel_core.comm.exceptions import SchemaValidationError

from stapel_billing import services
from stapel_billing.models import (
    CreditLot,
    LotSource,
    Transaction,
    TransactionType,
    Wallet,
)


def _payload(user_id, **extra):
    return {
        "user_id": str(user_id),
        "credits": 50,
        "idempotency_key": "referrals:reward:1",
        "source": "grant",
        **extra,
    }


@pytest.mark.django_db
class TestCreditFunction:
    def test_credit_lands_as_a_grant_lot_and_a_ledger_row(self, user):
        result = call(
            "billing.credit",
            _payload(
                user.pk,
                description="Referral bonus",
                metadata={"reward_id": "r-1"},
            ),
        )
        assert result["ok"] is True
        assert result["reason"] is None
        assert result["balance"] == 50
        txn = Transaction.objects.get(id=result["transaction_id"])
        assert txn.credits_delta == 50
        assert txn.type == TransactionType.ADJUSTMENT
        assert txn.description == "Referral bonus"
        assert txn.metadata["reward_id"] == "r-1"
        assert txn.idempotency_key == "referrals:reward:1"
        assert txn.lot.source == LotSource.GRANT
        assert txn.lot.expires_at is None
        assert Wallet.objects.get(user=user).balance == 50

    def test_expires_at_dates_the_lot(self, user):
        deadline = timezone.now() + timedelta(days=30)
        result = call(
            "billing.credit",
            _payload(user.pk, expires_at=deadline.isoformat(), type="subscription_bonus"),
        )
        txn = Transaction.objects.get(id=result["transaction_id"])
        assert txn.type == TransactionType.SUBSCRIPTION_BONUS
        assert txn.lot.expires_at == deadline

    def test_naive_expires_at_is_read_as_utc(self, user):
        naive = (timezone.now() + timedelta(days=1)).replace(tzinfo=None, microsecond=0)
        result = call("billing.credit", _payload(user.pk, expires_at=naive.isoformat()))
        lot = Transaction.objects.get(id=result["transaction_id"]).lot
        assert lot.expires_at.replace(tzinfo=None) == naive

    def test_a_replay_returns_the_first_result_and_credits_once(self, user):
        first = call("billing.credit", _payload(user.pk))
        services.debit(user=user, credits=10, type=TransactionType.AI_CHARGE)
        second = call("billing.credit", _payload(user.pk))
        assert second == first
        assert second["balance"] == 50  # the balance the first call left
        assert Wallet.objects.get(user=user).balance == 40
        assert CreditLot.objects.filter(wallet__user=user).count() == 1

    def test_same_key_other_amount_is_a_conflict(self, user):
        call("billing.credit", _payload(user.pk))
        result = call("billing.credit", _payload(user.pk, credits=51))
        assert result == {"ok": False, "balance": None, "reason": "idempotency_conflict"}
        assert Wallet.objects.get(user=user).balance == 50

    def test_same_key_other_source_is_a_conflict(self, user):
        call("billing.credit", _payload(user.pk))
        result = call("billing.credit", _payload(user.pk, source="adjustment"))
        assert result["reason"] == "idempotency_conflict"

    def test_a_key_spent_on_a_debit_is_a_conflict_not_a_credit(self, user):
        services.credit(
            user=user, credits=100, type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
        )
        call("billing.debit", {"user_id": str(user.pk), "credits": 50, "idempotency_key": "k-1"})
        result = call("billing.credit", _payload(user.pk, idempotency_key="k-1"))
        assert result == {"ok": False, "balance": None, "reason": "idempotency_conflict"}
        assert Wallet.objects.get(user=user).balance == 50

    @pytest.mark.parametrize("raw", ["not-a-date", "2001-01-01T00:00:00+00:00"])
    def test_unusable_expires_at_is_refused_structurally(self, user, raw):
        result = call("billing.credit", _payload(user.pk, expires_at=raw))
        assert result == {"ok": False, "balance": None, "reason": "invalid_expires_at"}
        assert not Transaction.objects.filter(wallet__user=user).exists()

    def test_unknown_user_is_structural(self, db):
        result = call("billing.credit", _payload(uuid.uuid4()))
        assert result == {"ok": False, "balance": None, "reason": "user_unknown"}

    def test_a_user_billing_has_not_met_is_materialised_from_identity(self, db):
        uid = uuid.uuid4()
        with override_settings(JWT_CREATE_USERS_FROM_TOKEN=True):
            result = call(
                "billing.credit",
                _payload(uid, identity={"is_anonymous": False, "auth_type": "email"}),
            )
        assert result["ok"] is True
        assert Wallet.objects.get(user_id=uid).balance == 50

    @pytest.mark.parametrize(
        "drop, extra",
        [
            ("idempotency_key", {}),
            ("source", {}),
            (None, {"source": "purchase"}),
            (None, {"source": "subscription"}),
            (None, {"credits": 0}),
            (None, {"type": "refund"}),
            (None, {"description": "x" * 201}),
            (None, {"idempotency_key": ""}),
            (None, {"unknown": 1}),
        ],
    )
    def test_payload_schema_enforced_through_comm_call(self, user, drop, extra):
        payload = _payload(user.pk, **extra)
        if drop:
            payload.pop(drop)
        with pytest.raises(SchemaValidationError):
            call("billing.credit", payload)
        assert not Transaction.objects.exists()


# ─── concurrency, on a database that really locks ───────────


@pytest.mark.skipif(
    not os.environ.get("STAPEL_TEST_DATABASE_URL"),
    reason="needs a real Postgres: set STAPEL_TEST_DATABASE_URL",
)
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("wallet_exists", [True, False])
def test_concurrent_duplicates_credit_exactly_once(wallet_exists):
    assert connection.vendor == "postgresql", (
        "STAPEL_TEST_DATABASE_URL is set but the suite is on "
        f"{connection.vendor} — this proves nothing on SQLite."
    )
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(
        username=f"racer-{wallet_exists}", email=f"racer-{wallet_exists}@example.com",
    )
    if wallet_exists:
        services.get_or_create_wallet(user)

    workers = 8
    barrier = threading.Barrier(workers)
    answers, errors = [], []

    def worker():
        try:
            barrier.wait()
            answers.append(call("billing.credit", _payload(user.pk)))
        except Exception as exc:  # noqa: BLE001 — surfaced by the assert below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(answers) == workers
    assert all(a["ok"] for a in answers)
    assert len({a["transaction_id"] for a in answers}) == 1
    assert Transaction.objects.filter(wallet__user=user).count() == 1
    assert CreditLot.objects.filter(wallet__user=user).count() == 1
    assert Wallet.objects.get(user=user).balance == 50
