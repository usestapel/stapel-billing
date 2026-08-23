"""Action subscriptions of the billing module — the credit ledger's erasure seam.

Wallets, transactions, holds and Stripe webhook payloads are this
package's slice of a person, so it is a data owner in the stapel-gdpr
sense: an erasure request for a subject it holds rows about must reach
those rows and come back with a receipt. Until 0.9.0 the only subscriber
here was `user.deleted`, the pre-0.5.0 account signal — an owner declared
in ``DATA_OWNERS`` that never answered the request the orchestrator
actually sends, discoverable only by waiting thirty days for the part to
time out.

Three handlers, one module, on purpose:

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


__all__ = [
    "handle_erasure_requested",
    "handle_owner_probe",
    "handle_user_deleted",
]
