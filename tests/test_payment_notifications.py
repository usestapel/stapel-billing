"""The payer gets told — the half of every billing fact that did not exist.

WHY THESE TESTS DISPATCH THROUGH THE REGISTRY INSTEAD OF CALLING THE HANDLER

The defect these cover was never a handler that behaved wrongly. It was the
absence of a handler: ``payment.completed`` was emitted, dispatched and
delivered, correctly, six times against real money, to a set of subscribers
that contained nobody who writes to the customer. Every test that imports a
function and calls it would have passed on the broken tree, because the
function it imports is the thing that was missing.

So the subject under test here is ``action_registry.handlers("payment.completed")``
— the real subscriber list the real consumer iterates. A test that asks the
registry what will happen when the fact arrives is the only shape that can
fail for the actual reason.

RED-FIRST, AND WHAT RED LOOKED LIKE

Run against 0.13.0 (in a worktree at that commit), the first test in this
file fails with::

    AssertionError: a payment.completed fact reached 0 subscriber(s) and
    produced 0 notification request(s)

which is the defect, stated as a number, from the same code path the stand
runs. Twenty-three of the twenty-five tests here failed on that tree.
"""
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_billing.models import ProviderGrant


# ─── Harness ───────────────────────────────────────────────


class _Recorder:
    """Stands in for ``request_notification`` and remembers the asks.

    Substituted on ``stapel_core.notifications`` rather than on this
    package, because that is where the late import inside
    ``notifications._send`` resolves it — patching a name this package
    re-exported would leave the real publisher running and prove nothing.
    """

    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def __call__(self, notification_type, **kwargs):
        self.calls.append((notification_type, kwargs))
        return self.result

    @property
    def types(self):
        return [call[0] for call in self.calls]

    def variables_of(self, notification_type):
        for name, kwargs in self.calls:
            if name == notification_type:
                return kwargs.get("variables") or {}
        raise AssertionError(
            f"no {notification_type} was requested; got {self.types!r}"
        )


