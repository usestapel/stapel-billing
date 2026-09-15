"""Action subscriptions of the billing module — erasure, merge, and the payer's letter.

Wallets, transactions, holds and Stripe webhook payloads are this
package's slice of a person, so it is a data owner in the stapel-gdpr
sense: an erasure request for a subject it holds rows about must reach
those rows and come back with a receipt. Until 0.9.0 the only subscriber
here was `user.deleted`, the pre-0.5.0 account signal — an owner declared
in ``DATA_OWNERS`` that never answered the request the orchestrator
actually sends, discoverable only by waiting thirty days for the part to
time out.

Seven handlers, one module, on purpose. Four of them are the account
life-cycle seam described below; the other three (0.14.0) are the payer's
side of this module's own facts, and they are here for the same reason the
rest are — a subscriber is only worth anything where the thing it subscribes
to is emitted, and an owner that emits a fact nobody consumes is indist-
inguishable from an owner that emits nothing:

* ``gdpr.erasure.requested`` — erase the subject (person out, bill kept —
  see :func:`stapel_billing.gdpr.erase_subject`), reply
  ``gdpr.section.erased`` with the counts.
* ``gdpr.owner.probe`` — reply ``gdpr.owner.alive``. It sits **beside**
  the erasure handler because that co-location is the whole evidence: an
  answer proves this subscriber is consumed, not that a container is
  running somewhere.
* ``user.deleted`` — the pre-0.5.0 account signal, which stapel-gdpr
  keeps emitting for one minor. Routed through the same erase call, so
  there is no second erasure implementation to drift.
* ``user.merged`` — the other half of an account's life cycle. A merge
  MOVES credits to the surviving account instead of erasing them, and an
  owner that subscribed only to the deletion half has a silent, wrong
  answer for the other (``stapel_core.lifecycle.E001``). Routed through
  ``services.merge_wallets``, so the wallet's invariants have one
  implementation here too.
* ``payment.completed`` / ``payment.failed`` / ``subscription.changed`` —
  the receipt, the declined-card notice, and the "your plan will not renew"
  notice. See :mod:`stapel_billing.notifications` for why the subscriber
  belongs in this library and the template belongs upstream in
  stapel-notifications.

Handlers are idempotent — delivery is at-least-once (outbox retries,
broker redelivery) — and a redelivery reports the ``0`` rows it touched
rather than pretending it did the work twice.
"""
import logging

from stapel_core.comm import on_action

from .gdpr import OWNER, SUBJECT_TYPES, erase_subject

logger = logging.getLogger(__name__)


def _receipt_id(correlation_id: str, subject_type: str, subject_key: str) -> str:
    """Durable, deterministic proof-of-work id for one erasure.

    Derived rather than random so a redelivery produces the SAME receipt:
    the orchestrator stores it on the part, and two ids for one erasure
    would make an audit trail that cannot be followed back.
    """
    return f"{OWNER}:{subject_type}:{subject_key}:{correlation_id}"


def _emit_receipt(correlation_id: str, subject_type: str, subject_key: str,
                  counts: dict, *, user_id: str | None = None) -> None:
    from stapel_core.comm import emit

    payload = {
        "owner": OWNER,
        "subject_type": subject_type,
        "subject_key": subject_key,
        "correlation_id": correlation_id,
        "receipt_id": _receipt_id(correlation_id, subject_type, subject_key),
        "counts": counts,
    }
    if user_id is not None:
        payload["user_id"] = user_id
    emit("gdpr.section.erased", payload, key=subject_key)


@on_action("gdpr.erasure.requested")
def handle_erasure_requested(event):
    """Erase this module's slice of one subject and confirm it.

    Erasure and confirmation are one transaction (outbox discipline): the
    receipt leaves iff the erasure committed, so this owner can never
    report an erasure a rollback undid.

    A subject type this module does not claim is ignored without a
    receipt — stapel-gdpr creates a part only for owners that claim the
    type, so answering would be answering for somebody else.
    """
    from django.db import transaction

    payload = event.payload or {}
    correlation_id = payload.get("correlation_id")
    subject_type = payload.get("subject_type")
    subject_key = payload.get("subject_key")
    if not correlation_id or not subject_type or not subject_key:
        logger.error(
            "malformed gdpr.erasure.requested event: %s",
            getattr(event, "event_id", "?"),
        )
        return
    if subject_type not in SUBJECT_TYPES:
        logger.debug(
            "gdpr.erasure.requested for subject_type %r — not owned by %s",
            subject_type, OWNER,
        )
        return

    with transaction.atomic():
        counts = erase_subject(
            subject_type, subject_key, workspace_id=payload.get("workspace_id"),
        )
        if counts is None:  # unreachable via SUBJECT_TYPES; empty key
            return
        _emit_receipt(str(correlation_id), subject_type, str(subject_key), counts)
    logger.info(
        "billing erased %s wallet(s) and %s transaction(s) for %s %s "
        "(ids pseudonymized, free text scrubbed, amounts kept)",
        counts["wallets"], counts["transactions"], subject_type, subject_key,
    )


