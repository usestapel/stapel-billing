"""Action subscriptions of the billing module — the credit ledger's erasure seam.

Wallets, transactions, holds and Stripe webhook payloads are this
package's slice of a person, so it is a data owner in the stapel-gdpr
sense: an erasure request for a subject it holds rows about must reach
those rows and come back with a receipt. Until 0.9.0 the only subscriber
here was `user.deleted`, the pre-0.5.0 account signal — an owner declared
in ``DATA_OWNERS`` that never answered the request the orchestrator
actually sends, discoverable only by waiting thirty days for the part to
time out.

Four handlers, one module, on purpose:

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


__all__ = [
    "handle_erasure_requested",
    "handle_owner_probe",
    "handle_user_deleted",
    "handle_user_merged",
]