@pytest.fixture
def notifier(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(
        "stapel_core.notifications.request_notification", recorder
    )
    return recorder


class _Event:
    """The envelope a comm subscriber is handed."""

    def __init__(self, payload, event_id=None):
        self.payload = payload
        self.event_id = event_id or str(uuid.uuid4())


def _deliver(action: str, payload: dict) -> int:
    """Deliver one fact to every subscriber the registry actually holds.

    Returns the number of subscribers it reached, so a test can say "it was
    delivered, to this many people, and none of them wrote to the customer"
    — which is a different and much more useful failure than "the function
    I imported returned None".
    """
    from stapel_core.comm import action_registry

    handlers = action_registry.handlers(action)
    event = _Event(payload)
    for handler in handlers:
        handler(event)
    return len(handlers)


@pytest.fixture
def payer(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username="payer", email="payer@example.com", password="x"
    )


def _payment_completed(payer, **overrides):
    payload = {
        "user_id": str(payer.id),
        "amount_cents": 2100,
        "currency": "usd",
        "transaction_id": str(uuid.uuid4()),
        # Relative, never a literal: the freshness gate refuses facts older
        # than NOTIFY_MAX_AGE_SECONDS, so a fixed date expires in a week.
        "created_at": (timezone.now() - timedelta(hours=1)).isoformat(),
        "plan": "pro",
        "period_start": "2026-09-09T00:00:00+00:00",
        "period_end": "2026-10-09T00:00:00+00:00",
        "invoice_url": "https://invoice.stripe.com/i/acct_x/live_y",
    }
    payload.update(overrides)
    return payload


# ─── The silence itself ────────────────────────────────────


@pytest.mark.django_db
class TestPaymentCompletedIsAnnouncedToThePayer:

    def test_a_completed_payment_asks_for_a_receipt(self, payer, notifier):
        """THE red-first test. On 0.13.0 this fails with 0 requests."""
        reached = _deliver("payment.completed", _payment_completed(payer))

        assert notifier.calls, (
            f"a payment.completed fact reached {reached} subscriber(s) and "
            f"produced 0 notification request(s)"
        )
        assert "billing.payment_succeeded" in notifier.types

    def test_the_receipt_says_how_much_and_for_what(self, payer, notifier):
        _deliver("payment.completed", _payment_completed(payer))
        variables = notifier.variables_of("billing.payment_succeeded")

        assert variables["amount"] == "$21.00"
        assert variables["item_name"] == "Pro"

    def test_the_receipt_carries_the_period_and_the_invoice(self, payer, notifier):
        _deliver("payment.completed", _payment_completed(payer))
        variables = notifier.variables_of("billing.payment_succeeded")

        assert variables["period_start"] == "2026-09-09"
        assert variables["period_end"] == "2026-10-09"
        assert variables["invoice_url"].startswith("https://invoice.stripe.com/")

    def test_a_package_purchase_names_the_package_and_has_no_period(
        self, payer, notifier
    ):
        """A top-up covers no month, and must not claim to."""
        payload = _payment_completed(payer)
        del payload["plan"], payload["period_start"], payload["period_end"]
        payload["package"] = "starter"
        _deliver("payment.completed", payload)

        variables = notifier.variables_of("billing.payment_succeeded")
        assert variables["item_name"] == "Starter"
        assert "period_start" not in variables
        assert "period_end" not in variables

    def test_the_receipt_is_addressed_to_the_payer(self, payer, notifier):
        _deliver("payment.completed", _payment_completed(payer))
        _, kwargs = notifier.calls[0]
        assert kwargs["user_id"] == str(payer.id)

    def test_a_fact_with_no_usable_amount_sends_nothing(self, payer, notifier):
        """A receipt is the one letter that cannot be written without a number."""
        _deliver(
            "payment.completed", _payment_completed(payer, amount_cents="nonsense")
        )
        assert notifier.calls == []


# ─── Exactly once ──────────────────────────────────────────


@pytest.mark.django_db
class TestAReplayedPaymentSendsOneReceipt:

    def test_three_deliveries_of_one_payment_send_one_receipt(
        self, payer, notifier
    ):
        """At-least-once delivery must not become at-least-once billing mail."""
        payload = _payment_completed(payer)

        _deliver("payment.completed", payload)
        _deliver("payment.completed", payload)
        _deliver("payment.completed", payload)

        assert len(notifier.calls) == 1, (
            f"one payment produced {len(notifier.calls)} receipts"
        )

    def test_the_claim_is_keyed_on_the_payment_not_the_payer(
        self, payer, notifier
    ):
        """A second, genuinely different payment is a second receipt."""
        _deliver("payment.completed", _payment_completed(payer))
        _deliver("payment.completed", _payment_completed(payer))

        assert len(notifier.calls) == 2

    def test_the_claim_names_the_transaction(self, payer, notifier):
        payload = _payment_completed(payer)
        _deliver("payment.completed", payload)

        assert ProviderGrant.objects.filter(
            provider=ProviderGrant.PROVIDER_NOTIFY,
            scope=ProviderGrant.SCOPE_NOTIFY_PAYMENT,
            external_id=payload["transaction_id"],
        ).exists()

    def test_a_notification_claim_does_not_block_the_grant_claim(
        self, payer, notifier
    ):
        """Different questions about one id must not share a row.

        The grant claims live under provider="stripe"; these under "notify".
        Collapsing them would make a granted invoice unnotifiable.
        """
        from stapel_billing.services import claim_provider_object

        payload = _payment_completed(payer)
        _deliver("payment.completed", payload)

        assert claim_provider_object(
            scope=ProviderGrant.SCOPE_NOTIFY_PAYMENT,
            external_id=payload["transaction_id"],
            provider="stripe",
        ) is True

    def test_a_publish_that_did_not_happen_releases_the_claim(
        self, payer, monkeypatch
    ):
        """A dropped letter must stay retriable, not be recorded as sent."""
        from stapel_billing.actions import NotificationNotQueued

        refuser = _Recorder(result=False)
        monkeypatch.setattr(
            "stapel_core.notifications.request_notification", refuser
        )
        payload = _payment_completed(payer)

        with pytest.raises(NotificationNotQueued):
            _deliver("payment.completed", payload)

        assert not ProviderGrant.objects.filter(
            provider=ProviderGrant.PROVIDER_NOTIFY,
            external_id=payload["transaction_id"],
        ).exists()

        # …and the retry the raise asks for actually succeeds.
        accepter = _Recorder(result=True)
        monkeypatch.setattr(
            "stapel_core.notifications.request_notification", accepter
        )
        _deliver("payment.completed", payload)
        assert len(accepter.calls) == 1


# ─── The adjacent silences ─────────────────────────────────


@pytest.mark.django_db
class TestADeclinedChargeIsAnnounced:

    def _failed(self, payer, **overrides):
        from django.utils import timezone

        payload = {
            "user_id": str(payer.id),
            "amount_cents": 1500,
            "currency": "usd",
            "invoice_id": "in_declined_1",
            "plan": "pro",
            "decline_reason": "insufficient_funds",
            # Required since 0.16.0 — a fact this subscriber cannot date is
            # refused, not mailed. See TestAnOldPaymentIsNotMailedToday.
            "created_at": timezone.now().isoformat(),
        }
        payload.update(overrides)
        return payload

    def test_a_declined_charge_asks_for_a_notice(self, payer, notifier):
        reached = _deliver("payment.failed", self._failed(payer))

        assert notifier.calls, (
            f"a payment.failed fact reached {reached} subscriber(s) and "
            f"produced 0 notification request(s)"
        )
        assert "billing.payment_failed" in notifier.types

    def test_the_decline_code_is_turned_into_a_sentence(self, payer, notifier):
        """'insufficient_funds' is not something to print at a customer."""
        _deliver("payment.failed", self._failed(payer))
        variables = notifier.variables_of("billing.payment_failed")

        assert variables["decline_reason"] == (
            "the card did not have enough available funds"
        )

    def test_an_unknown_bare_code_is_dropped_rather_than_printed(
        self, payer, notifier
    ):
        _deliver(
            "payment.failed", self._failed(payer, decline_reason="do_not_honor")
        )
        variables = notifier.variables_of("billing.payment_failed")

        assert "decline_reason" not in variables

    def test_a_human_message_from_the_provider_is_kept(self, payer, notifier):
        _deliver(
            "payment.failed",
            self._failed(payer, decline_reason="Your card was declined."),
        )
        variables = notifier.variables_of("billing.payment_failed")

        assert variables["decline_reason"] == "Your card was declined."

    def test_stripes_own_retry_schedule_does_not_mail_daily(
        self, payer, notifier
    ):
        """One invoice, four attempts, one letter."""
        for _ in range(4):
            _deliver("payment.failed", self._failed(payer))

        assert len(notifier.calls) == 1

    def test_the_button_is_omitted_when_the_host_named_no_billing_page(
        self, payer, notifier
    ):
        _deliver("payment.failed", self._failed(payer))
        variables = notifier.variables_of("billing.payment_failed")

        assert "retry_url" not in variables

    def test_the_button_points_at_the_configured_billing_page(
        self, payer, notifier, settings
    ):
        settings.STAPEL_BILLING = {
            **getattr(settings, "STAPEL_BILLING", {}),
            "BILLING_PAGE_URL": "https://app.example.com/billing",
        }
        _deliver("payment.failed", self._failed(payer))
        variables = notifier.variables_of("billing.payment_failed")

        assert variables["retry_url"] == "https://app.example.com/billing"


@pytest.mark.django_db
class TestASubscriptionThatWillNotRenewIsAnnounced:

    def _changed(self, payer, **overrides):
        payload = {
            "user_id": str(payer.id),
            "plan": "pro",
            "status": "active",
            "current_period_end": "2026-09-28T00:00:00+00:00",
            "cancel_at_period_end": True,
        }
        payload.update(overrides)
        return payload

    def test_a_pending_cancellation_asks_for_a_notice(self, payer, notifier):
        reached = _deliver("subscription.changed", self._changed(payer))

        assert notifier.calls, (
            f"a cancel_at_period_end subscription.changed reached {reached} "
            f"subscriber(s) and produced 0 notification request(s)"
        )
        assert "billing.subscription_ending" in notifier.types

    def test_the_notice_names_the_date_access_stops(self, payer, notifier):
        _deliver("subscription.changed", self._changed(payer))
        variables = notifier.variables_of("billing.subscription_ending")

        assert variables["period_end"] == "2026-09-28"
        assert variables["item_name"] == "Pro"

    def test_a_subscription_that_is_simply_active_says_nothing(
        self, payer, notifier
    ):
        """Every other subscription change is somebody else's letter."""
        _deliver(
            "subscription.changed",
            self._changed(payer, cancel_at_period_end=False),
        )
        assert notifier.calls == []

    def test_eleven_updates_in_one_period_send_one_notice(self, payer, notifier):
        for _ in range(11):
            _deliver("subscription.changed", self._changed(payer))

        assert len(notifier.calls) == 1

    def test_a_new_period_earns_a_new_notice(self, payer, notifier):
        """Cancelled, resumed, renewed, cancelled again is a second letter."""
        _deliver("subscription.changed", self._changed(payer))
        _deliver(
            "subscription.changed",
            self._changed(payer, current_period_end="2026-10-28T00:00:00+00:00"),
        )

        assert len(notifier.calls) == 2

    def test_a_cancellation_with_no_end_date_sends_nothing(
        self, payer, notifier
    ):
        """A letter whose whole subject is a date must have the date."""
        _deliver(
            "subscription.changed", self._changed(payer, current_period_end=None)
        )
        assert notifier.calls == []


# ─── The facts themselves carry what the letters need ──────


@pytest.mark.django_db
class TestTheFactsCarryWhatTheLetterNeeds:

    def test_subscription_changed_always_states_cancel_at_period_end(self, payer):
        """Not inferable from status — so it is emitted, not guessed."""
        import jsonschema
        from django.db import transaction

        from stapel_billing import services
        from stapel_billing.models import Subscription, SubscriptionStatus
        from tests.test_providers import _load_schema
        from stapel_core.comm import action_registry

        received = []
        action_registry.subscribe(
            "subscription.changed", lambda event: received.append(event)
        )
        sub = Subscription.objects.create(
            user=payer, plan="pro", status=SubscriptionStatus.ACTIVE,
            cancel_at_period_end=True,
        )
        # Calling the announcer by hand still has to obey the rule it exists
        # to serve: the fact and the row it describes commit together.
        with transaction.atomic():
            services._announce_subscription(sub)

        assert received
        payload = received[-1].payload
        jsonschema.validate(payload, _load_schema("subscription.changed"))
        assert payload["cancel_at_period_end"] is True

    def test_an_unknown_fact_value_is_an_absent_key_not_a_null(self):
        """`"invoice_url": null` makes every consumer write an is-null branch."""
        from stapel_billing.services import _announce_extras

        assert _announce_extras(invoice_url="", period_end=None, plan="pro") == {
            "plan": "pro"
        }


class TestAmountsAreRenderedTruthfully:
    """No database needed: this is arithmetic and a symbol table."""

    @pytest.mark.parametrize("cents,currency,expected", [
        (2100, "usd", "$21.00"),
        (2100, "eur", "€21.00"),
        (2100, "rub", "₽21.00"),
        (500, "usd", "$5.00"),
        (123456, "usd", "$1,234.56"),
        # Zero-decimal: dividing by 100 would understate this by 100x.
        (1500, "jpy", "¥1,500"),
        # No symbol is better than the wrong symbol.
        (2100, "sek", "21.00 SEK"),
    ])
    def test_amount(self, cents, currency, expected):
        from stapel_billing.notifications import format_amount

        assert format_amount(cents, currency) == expected

    def test_an_unusable_amount_renders_as_nothing_so_the_caller_can_refuse(self):
        from stapel_billing.notifications import format_amount

        assert format_amount(None, "usd") == ""
        assert format_amount("free", "usd") == ""


# ─── The webhook that had no handler at all ────────────────


@pytest.mark.django_db
class TestADeclinedRenewalBecomesAFact:
    """``invoice.payment_failed`` was not in the registry before 0.14.0.

    Not "handled badly" — absent. Stripe retried on its own schedule, the
    plan lapsed, and the deployment's own logs had nothing to say about it.
    """

    def _subscription(self, payer):
        from stapel_billing.models import Subscription, SubscriptionStatus

        return Subscription.objects.create(
            user=payer, plan="pro", status=SubscriptionStatus.ACTIVE,
            stripe_subscription_id="sub_live_1",
            stripe_customer_id="cus_live_1",
        )

    def _emitted(self):
        from stapel_core.comm import action_registry

        received = []
        action_registry.subscribe(
            "payment.failed", lambda event: received.append(event)
        )
        return received

    def test_a_failed_renewal_emits_payment_failed(self, payer):
        import jsonschema

        from stapel_billing import services
        from tests.test_providers import _load_schema

        self._subscription(payer)
        received = self._emitted()

        services.handle_invoice_payment_failed({
            "type": "invoice.payment_failed",
            "data": {"object": {
                "id": "in_failed_1",
                "subscription": "sub_live_1",
                "currency": "usd",
                "amount_due": 1500,
                "last_payment_error": {"decline_code": "insufficient_funds"},
            }},
        })

        assert len(received) == 1
        payload = received[-1].payload
        jsonschema.validate(payload, _load_schema("payment.failed"))
        assert payload["user_id"] == str(payer.id)
        assert payload["amount_cents"] == 1500
        assert payload["invoice_id"] == "in_failed_1"
        assert payload["plan"] == "pro"
        assert payload["decline_reason"] == "insufficient_funds"

    def test_a_failure_nobody_can_be_attributed_to_emits_nothing(self, payer):
        """Better silent than mailing the wrong payer about a stranger's card."""
        from stapel_billing import services

        received = self._emitted()
        services.handle_invoice_payment_failed({
            "type": "invoice.payment_failed",
            "data": {"object": {
                "id": "in_orphan", "subscription": "sub_nobody_knows",
                "currency": "usd", "amount_due": 1500,
            }},
        })

        assert received == []

    def test_a_bare_charge_is_attributed_through_the_stored_customer(self, payer):
        from stapel_billing import services

        self._subscription(payer)
        received = self._emitted()

        services.handle_charge_failed({
            "type": "charge.failed",
            "data": {"object": {
                "id": "ch_failed_1",
                "payment_intent": "pi_failed_1",
                "customer": "cus_live_1",
                "currency": "usd",
                "amount": 500,
                "outcome": {"seller_message": "The bank declined the card."},
            }},
        })

        assert len(received) == 1
        payload = received[-1].payload
        assert payload["payment_intent_id"] == "pi_failed_1"
        assert payload["decline_reason"] == "The bank declined the card."

    def test_the_renewal_receipt_carries_its_period_and_document(self, payer):
        """The success path gained what a receipt needs at the same time."""
        from stapel_core.comm import action_registry

        from stapel_billing import services

        self._subscription(payer)
        received = []
        action_registry.subscribe(
            "payment.completed", lambda event: received.append(event)
        )

        services.handle_invoice_paid({
            "type": "invoice.paid",
            "data": {"object": {
                "id": "in_renewal_1",
                "subscription": "sub_live_1",
                "status": "paid",
                "billing_reason": "subscription_cycle",
                "currency": "usd",
                "amount_paid": 1500,
                "hosted_invoice_url": "https://invoice.stripe.com/i/acct_x/live_y",
                "lines": {"data": [{"period": {
                    "start": 1788912000,  # 2026-09-09
                    "end": 1791590400,    # 2026-10-10
                }}]},
            }},
        })

        facts = [e for e in received if e.payload.get("plan") == "pro"]
        assert facts, "a paid renewal emitted no payment.completed"
        payload = facts[-1].payload
        assert payload["invoice_url"].startswith("https://invoice.stripe.com/")
        assert payload["period_start"].startswith("2026-09-")
        assert payload["period_end"].startswith("2026-10-")


# ─── Old facts must not become new letters ─────────────────


@pytest.mark.django_db
class TestAnOldPaymentIsNotMailedToday:
    """The gap the send-once claim cannot close.

    The claim silences a redelivery of a payment ALREADY notified. Nothing
    taken before 0.14.0 has a claim row — the code that writes them is the
    code that was missing — so to the claim table a replayed outbox row from
    before the fix is indistinguishable from a payment that just happened.
    Six real charges on the fleet this was found on are in that state, and
    the owner is writing to those payers personally; a receipt dated three
    weeks after the charge would arrive on top of that and read as a second
    charge.

    Verified before the gate existed: a 21-day-old `payment.completed`
    produced `['billing.payment_succeeded']`.
    """

    def _old(self, payer, days):
        from django.utils import timezone
        from datetime import timedelta

        return _payment_completed(
            payer,
            created_at=(timezone.now() - timedelta(days=days)).isoformat(),
        )

    def test_a_three_week_old_payment_sends_nothing(self, payer, notifier):
        _deliver("payment.completed", self._old(payer, 21))
        assert notifier.calls == []

    def test_a_payment_from_minutes_ago_still_sends(self, payer, notifier):
        """The gate must not be a new way to lose a receipt."""
        from django.utils import timezone
        from datetime import timedelta

        _deliver("payment.completed", _payment_completed(
            payer, created_at=(timezone.now() - timedelta(minutes=5)).isoformat(),
        ))
        assert notifier.types == ["billing.payment_succeeded"]

    def test_the_boundary_is_the_configured_window(self, payer, notifier, settings):
        settings.STAPEL_BILLING = {
            **getattr(settings, "STAPEL_BILLING", {}),
            "NOTIFY_MAX_AGE_SECONDS": 3 * 24 * 3600,
        }
        _deliver("payment.completed", self._old(payer, 2))
        assert notifier.types == ["billing.payment_succeeded"]

        notifier.calls.clear()
        _deliver("payment.completed", self._old(payer, 4))
        assert notifier.calls == []

    def test_a_refused_fact_leaves_no_claim_behind(self, payer, notifier):
        """The claim table means "a letter was sent", never "considered"."""
        payload = self._old(payer, 21)
        _deliver("payment.completed", payload)

        assert not ProviderGrant.objects.filter(
            provider=ProviderGrant.PROVIDER_NOTIFY,
            external_id=payload["transaction_id"],
        ).exists()

    def test_a_fact_with_no_timestamp_is_refused_not_mailed(
        self, payer, notifier
    ):
        """"I cannot tell how old this is" must not resolve to "mail it"."""
        payload = _payment_completed(payer)
        del payload["created_at"]
        _deliver("payment.completed", payload)

        assert notifier.calls == []

    def test_a_host_that_wants_to_backfill_can_switch_the_gate_off(
        self, payer, notifier, settings
    ):
        settings.STAPEL_BILLING = {
            **getattr(settings, "STAPEL_BILLING", {}),
            "NOTIFY_MAX_AGE_SECONDS": 0,
        }
        _deliver("payment.completed", self._old(payer, 21))
        assert notifier.types == ["billing.payment_succeeded"]

    def test_an_old_declined_charge_is_not_mailed_either(self, payer, notifier):
        from django.utils import timezone
        from datetime import timedelta

        _deliver("payment.failed", {
            "user_id": str(payer.id), "amount_cents": 1500, "currency": "usd",
            "invoice_id": "in_old_1", "plan": "pro",
            "created_at": (timezone.now() - timedelta(days=21)).isoformat(),
        })
        assert notifier.calls == []


@pytest.mark.django_db
class TestASubscriptionThatAlreadyEndedIsNotAnnounced:

    def _changed(self, payer, period_end):
        return {
            "user_id": str(payer.id), "plan": "pro", "status": "active",
            "current_period_end": period_end,
            "cancel_at_period_end": True,
        }

    def test_a_period_that_has_already_passed_sends_nothing(
        self, payer, notifier
    ):
        """"You keep access until 28 September", posted in October, is not a
        late notice — it is a false one."""
        from django.utils import timezone
        from datetime import timedelta

        _deliver("subscription.changed", self._changed(
            payer, (timezone.now() - timedelta(days=10)).isoformat(),
        ))
        assert notifier.calls == []

    def test_a_period_still_running_is_announced(self, payer, notifier):
        from django.utils import timezone
        from datetime import timedelta

        _deliver("subscription.changed", self._changed(
            payer, (timezone.now() + timedelta(days=12)).isoformat(),
        ))
        assert notifier.types == ["billing.subscription_ending"]

    def test_switching_the_age_gate_off_does_not_switch_a_falsehood_on(
        self, payer, notifier, settings
    ):
        """NOTIFY_MAX_AGE_SECONDS is about lateness. A period in the past is
        wrong for every host, at every setting."""
        from django.utils import timezone
        from datetime import timedelta

        settings.STAPEL_BILLING = {
            **getattr(settings, "STAPEL_BILLING", {}),
            "NOTIFY_MAX_AGE_SECONDS": 0,
        }
        _deliver("subscription.changed", self._changed(
            payer, (timezone.now() - timedelta(days=10)).isoformat(),
        ))
        assert notifier.calls == []