@on_action("gdpr.owner.probe")
def handle_owner_probe(event):
    """Answer the liveness probe with what this owner actually erases.

    Same module as the erasure handler by design (see the module
    docstring): the answer is evidence that the subscriber above is
    consumed. The reported ``subject_types`` are the ones this module can
    really erase, not the ones a settings file hoped for — a probe that
    echoed the host's declaration back would confirm nothing.
    """
    from stapel_core.comm import emit

    payload = event.payload or {}
    correlation_id = payload.get("correlation_id")
    answer = {"owner": OWNER, "subject_types": list(SUBJECT_TYPES)}
    if correlation_id:
        answer["correlation_id"] = str(correlation_id)
    emit("gdpr.owner.alive", answer, key=OWNER)


@on_action("user.deleted")
def handle_user_deleted(event):
    """DEPRECATED account signal — same erase path, kept for one minor.

    stapel-gdpr 0.5.0 emits `gdpr.erasure.requested` and `user.deleted`
    side by side for an account, and drops the latter in 0.6.0. Both land
    here; the second one finds a detached wallet, touches nothing and
    receipts zeroes, which is what an idempotent handler looks like. When
    this handler goes, no erasure logic goes with it — it holds none.
    """
    from django.db import transaction

    payload = event.payload or {}
    user_id = payload.get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s",
                     getattr(event, "event_id", "?"))
        return
    correlation_id = payload.get("correlation_id")
    with transaction.atomic():
        counts = erase_subject("account", user_id)
        if correlation_id:
            _emit_receipt(
                str(correlation_id), "account", str(user_id), counts or {},
                user_id=str(user_id),
            )
    logger.info(
        "billing data erased for deleted user %s (%s wallet(s))",
        user_id, (counts or {}).get("wallets", 0),
    )


@on_action("user.merged")
def handle_user_merged(event):
    """Fold a merged account's wallet into the survivor's.

    ``user.merged`` (stapel-auth 0.30.0) is the opposite of the signal
    above: a guest account was folded into an account that already existed,
    ``from_user_id`` stops existing, and every row that named it belongs to
    ``into_user_id`` now. Nothing is erased. An owner that answered only the
    deletion half would leave the guest's credits in a wallet nobody can
    sign in to — money the customer paid for, invisible to them, and beyond
    the reach of any erasure they could later request
    (``stapel_core.lifecycle.E001``).

    **Merge policy — the credits move and the ledger says so.**
    :func:`stapel_billing.services.merge_wallets` moves the merged wallet's
    lots (not a summed integer: a lot carries its own expiry, and adding two
    balances would silently turn an expiring subscription bundle into
    non-expiring cash), moves the transactions, holds and debts with them,
    writes ONE explicit ``ADJUSTMENT`` row on the survivor for the change,
    and closes the merged wallet at zero with a ``merged_into`` stamp. A
    balance is never written silently here — a wallet that gained credits
    with no row saying why is a support answer nobody can give.

    **Exactly once, provably.** Delivery is at-least-once, so the same event
    WILL arrive twice, and a second credit is money the deployment did not
    receive. The guard is a deterministic idempotency key derived from the
    pair of account ids (:func:`~stapel_billing.services.merge_idempotency_key`,
    the same trick as ``_receipt_id`` above): the second delivery computes
    the same key, finds the ledger row the first one wrote, and returns
    zeroes without touching a lot.

    Malformed or missing ids are logged and dropped rather than raised on —
    a raise makes the bus redeliver a payload that can never succeed. An
    ``IntegrityError`` is deliberately NOT swallowed: it means the surviving
    account is not known to this service yet, which is an event that is
    early rather than poison, and redelivery is what fixes it.
    """
    from django.core.exceptions import ValidationError

    from .services import merge_wallets

    payload = event.payload or {}
    from_user_id = payload.get("from_user_id")
    into_user_id = payload.get("into_user_id")
    if not from_user_id or not into_user_id:
        logger.error(
            "user.merged event without both account ids: %s",
            getattr(event, "event_id", "?"),
        )
        return
    if str(from_user_id) == str(into_user_id):
        logger.error(
            "user.merged names one account twice (%s): %s",
            from_user_id, getattr(event, "event_id", "?"),
        )
        return

    try:
        counts = merge_wallets(
            from_user_id=str(from_user_id), into_user_id=str(into_user_id)
        )
    except (TypeError, ValueError, ValidationError):
        # ValidationError is in the list on purpose: a UUID column rejects a
        # malformed key with ValidationError, which is NOT a ValueError, and
        # a handler that caught only the latter would raise into the bus.
        logger.error(
            "user.merged with unusable ids (from=%r into=%r): %s",
            from_user_id, into_user_id, getattr(event, "event_id", "?"),
        )
        return
    logger.info(
        "billing merged %s credit(s) from user %s into %s (%s lot(s), "
        "%s transaction(s), %s hold(s), %s debt(s))",
        counts["credits"], from_user_id, into_user_id, counts["credit_lots"],
        counts["transactions"], counts["credit_holds"], counts["credit_debts"],
    )


