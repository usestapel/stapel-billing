"""Erasure over comm: the credit ledger answers a subject request.

The finding this closes: stapel-billing was declared a GDPR data owner
and shipped exactly one subscriber — `user.deleted`, the signal
stapel-gdpr 0.5.0 deprecated — so the request the orchestrator actually
sends (`gdpr.erasure.requested`) reached nobody. The part would have sat
unreceipted until it timed out thirty days later. Worse, the erasure it
did run could not work: `update(user_id=None)` against a NOT NULL column
inside `except Exception: pass` raised on every wallet and reported
success.

So the tests below assert four things that have to hold together: the
person really goes, the **bill really stays**, the receipt says how much
was touched, and the liveness answer comes from the same module — an
`alive` emitted from anywhere else would prove only that a container is
running.
"""
import types
from unittest.mock import patch

import pytest

from stapel_billing import services
from stapel_billing.actions import (
    handle_erasure_requested,
    handle_owner_probe,
    handle_user_deleted,
)
from stapel_billing.gdpr import (
    ERASED_PAYLOAD,
    OWNER,
    PSEUDONYM_PREFIX,
    SUBJECT_TYPES,
    erase_subject,
    pseudonymize,
)
from stapel_billing.models import (
    CreditHold,
    CreditLot,
    HoldStatus,
    LotSource,
    StripeWebhookEvent,
    Subscription,
    Transaction,
    TransactionType,
    Wallet,
)


def _event(**payload):
    return types.SimpleNamespace(payload=payload, event_id="evt-1", service="gdpr")


def _stocked(user, *, credits=100):
    """A wallet with history: a purchase, a spend and a live reservation."""
    services.credit(
        user=user,
        credits=credits,
        type=TransactionType.CREDIT_PURCHASE,
        source=LotSource.PURCHASE,
        amount_cents=1990,
        description="Purchase: pack for alice@example.com",
        metadata={"package": "pack-10", "stripe_session_id": "cs_alice",
                  "note": "gift from bob@example.com"},
    )
    services.debit(
        user=user,
        credits=10,
        type=TransactionType.AI_CHARGE,
        description="Summary of 'Board sync with Alice'",
        metadata={"lot_id": "x", "meeting_title": "Board sync"},
    )
    services.hold(
        user=user,
        credits=5,
        type=TransactionType.AI_CHARGE,
        idempotency_key="hold-1",
        description="Reserved for alice@example.com",
        metadata={"plan": "pro", "email": "alice@example.com"},
    )
    return Wallet.objects.get(user=user)


def _totals(wallet_id):
    """The arithmetic a finance report reads — untouched by an erasure."""
    rows = Transaction.objects.filter(wallet_id=wallet_id)
    return {
        "rows": rows.count(),
        "credits": sum(r.credits_delta for r in rows),
        "cents": sum(r.amount_cents or 0 for r in rows),
        "balances": [r.balance_after for r in rows.order_by("created_at", "id")],
        "lots": sum(
            lot.credits_initial for lot in CreditLot.objects.filter(wallet_id=wallet_id)
        ),
    }


