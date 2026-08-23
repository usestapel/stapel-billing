"""Credit lots, expiry and reservations (0.8.0).

The claims under test are the ones the two-pool design is sold on: an
expiring subscription bundle is spent before non-expiring cash credits, a
bundle that runs out of time actually dies (and says so in the ledger), and
credits that come back from a reservation come back with the deadline they
left with rather than as money the deployment can never reclaim.
"""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_billing import services
from stapel_billing.models import (
    CreditHold,
    CreditLot,
    HoldAllocation,
    HoldStatus,
    LotSource,
    Transaction,
    TransactionType,
    Wallet,
)


def _backfill():
    """The 0.8.0 RunPython step. Imported by path: the module name starts
    with a digit, so it is not a legal import statement."""
    import importlib

    module = importlib.import_module(
        "stapel_billing.migrations.0003_credit_lots_and_holds"
    )
    return module.backfill_wallet_lots


def _fund(user, credits, *, source=LotSource.PURCHASE, expires_at=None, type=None):
    return services.credit(
        user=user,
        credits=credits,
        type=type or TransactionType.CREDIT_PURCHASE,
        source=source,
        expires_at=expires_at,
    )


@pytest.mark.django_db
class TestLotConsumptionOrder:
    def test_expiring_lot_is_spent_before_the_non_expiring_one(self, user):
        soon = timezone.now() + timedelta(days=2)
        later = timezone.now() + timedelta(days=30)
        _fund(user, 100)  # non-expiring cash credits, granted FIRST
        _fund(user, 30, source=LotSource.SUBSCRIPTION, expires_at=later)
        _fund(user, 10, source=LotSource.SUBSCRIPTION, expires_at=soon)

        txn = services.debit(
            user=user, credits=35, type=TransactionType.AI_CHARGE
        )

        lots = {
            (lot.source, lot.expires_at): lot.credits_remaining
            for lot in CreditLot.objects.filter(wallet__user=user)
        }
        assert lots[(LotSource.SUBSCRIPTION, soon)] == 0        # died used
        assert lots[(LotSource.SUBSCRIPTION, later)] == 5       # then the next deadline
        assert lots[(LotSource.PURCHASE, None)] == 100          # cash untouched
        assert txn.balance_after == 105
        # Three lots would have been two rows of bookkeeping nobody could read
        # back; the split is on the transaction that made it.
        assert [entry["credits"] for entry in txn.metadata["lots"]] == [10, 25]
        assert txn.lot is None  # spanned two lots

    def test_single_lot_debit_names_its_lot(self, user):
        _fund(user, 50)
        txn = services.debit(user=user, credits=20, type=TransactionType.AI_CHARGE)
        assert txn.lot is not None
        assert txn.lot.credits_remaining == 30

    def test_shortfall_is_measured_against_the_lots(self, user):
        _fund(user, 5)
        with pytest.raises(services.InsufficientCreditsError):
            services.debit(user=user, credits=6, type=TransactionType.AI_CHARGE)
        # Nothing moved: no debit row, no lot touched.
        assert not Transaction.objects.filter(
            wallet__user=user, credits_delta__lt=0
        ).exists()
        assert CreditLot.objects.get(wallet__user=user).credits_remaining == 5

    def test_expired_lot_is_not_spendable_even_before_the_sweep(self, user):
        lot = _fund(
            user, 40, source=LotSource.SUBSCRIPTION,
            expires_at=timezone.now() + timedelta(hours=1),
        ).lot
        CreditLot.objects.filter(pk=lot.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        with pytest.raises(services.InsufficientCreditsError):
            services.debit(user=user, credits=1, type=TransactionType.AI_CHARGE)

    def test_credit_refuses_an_expiry_already_in_the_past(self, user):
        with pytest.raises(ValueError):
            _fund(
                user, 10, source=LotSource.SUBSCRIPTION,
                expires_at=timezone.now() - timedelta(seconds=1),
            )


@pytest.mark.django_db
class TestExpiry:
    def _expired_lot(self, user, credits=30):
        txn = _fund(
            user, credits, source=LotSource.SUBSCRIPTION,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        CreditLot.objects.filter(pk=txn.lot.pk).update(
            expires_at=timezone.now() - timedelta(minutes=5)
        )
        return txn.lot

    def test_task_writes_an_expiration_row_and_zeroes_the_cache(self, user):
        from stapel_billing.tasks import expire_credit_lots

        lot = self._expired_lot(user, 30)

        assert expire_credit_lots() == 30

        lot.refresh_from_db()
        assert lot.credits_remaining == 0
        row = Transaction.objects.get(
            wallet__user=user, type=TransactionType.EXPIRATION
        )
        assert row.credits_delta == -30
        assert row.balance_after == 0
        assert row.lot_id == lot.id
        assert Wallet.objects.get(user=user).balance == 0

    def test_expiry_leaves_the_non_expiring_lot_alone(self, user):
        from stapel_billing.tasks import expire_credit_lots

        _fund(user, 100)
        self._expired_lot(user, 30)

        assert expire_credit_lots() == 30
        assert Wallet.objects.get(user=user).balance == 100

    def test_a_wallet_operation_sweeps_before_it_writes_its_row(self, user):
        """A ledger row must never claim a balance holding dead credits."""
        _fund(user, 100)
        self._expired_lot(user, 30)

        txn = services.debit(user=user, credits=10, type=TransactionType.AI_CHARGE)

        assert txn.balance_after == 90
        assert Transaction.objects.filter(
            wallet__user=user, type=TransactionType.EXPIRATION
        ).count() == 1


@pytest.mark.django_db
class TestHold:
    def test_hold_takes_the_credits_out_of_the_lots_without_a_ledger_row(self, user):
        _fund(user, 100)
        held = services.hold(
            user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key="mic:1",
        )
        assert held.status == HoldStatus.HELD
        assert Wallet.objects.get(user=user).balance == 85
        assert CreditLot.objects.get(wallet__user=user).credits_remaining == 85
        # Nothing has been billed yet.
        assert not Transaction.objects.filter(
            wallet__user=user, credits_delta__lt=0
        ).exists()
        assert HoldAllocation.objects.get(hold=held).credits == 15

    def test_hold_is_idempotent_per_key(self, user):
        _fund(user, 100)
        first = services.hold(
            user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key="mic:1",
        )
        second = services.hold(
            user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key="mic:1",
        )
        assert first.id == second.id
        assert Wallet.objects.get(user=user).balance == 85

    def test_hold_refuses_when_the_lots_are_short(self, user):
        _fund(user, 10)
        with pytest.raises(services.InsufficientCreditsError):
            services.hold(
                user=user, credits=15, type=TransactionType.AI_CHARGE,
                idempotency_key="mic:1",
            )
        assert Wallet.objects.get(user=user).balance == 10

    def test_default_ttl_comes_from_settings(self, user, settings):
        settings.STAPEL_BILLING = {
            **getattr(settings, "STAPEL_BILLING", {}),
            "HOLD_DEFAULT_TTL_SECONDS": 60,
        }
        _fund(user, 100)
        held = services.hold(
            user=user, credits=5, type=TransactionType.AI_CHARGE,
            idempotency_key="ttl",
        )
        assert held.expires_at is not None
        assert held.expires_at <= timezone.now() + timedelta(seconds=61)


@pytest.mark.django_db
class TestRelease:
    def test_release_restores_the_same_expiry(self, user):
        """The whole point of HoldAllocation: a released bundle keeps its deadline."""
        deadline = (timezone.now() + timedelta(days=10)).replace(microsecond=0)
        _fund(user, 40, source=LotSource.SUBSCRIPTION, expires_at=deadline)

        held = services.hold(
            user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key="ask:1",
        )
        services.release(hold_id=held.id)

        restored = CreditLot.objects.get(wallet__user=user, source=LotSource.HOLD_RELEASE)
        assert restored.credits_remaining == 15
        assert restored.expires_at == deadline
        assert Wallet.objects.get(user=user).balance == 40
        held.refresh_from_db()
        assert held.status == HoldStatus.RELEASED

    def test_release_is_idempotent(self, user):
        _fund(user, 40)
        held = services.hold(
            user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key="ask:1",
        )
        services.release(hold_id=held.id)
        services.release(hold_id=held.id)
        assert Wallet.objects.get(user=user).balance == 40
        assert CreditLot.objects.filter(
            wallet__user=user, source=LotSource.HOLD_RELEASE
        ).count() == 1

    def test_credits_that_ran_out_the_clock_while_held_die_on_release(self, user):
        txn = _fund(
            user, 20, source=LotSource.SUBSCRIPTION,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        held = services.hold(
            user=user, credits=20, type=TransactionType.AI_CHARGE,
            idempotency_key="slow-job",
        )
        CreditLot.objects.filter(pk=txn.lot.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        services.release(hold_id=held.id)

        assert Wallet.objects.get(user=user).balance == 0
        assert Transaction.objects.filter(
            wallet__user=user, type=TransactionType.EXPIRATION
        ).exists()

    def test_release_of_an_unknown_hold_raises(self, user):
        with pytest.raises(services.HoldNotFoundError):
            services.release(hold_id=uuid.uuid4())

    def test_expire_holds_releases_and_records_the_outcome(self, user):
        _fund(user, 40)
        held = services.hold(
            user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key="crashed",
            expires_at=timezone.now() + timedelta(seconds=30),
        )
        CreditHold.objects.filter(pk=held.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        from stapel_billing.tasks import expire_holds

        assert expire_holds() == 1
        held.refresh_from_db()
        assert held.status == HoldStatus.EXPIRED
        assert Wallet.objects.get(user=user).balance == 40


@pytest.mark.django_db
class TestCapture:
    def test_capture_of_the_full_amount_writes_one_charge(self, user):
        _fund(user, 100)
        held = services.hold(
            user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key="mic:1", description="MIC",
        )
        txn = services.capture(hold_id=held.id)

        assert txn.credits_delta == -15
        assert txn.type == TransactionType.AI_CHARGE
        assert txn.balance_after == 85
        assert txn.metadata["hold_id"] == str(held.id)
        assert Wallet.objects.get(user=user).balance == 85
        held.refresh_from_db()
        assert held.status == HoldStatus.CAPTURED

    def test_capture_under_the_estimate_refunds_into_the_same_lot_expiry(self, user):
        deadline = (timezone.now() + timedelta(days=10)).replace(microsecond=0)
        _fund(user, 40, source=LotSource.SUBSCRIPTION, expires_at=deadline)
        held = services.hold(
            user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key="mic:1",
        )

        txn = services.capture(hold_id=held.id, actual_credits=12)

        assert txn.credits_delta == -12
        assert txn.balance_after == 28
        restored = CreditLot.objects.get(
            wallet__user=user, source=LotSource.HOLD_RELEASE
        )
        assert (restored.credits_remaining, restored.expires_at) == (3, deadline)

    def test_capture_over_the_estimate_takes_the_difference(self, user):
        _fund(user, 40)
        held = services.hold(
            user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key="mic:1",
        )
        txn = services.capture(hold_id=held.id, actual_credits=18)

        assert txn.credits_delta == -18
        assert txn.balance_after == 22
        assert Wallet.objects.get(user=user).balance == 22

    def test_capture_over_what_the_wallet_can_cover_refuses_and_keeps_the_hold(self, user):
        _fund(user, 15)
        held = services.hold(
            user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key="mic:1",
        )
        with pytest.raises(services.InsufficientCreditsError):
            services.capture(hold_id=held.id, actual_credits=20)
        held.refresh_from_db()
        assert held.status == HoldStatus.HELD

    def test_capture_is_idempotent_through_the_hold(self, user):
        _fund(user, 100)
        held = services.hold(
            user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key="mic:1",
        )
        first = services.capture(hold_id=held.id, actual_credits=12)
        second = services.capture(hold_id=held.id, actual_credits=12)
        assert first.id == second.id
        assert Wallet.objects.get(user=user).balance == 88

    def test_capture_of_a_released_hold_is_a_state_error(self, user):
        _fund(user, 100)
        held = services.hold(
            user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key="mic:1",
        )
        services.release(hold_id=held.id)
        with pytest.raises(services.HoldStateError):
            services.capture(hold_id=held.id)


@pytest.mark.django_db
class TestReconcile:
    def test_a_hand_written_balance_is_reported_not_repaired(self, user):
        _fund(user, 100)
        Wallet.objects.filter(user=user).update(balance=999)

        report = services.reconcile_wallet_balances()

        assert len(report) == 1
        assert report[0]["cached_balance"] == 999
        assert report[0]["lot_balance"] == 100
        assert Wallet.objects.get(user=user).balance == 999  # not silently fixed

    def test_a_wallet_the_services_maintain_does_not_drift(self, user):
        _fund(user, 100)
        services.debit(user=user, credits=40, type=TransactionType.AI_CHARGE)
        held = services.hold(
            user=user, credits=10, type=TransactionType.AI_CHARGE,
            idempotency_key="k",
        )
        services.capture(hold_id=held.id, actual_credits=7)
        assert services.reconcile_wallet_balances() == []


@pytest.mark.django_db
class TestCommFunctions:
    def test_hold_capture_release_round_trip(self, user):
        from stapel_core.comm import call

        _fund(user, 100)
        held = call(
            "billing.hold",
            {
                "user_id": str(user.pk),
                "credits": 15,
                "idempotency_key": "fn:1",
                "type": "ai_charge",
            },
        )
        assert held["ok"] is True
        assert held["balance"] == 85

        captured = call(
            "billing.capture", {"hold_id": held["hold_id"], "actual_credits": 12}
        )
        assert captured["ok"] is True
        assert captured["balance"] == 88
        assert Transaction.objects.filter(id=captured["transaction_id"]).exists()

    def test_release_function_gives_the_credits_back(self, user):
        from stapel_core.comm import call

        _fund(user, 100)
        held = call(
            "billing.hold",
            {"user_id": str(user.pk), "credits": 15, "idempotency_key": "fn:2"},
        )
        released = call("billing.release", {"hold_id": held["hold_id"]})
        assert released == {
            "ok": True,
            "balance": 100,
            "status": HoldStatus.RELEASED,
            "reason": None,
        }

    def test_insufficient_credits_is_structural(self, user):
        from stapel_core.comm import call

        _fund(user, 3)
        result = call(
            "billing.hold",
            {"user_id": str(user.pk), "credits": 15, "idempotency_key": "fn:3"},
        )
        assert result == {
            "ok": False,
            "hold_id": None,
            "balance": 3,
            "reason": "insufficient_credits",
        }

    def test_unknown_hold_is_structural(self, user):
        from stapel_core.comm import call

        result = call("billing.capture", {"hold_id": str(uuid.uuid4())})
        assert result["ok"] is False
        assert result["reason"] == "hold_not_found"


class TestBeatSchedule:
    def test_factory_names_all_three_workers(self):
        from stapel_billing.tasks import (
            EXPIRE_HOLDS_TASK_NAME,
            EXPIRE_LOTS_TASK_NAME,
            RECONCILE_TASK_NAME,
            get_billing_beat_schedule,
        )

        tasks = {entry["task"] for entry in get_billing_beat_schedule().values()}
        assert tasks == {
            EXPIRE_LOTS_TASK_NAME,
            EXPIRE_HOLDS_TASK_NAME,
            RECONCILE_TASK_NAME,
        }

    def test_w105_fires_when_the_schedule_omits_the_expiry(self, settings):
        from stapel_billing.checks import check_expiry_is_scheduled

        settings.CELERY_BEAT_SCHEDULE = {
            "something-else": {"task": "myapp.tasks.noop"}
        }
        warnings = check_expiry_is_scheduled(None)
        assert [w.id for w in warnings] == ["stapel_billing.W105"]

    def test_w105_is_silent_once_the_factory_is_registered(self, settings):
        from stapel_billing.checks import check_expiry_is_scheduled
        from stapel_billing.tasks import get_billing_beat_schedule

        settings.CELERY_BEAT_SCHEDULE = {**get_billing_beat_schedule()}
        assert check_expiry_is_scheduled(None) == []

    def test_w105_does_not_second_guess_a_host_without_a_beat(self, settings):
        """A cron-driven host schedules the callables where we cannot see it."""
        from stapel_billing.checks import check_expiry_is_scheduled

        settings.CELERY_BEAT_SCHEDULE = {}
        assert check_expiry_is_scheduled(None) == []


@pytest.mark.django_db
class TestMigrationBackfill:
    def test_one_opening_balance_lot_per_funded_wallet(self, user):
        """The 0.8.0 migration turns a cached balance into a single lot.

        Called directly against the live app registry: the test settings
        create tables from models rather than replaying migrations, and the
        claim under test is what the RunPython step does, not how Django
        applies it.
        """
        from django.apps import apps

        wallet = Wallet.objects.create(user=user, balance=250)
        _backfill()(apps, None)

        lot = CreditLot.objects.get(wallet=wallet)
        assert (lot.credits_initial, lot.credits_remaining) == (250, 250)
        # Adjustment + never-expires: the ledger cannot say which credits
        # these were, so they are the opening balance of the new model and
        # they outlive everything granted after it.
        assert lot.source == LotSource.ADJUSTMENT
        assert lot.expires_at is None
        assert lot.granting_transaction is None

    def test_an_empty_wallet_gets_no_lot(self, user):
        from django.apps import apps

        Wallet.objects.create(user=user, balance=0)
        _backfill()(apps, None)
        assert not CreditLot.objects.exists()


@pytest.mark.django_db
class TestWalletEndpointShowsTheLots:
    """`GET /wallet` publishes what the balance is made of (0.8.1).

    Before this the endpoint answered with one integer, so a client that
    wanted to say "3000 credits expire on the 28th" had to either invent the
    rule or not say it. The lots go out in the debit walker's own order, so
    the client never re-derives which credits go first.
    """

    def test_lots_go_out_in_the_order_the_debit_walker_spends_them(
        self, authed_client, user
    ):
        later = timezone.now() + timedelta(days=30)
        _fund(user, 100)  # non-expiring cash, granted FIRST
        _fund(user, 30, source=LotSource.SUBSCRIPTION, expires_at=later)

        data = authed_client.get("/billing/api/wallet").json()

        assert data["balance"] == 130
        assert [(lot["source"], lot["credits_remaining"]) for lot in data["lots"]] == [
            ("subscription", 30),
            ("purchase", 100),
        ]
        assert data["lots"][0]["credits_initial"] == 30
        assert data["lots"][0]["expires_at"] is not None
        assert data["lots"][1]["expires_at"] is None

    def test_expiring_soon_names_the_first_dated_lot(self, authed_client, user):
        soon = timezone.now() + timedelta(days=2)
        later = timezone.now() + timedelta(days=30)
        _fund(user, 100)
        _fund(user, 30, source=LotSource.SUBSCRIPTION, expires_at=later)
        _fund(user, 10, source=LotSource.SUBSCRIPTION, expires_at=soon)

        data = authed_client.get("/billing/api/wallet").json()

        assert data["expiring_soon"]["credits"] == 10
        assert data["expiring_soon"]["expires_at"] == data["lots"][0]["expires_at"]

    def test_nothing_expires_when_every_lot_is_cash(self, authed_client, user):
        _fund(user, 100)
        data = authed_client.get("/billing/api/wallet").json()
        assert data["expiring_soon"] is None
        assert len(data["lots"]) == 1

    def test_a_spent_lot_is_gone_and_an_expired_one_never_shows(
        self, authed_client, user
    ):
        # credit() refuses a deadline already gone, so the lot is granted
        # alive and then walked past its expiry.
        _fund(
            user,
            10,
            source=LotSource.SUBSCRIPTION,
            expires_at=timezone.now() + timedelta(days=1),
        )
        _fund(user, 100)
        CreditLot.objects.filter(source=LotSource.SUBSCRIPTION).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        data = authed_client.get("/billing/api/wallet").json()

        # Dead credits are not spendable, so they are not in the snapshot —
        # with or without the hourly sweep having run.
        assert [lot["source"] for lot in data["lots"]] == ["purchase"]
        assert data["expiring_soon"] is None

    def test_a_held_hold_is_published_and_a_released_one_is_not(
        self, authed_client, user
    ):
        _fund(user, 100)
        held = services.hold(
            user=user,
            credits=15,
            type=TransactionType.AI_CHARGE,
            idempotency_key="mic:1",
            description="transcribe",
        )

        data = authed_client.get("/billing/api/wallet").json()
        assert data["balance"] == 85
        assert len(data["holds"]) == 1
        published = data["holds"][0]
        assert published["id"] == str(held.id)
        assert published["credits"] == 15
        assert published["status"] == HoldStatus.HELD
        assert published["type"] == TransactionType.AI_CHARGE
        assert published["description"] == "transcribe"

        services.release(hold_id=held.id)

        after = authed_client.get("/billing/api/wallet").json()
        assert after["holds"] == []
        assert after["balance"] == 100

    def test_an_empty_wallet_answers_with_empty_lists(self, authed_client, user):
        data = authed_client.get("/billing/api/wallet").json()
        assert data["lots"] == []
        assert data["holds"] == []
        assert data["expiring_soon"] is None