# ─── Telling the payer ─────────────────────────────────────
#
# The three handlers below are the other half of the three facts this module
# already emits. Reasoning, seam choice and the idempotency rule live in
# :mod:`stapel_billing.notifications`; what follows is the wiring.
#
# They subscribe to the EXISTING facts rather than to a new "send an email"
# event, and that is the whole point: ``payment.completed`` is already
# emitted inside the webhook's atomic block through the transactional
# outbox, so a receipt is produced exactly when money was actually taken and
# committed. A second event emitted beside it could be emitted when the
# first one was not, and then the two would have to be kept in agreement
# forever.


class NotificationNotQueued(RuntimeError):
    """The bus refused a notification request. Transient, so: retry.

    A ``RuntimeError`` subclass, not a ``ValidationError`` — the comm layer
    treats ``ValidationError`` as "this payload can never work, park it",
    and a bus that was briefly down is the opposite of that
    (``stapel_core.comm.actions`` docstring, "ValidationError is never a
    retry signal").
    """


def _claim_and_send(*, scope: str, external_id, notification_type, user_id, variables):
    """Claim, publish, and undo the claim if the publish did not happen.

    The ordering is the load-bearing part. Claiming first is what makes a
    redelivery silent; releasing the claim on failure is what stops the
    claim from becoming a permanent record that a letter was sent when it
    was not. Between the two there is a window in which a process death
    loses the letter — small, and the alternative (publish first, claim
    after) loses the idempotency this task exists to provide.
    """
    from .models import ProviderGrant
    from .notifications import _send
    from .services import claim_provider_object

    if not claim_provider_object(
        scope=scope,
        external_id=external_id,
        provider=ProviderGrant.PROVIDER_NOTIFY,
    ):
        logger.info(
            "%s for %s already sent — redelivery, nothing to do",
            notification_type, external_id,
        )
        return False

    if _send(notification_type, user_id=user_id, variables=variables):
        logger.info("%s queued for user %s (%s)", notification_type, user_id, external_id)
        return True

    ProviderGrant.objects.filter(
        provider=ProviderGrant.PROVIDER_NOTIFY, scope=scope,
        external_id=str(external_id),
    ).delete()
    raise NotificationNotQueued(
        f"{notification_type} for {external_id} was not accepted by the bus; "
        "the send-once claim has been released so a redelivery retries it."
    )