@pytest.mark.django_db
class TestEraseSubject:
    """Remove the person, keep the bill."""

    def test_the_owner_ids_become_pseudonyms_and_the_rows_stay(self, user):
        wallet = _stocked(user)
        before = _totals(wallet.id)
        Subscription.objects.create(user=user, plan="pro",
                                    stripe_customer_id="cus_alice",
                                    stripe_subscription_id="sub_alice")

        counts = erase_subject("account", user.id)

        wallet.refresh_from_db()
        assert wallet.user_id is None
        assert wallet.user_pseudonym == pseudonymize(user.id)
        assert wallet.user_pseudonym.startswith(PSEUDONYM_PREFIX)
        sub = Subscription.objects.get()
        assert (sub.user_id, sub.stripe_customer_id, sub.stripe_subscription_id) == (
            None, "", "",
        )
        assert sub.user_pseudonym == wallet.user_pseudonym
        assert counts["wallets"] == 1 and counts["subscriptions"] == 1
        assert _totals(wallet.id) == before

    def test_the_ledger_totals_survive_the_erasure(self, user):
        """The bill is the product's record: an erasure that restated a
        closed reporting period would be a bookkeeping change wearing a
        privacy costume."""
        wallet = _stocked(user, credits=300)
        before = _totals(wallet.id)
        assert before["rows"] == 2 and before["credits"] == 290

        erase_subject("account", user.id)

        assert _totals(wallet.id) == before
        assert Wallet.objects.get().balance == 285  # 5 still reserved
        assert CreditHold.objects.get().credits == 5

    def test_free_text_and_unknown_metadata_keys_go(self, user):
        wallet = _stocked(user)

        erase_subject("account", user.id)

        for row in Transaction.objects.all():
            assert row.description == ""
            assert "note" not in row.metadata
            assert "meeting_title" not in row.metadata
            # The processor's name for the same person is not an
            # accounting dimension — it is a join key back to a live
            # Stripe customer.
            assert "stripe_session_id" not in row.metadata
        purchase = Transaction.objects.get(type=TransactionType.CREDIT_PURCHASE)
        assert purchase.metadata == {"package": "pack-10"}  # the dimension stays
        held = CreditHold.objects.get()
        assert held.description == "" and held.metadata == {"plan": "pro"}
        assert held.status == HoldStatus.HELD  # a reservation is not resolved by erasure
        assert wallet.id == Wallet.objects.get().id  # nothing was deleted

    def test_stripe_webhook_payloads_are_erased(self, user):
        """By volume the largest store of a person's PII here: a Stripe
        payload carries their email, address and card details."""
        Subscription.objects.create(user=user, plan="pro",
                                    stripe_customer_id="cus_alice",
                                    stripe_subscription_id="sub_alice")
        mine = StripeWebhookEvent.objects.create(
            stripe_event_id="evt_1", event_type="invoice.paid",
            payload={"data": {"object": {"customer": "cus_alice",
                                         "customer_email": "alice@example.com"}}},
        )
        mine_by_object = StripeWebhookEvent.objects.create(
            stripe_event_id="evt_2", event_type="customer.subscription.updated",
            payload={"data": {"object": {"id": "sub_alice"}}},
        )
        theirs = StripeWebhookEvent.objects.create(
            stripe_event_id="evt_3", event_type="invoice.paid",
            payload={"data": {"object": {"customer": "cus_bob"}}},
        )

        counts = erase_subject("account", user.id)

        mine.refresh_from_db()
        mine_by_object.refresh_from_db()
        theirs.refresh_from_db()
        assert mine.payload == ERASED_PAYLOAD
        assert mine_by_object.payload == ERASED_PAYLOAD
        assert theirs.payload["data"]["object"]["customer"] == "cus_bob"
        assert counts["webhook_events"] == 2
        # The dedup key stays: dropping the row would trade a privacy
        # promise for a double-grant on the next redelivery.
        assert StripeWebhookEvent.objects.count() == 3

    def test_another_users_wallet_is_untouched(self, user, django_user_model):
        _stocked(user)
        other = django_user_model.objects.create_user(
            username="other", email="other@example.com", password="x")
        _stocked(other)

        erase_subject("account", user.id)

        survivor = Wallet.objects.get(user=other)
        assert survivor.user_pseudonym == ""
        assert Transaction.objects.filter(
            wallet=survivor, description="Purchase: pack for alice@example.com"
        ).exists()

    def test_a_subject_with_nothing_here_receipts_zeroes(self, user):
        assert erase_subject("account", user.id) == {
            "wallets": 0, "subscriptions": 0, "transactions": 0,
            "credit_holds": 0, "webhook_events": 0,
        }

    def test_an_unclaimed_subject_returns_none(self, user):
        _stocked(user)
        assert erase_subject("workspace", "ws-1") is None
        assert erase_subject("account", None) is None
        assert Wallet.objects.get().user_id == user.id


@pytest.mark.django_db
class TestPseudonymize:
    def test_it_is_keyed_stable_and_idempotent(self, settings):
        first = pseudonymize("u-1")
        assert first.startswith(PSEUDONYM_PREFIX)
        assert pseudonymize("u-1") == first
        assert pseudonymize(first) == first  # a second erasure leaves it alone
        assert pseudonymize("u-2") != first
        settings.SECRET_KEY = "rotated-key"
        assert pseudonymize("u-1") != first  # keyed, not a bare digest