@on_action("payment.completed")
def handle_payment_completed_notification(event):
    """Tell the payer their payment went through.

    Keyed on ``transaction_id``: one ledger row is one payment, and it is
    the only id in this fact that is unique per charge. ``user_id`` would
    collapse a customer's whole history into one letter; the bus event id
    would defeat the point, because a redelivery carries a fresh one.
    """
    from .models import ProviderGrant
    from .notifications import TYPE_PAYMENT_SUCCEEDED, payment_succeeded_variables

    payload = event.payload or {}
    user_id = payload.get("user_id")
    transaction_id = payload.get("transaction_id")
    if not user_id or not transaction_id:
        logger.error(
            "payment.completed without user_id/transaction_id (%s) — cannot "
            "address a receipt", getattr(event, "event_id", "?"),
        )
        return

    variables = payment_succeeded_variables(payload)
    if variables is None:
        logger.error(
            "payment.completed %s carries no usable amount — no receipt sent",
            transaction_id,
        )
        return

    _claim_and_send(
        scope=ProviderGrant.SCOPE_NOTIFY_PAYMENT,
        external_id=transaction_id,
        notification_type=TYPE_PAYMENT_SUCCEEDED,
        user_id=user_id,
        variables=variables,
    )


@on_action("payment.failed")
def handle_payment_failed_notification(event):
    """Tell the payer a charge was declined, and how to fix it.

    The adjacent silence, and the one with a live example: of six charges
    on one account, one succeeded only on the retry after an
    ``insufficient_funds`` decline. The customer was told about neither the
    failure nor the eventual success.

    Keyed on the provider object that failed (invoice or payment intent),
    so Stripe's own retry schedule — which fires this event again for the
    same invoice — does not mail the customer once a day about one card.
    """
    from .models import ProviderGrant
    from .notifications import TYPE_PAYMENT_FAILED, payment_failed_variables

    payload = event.payload or {}
    user_id = payload.get("user_id")
    external_id = payload.get("invoice_id") or payload.get("payment_intent_id")
    if not user_id or not external_id:
        logger.error(
            "payment.failed without user_id/invoice_id (%s) — cannot address "
            "a notice", getattr(event, "event_id", "?"),
        )
        return

    variables = payment_failed_variables(payload)
    if variables is None:
        logger.error(
            "payment.failed %s carries no usable amount — no notice sent",
            external_id,
        )
        return

    _claim_and_send(
        scope=ProviderGrant.SCOPE_NOTIFY_PAYMENT_FAILED,
        external_id=external_id,
        notification_type=TYPE_PAYMENT_FAILED,
        user_id=user_id,
        variables=variables,
    )


@on_action("subscription.changed")
def handle_subscription_ending_notification(event):
    """Tell a subscriber their plan will not renew, and until when.

    ``cancel_at_period_end`` is the state in which a person has cancelled,
    is still fully entitled, and — until this handler — was never told when
    that stops. Two of three live subscriptions on the fleet this was found
    on were in exactly that state, with service owed weeks into the future.

    ``subscription.changed`` fires on every provider update, so the claim is
    keyed on ``(subscription, period_end)`` rather than on the event: a
    subscription touched eleven times during one period produces one letter
    about that period, and a RENEWED subscription that is cancelled again
    later has a new period end and therefore correctly gets a new letter.
    """
    from .models import ProviderGrant
    from .notifications import (
        TYPE_SUBSCRIPTION_ENDING,
        iso_date,
        subscription_ending_variables,
    )

    payload = event.payload or {}
    if not payload.get("cancel_at_period_end"):
        # Every other subscription change — a renewal, a plan switch, a
        # status move — is somebody else's letter or nobody's. Silence here
        # is a decision, not the bug this module fixes.
        return

    user_id = payload.get("user_id")
    period_end = iso_date(payload.get("current_period_end"))
    if not user_id or not period_end:
        logger.error(
            "subscription.changed is cancel_at_period_end but carries no "
            "user_id/current_period_end (%s) — a letter that cannot name the "
            "date it is about is worse than none",
            getattr(event, "event_id", "?"),
        )
        return

    variables = subscription_ending_variables(payload)
    if variables is None:
        return

    _claim_and_send(
        scope=ProviderGrant.SCOPE_NOTIFY_SUBSCRIPTION_ENDING,
        external_id=f"{user_id}:{period_end}",
        notification_type=TYPE_SUBSCRIPTION_ENDING,
        user_id=user_id,
        variables=variables,
    )


__all__ = [
    "handle_erasure_requested",
    "handle_owner_probe",
    "handle_user_deleted",
    "handle_user_merged",
    "handle_payment_completed_notification",
    "handle_payment_failed_notification",
    "handle_subscription_ending_notification",
    "NotificationNotQueued",
]