@pytest.mark.django_db
class TestErasureRequested:
    def test_the_receipt_carries_the_counts(self, user):
        _stocked(user)
        Subscription.objects.create(user=user, plan="pro")

        with patch("stapel_core.comm.emit") as m_emit:
            handle_erasure_requested(_event(
                request_id=5, correlation_id="corr-1",
                subject_type="account", subject_key=str(user.id),
            ))

        name, payload = m_emit.call_args.args
        assert name == "gdpr.section.erased"
        assert payload["owner"] == OWNER == "billing"
        assert payload["subject_type"] == "account"
        assert payload["subject_key"] == str(user.id)
        assert payload["correlation_id"] == "corr-1"
        assert payload["counts"] == {
            "wallets": 1, "subscriptions": 1, "transactions": 2,
            "credit_holds": 1, "webhook_events": 0,
        }
        assert Wallet.objects.get().user_id is None

    def test_redelivery_touches_nothing_and_says_so(self, user):
        """At-least-once delivery: the second receipt reports 0 rather
        than claiming the work twice, and carries the same receipt id."""
        _stocked(user)
        event = _event(request_id=6, correlation_id="corr-2",
                       subject_type="account", subject_key=str(user.id))

        with patch("stapel_core.comm.emit") as m_emit:
            handle_erasure_requested(event)
            handle_erasure_requested(event)

        first, second = [c.args[1] for c in m_emit.call_args_list]
        assert first["counts"]["wallets"] == 1
        assert second["counts"] == {
            "wallets": 0, "subscriptions": 0, "transactions": 0,
            "credit_holds": 0, "webhook_events": 0,
        }
        assert first["receipt_id"] == second["receipt_id"]

    def test_an_unclaimed_subject_is_ignored_without_a_receipt(self, user):
        """gdpr creates a part only for owners that claim the type, so a
        receipt here would be answering for somebody else."""
        _stocked(user)
        with patch("stapel_core.comm.emit") as m_emit:
            handle_erasure_requested(_event(
                request_id=7, correlation_id="corr-3",
                subject_type="workspace", subject_key="ws-1",
            ))
        m_emit.assert_not_called()
        assert Wallet.objects.get().user_id == user.id

    @pytest.mark.parametrize("payload", [
        {"correlation_id": "c", "subject_type": "account"},   # no key
        {"correlation_id": "c", "subject_key": "1"},          # no type
        {"subject_type": "account", "subject_key": "1"},      # no correlation
    ])
    def test_a_malformed_request_is_dropped(self, user, payload):
        _stocked(user)
        with patch("stapel_core.comm.emit") as m_emit:
            handle_erasure_requested(_event(**payload))
        m_emit.assert_not_called()
        assert Wallet.objects.get().user_id == user.id

    def test_the_receipt_validates_against_the_committed_schema(self, user):
        """No mock: the emit goes through comm with schema validation on,
        so schemas/emits/gdpr.section.erased.json is what accepts it."""
        from stapel_core.comm import subscribe_action

        seen = []
        subscribe_action("gdpr.section.erased", lambda e: seen.append(e.payload))
        _stocked(user)
        handle_erasure_requested(_event(
            request_id=8, correlation_id="corr-4",
            subject_type="account", subject_key=str(user.id),
        ))
        assert seen[-1]["counts"]["wallets"] == 1


@pytest.mark.django_db
class TestOwnerProbe:
    def test_alive_names_the_owner_and_what_it_can_erase(self):
        with patch("stapel_core.comm.emit") as m_emit:
            handle_owner_probe(_event(correlation_id="corr-probe"))
        name, payload = m_emit.call_args.args
        assert name == "gdpr.owner.alive"
        assert payload == {
            "owner": "billing", "subject_types": ["account"],
            "correlation_id": "corr-probe",
        }

    def test_only_account_is_claimed(self):
        """Every row here hangs off a wallet keyed by one id. An owner
        that claims what it cannot do turns the health table green on a
        subject nobody erased."""
        assert SUBJECT_TYPES == ("account",)
        with patch("stapel_core.comm.emit") as m_emit:
            handle_owner_probe(_event(correlation_id="c"))
        assert m_emit.call_args.args[1]["subject_types"] == ["account"]

    def test_a_probe_without_a_correlation_id_still_answers(self):
        with patch("stapel_core.comm.emit") as m_emit:
            handle_owner_probe(_event())
        assert m_emit.call_args.args[1] == {
            "owner": "billing", "subject_types": ["account"],
        }

    def test_the_answer_comes_from_the_module_that_erases(self):
        """Co-location IS the evidence: `alive` proves the erasure
        subscriber is consumed, not that a container is deployed. Two
        modules would make it prove nothing."""
        from stapel_core.comm.registry import action_registry

        assert handle_erasure_requested in action_registry.handlers(
            "gdpr.erasure.requested")
        assert handle_owner_probe in action_registry.handlers("gdpr.owner.probe")
        assert handle_owner_probe.__module__ == handle_erasure_requested.__module__

    def test_the_alive_answer_validates_against_the_committed_schema(self):
        from stapel_core.comm import subscribe_action

        seen = []
        subscribe_action("gdpr.owner.alive", lambda e: seen.append(e.payload))
        handle_owner_probe(_event(correlation_id="corr-probe-2"))
        assert seen[-1]["owner"] == "billing"


@pytest.mark.django_db
class TestDeprecatedUserDeleted:
    """stapel-gdpr keeps emitting `user.deleted` for accounts until its
    0.6.0. It routes through the same erase call, so the handler can be
    deleted without deleting any erasure logic."""

    def test_it_erases_through_the_same_path(self, user):
        _stocked(user)
        with patch("stapel_core.comm.emit"):
            handle_user_deleted(_event(user_id=str(user.id)))
        wallet = Wallet.objects.get()
        assert wallet.user_id is None
        assert wallet.user_pseudonym == pseudonymize(user.id)
        assert Transaction.objects.count() == 2  # the bill stayed

    def test_it_receipts_when_a_correlation_id_is_present(self, user):
        _stocked(user)
        with patch("stapel_core.comm.emit") as m_emit:
            handle_user_deleted(_event(user_id=str(user.id),
                                       correlation_id="corr-legacy"))
        payload = m_emit.call_args.args[1]
        assert payload["correlation_id"] == "corr-legacy"
        assert payload["user_id"] == str(user.id)
        assert payload["counts"]["wallets"] == 1
