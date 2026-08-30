"""Wallet operations and payment-provider integration helpers.

A wallet is a set of :class:`~stapel_billing.models.CreditLot` rows, not a
single integer: credits arrive knowing where they came from and when they
die, and spend walks them **expiring-soonest first** so a subscription
bundle is burned before the non-expiring credits a customer paid cash for.
``Wallet.balance`` is a maintained cache of that walk's total — recomputed
from the lots inside the same ``select_for_update()`` block as every
mutation, so a read path never has to aggregate and a write path can never
leave the two disagreeing.

Reservations (``hold`` → ``capture`` / ``release``) sit in front of the
ledger: a hold really removes the credits from the lots, and the ledger row
is written when the work is actually billed. Anything not billed goes back
into a lot that carries the **original** lot's expiry, because a released
subscription credit that came back non-expiring would be a bundle the
deployment can never reclaim.

The payment backend is pluggable: ``get_provider()`` resolves the
``STAPEL_BILLING["PAYMENT_PROVIDER"]`` dotted path (Stripe by default)
and the checkout/portal/webhook helpers below delegate to it.  Provider
credentials are read lazily at call time — nothing is frozen at import.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Optional

from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from stapel_core.comm import emit
from stapel_core.signals import payment_completed, subscription_changed

from .catalog import CREDIT_PACKAGES_BY_SLUG, PLANS_BY_SLUG
from .models import (
    CreditDebt,
    CreditHold,
    CreditLot,
    DebtReason,
    HoldAllocation,
    HoldStatus,
    LotSource,
    PendingSubscriptionPeriod,
    ProviderGrant,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    Wallet,
)
from .providers.base import PaymentProvider

logger = logging.getLogger(__name__)


class InsufficientCreditsError(Exception):
    pass


class HoldNotFoundError(Exception):
    """No hold with that id. Distinct from "the hold is in the wrong state"."""


class HoldStateError(Exception):
    """The hold exists but has already been captured, released or expired."""


class HoldKeyResolvedError(HoldStateError):
    """This wallet has used that idempotency key, and that hold is over.

    Raised by :func:`hold` instead of handing back a hold that reserves
    nothing. Until 0.11.0 the idempotency short-circuit matched a hold in
    ANY status, so re-using a key whose hold had been released, captured or
    expired answered "ok, reserved" with zero credits actually held — and
    the capture that followed failed with ``hold_not_held``, at the point
    where the work was already done and had to be given away.

    Carries the resolved hold so the caller can decide: a genuine retry
    of an already-finished operation is done (nothing more to do), while a
    NEW operation that happens to reuse the key needs a new key.
    """

    def __init__(self, hold: "CreditHold"):
        self.hold = hold
        self.hold_id = hold.id
        self.status = hold.status
        super().__init__(
            f"idempotency_key {hold.idempotency_key!r} belongs to hold "
            f"{hold.id}, which is {hold.status} — nothing is reserved"
        )


@dataclass(frozen=True)
class DebitResult:
    """What a partial debit actually managed to charge.

    Returned by :func:`debit` only when the caller passes
    ``allow_partial=True`` — the all-or-nothing default still returns the
    ``Transaction``, so nothing that worked on 0.10.0 changes shape.

    ``shortfall`` is the part the wallet could not cover. It is not lost:
    ``debt`` is the :class:`~stapel_billing.models.CreditDebt` row that
    records it, and the next credit into this wallet collects it.
    """

    transaction: Transaction
    requested: int
    debited: int
    shortfall: int
    balance: int
    debt: Optional[CreditDebt] = None

    @property
    def debt_id(self) -> Optional[str]:
        return str(self.debt.id) if self.debt is not None else None

    @property
    def complete(self) -> bool:
        """True when the wallet covered the whole charge."""
        return self.shortfall == 0


@dataclass(frozen=True)
class Affordability:
    """The read-only answer to "would a charge of N go through right now".

    A pre-flight, not a reservation: nothing is locked and nothing is
    written, so two callers can both be told yes and only one of them will
    be right. That is the honest shape of the question — the caller that
    needs the answer to *stay* true takes a :func:`hold`.
    """

    affordable: bool
    balance: int
    requested: int
    shortfall: int


@dataclass(frozen=True)
class ClawbackResult:
    """What a refund/dispute/credit-note clawback recovered.

    ``reclaimed`` came back out of the lots the grant created; ``forgiven``
    had already expired unspent (the deployment took those credits back
    long ago, and charging for them twice would be a second clawback of the
    same credits); ``debt`` is what was already spent and is now owed.
    """

    transaction: Transaction
    requested: int
    reclaimed: int
    forgiven: int
    outstanding: int
    debt: Optional[CreditDebt] = None


def _get_user_model():
    from django.contrib.auth import get_user_model

    return get_user_model()


def get_or_create_wallet(user) -> Wallet:
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


# ─── Credit lots ────────────────────────────────────────────
#
# Every helper below assumes the caller already holds the wallet's row lock
# (``Wallet.objects.select_for_update()``). That lock — not the ordering of
# the queries — is what serialises two concurrent spends of the last credit.


def _lock_wallet(user) -> Wallet:
    return Wallet.objects.select_for_update().get_or_create(user=user)[0]


def _live_lot_filter(now) -> Q:
    """Lots that can still be spent: unspent and not past their expiry."""
    return Q(credits_remaining__gt=0) & (
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )


def _live_lots_ordered(wallet, *, now):
    """The consumption order: expiring soonest first, non-expiring last.

    ``nulls_last`` is the whole point — a lot with no expiry is the one that
    can afford to wait, so it absorbs whatever the dated lots cannot cover.
    ``created_at`` breaks ties so the walk is deterministic (and so an
    older subscription bundle goes before a newer one with the same deadline).
    """
    return (
        CreditLot.objects.filter(wallet=wallet)
        .filter(_live_lot_filter(now))
        .order_by(F("expires_at").asc(nulls_last=True), "created_at", "id")
    )


def _live_lots(wallet, *, now):
    """The consumption walk, locked: what :func:`debit` actually spends."""
    return _live_lots_ordered(wallet, now=now).select_for_update()


def live_lots(wallet, *, now=None):
    """The wallet's live lots, in the order a debit would spend them.

    The read-only twin of the consumption walk — same ordering, no row lock,
    because a caller that is only *reporting* the lots must not queue behind
    (or in front of) the caller that is spending them. Sharing the ordering
    with the walker is the point: "which credits go first" is the server's
    answer, and a client handed the list in any other order would have to
    guess it back.
    """
    return _live_lots_ordered(wallet, now=now or timezone.now())


def open_holds(wallet):
    """Reservations still holding credits (``status=held``), newest first.

    Captured, released and expired holds are history: their credits are
    either billed or back in the lots, so they say nothing about what this
    wallet can spend right now.
    """
    return CreditHold.objects.filter(wallet=wallet, status=HoldStatus.HELD)


def _live_balance(wallet, *, now) -> int:
    """Sum of the live lots — the truth ``Wallet.balance`` caches."""
    return (
        CreditLot.objects.filter(wallet=wallet)
        .filter(_live_lot_filter(now))
        .aggregate(total=Sum("credits_remaining"))["total"]
        or 0
    )


def _sync_balance(wallet, *, now) -> int:
    """Recompute the cached balance from the lots and persist it.

    Derived rather than incremented: the lot table is the truth, the sum is
    one indexed query under a lock we already hold, and a cache that is
    recomputed cannot slowly drift away from what it claims to summarise.
    """
    balance = _live_balance(wallet, now=now)
    if balance != wallet.balance:
        wallet.balance = balance
        wallet.save(update_fields=["balance", "updated_at"])
    return balance


def _expire_due_lots(wallet, *, now) -> int:
    """Zero this wallet's expired lots, one EXPIRATION row each.

    Runs at the top of every wallet operation as well as from the hourly
    worker: an operation that wrote a ledger row while an expired lot still
    counted would record a ``balance_after`` containing credits the customer
    can no longer spend.
    """
    due = list(
        CreditLot.objects.select_for_update()
        .filter(wallet=wallet, credits_remaining__gt=0, expires_at__lte=now)
        .order_by("expires_at", "created_at", "id")
    )
    if not due:
        return 0
    # ``_live_balance`` already excludes every lot in *due*, so adding them
    # back gives the balance as it stood before this sweep — the number the
    # first EXPIRATION row steps down from.
    running = _live_balance(wallet, now=now) + sum(lot.credits_remaining for lot in due)
    expired_total = 0
    for lot in due:
        amount = lot.credits_remaining
        lot.credits_remaining = 0
        lot.save(update_fields=["credits_remaining"])
        expired_total += amount
        running -= amount
        Transaction.objects.create(
            wallet=wallet,
            lot=lot,
            type=TransactionType.EXPIRATION,
            amount_cents=None,
            credits_delta=-amount,
            balance_after=running,
            description=f"Credits expired ({lot.get_source_display()})",
            metadata={
                "lot_id": str(lot.id),
                "source": lot.source,
                "expires_at": lot.expires_at.isoformat(),
            },
        )
    _sync_balance(wallet, now=now)
    logger.info(
        "expired %s credit(s) in %s lot(s) on wallet %s",
        expired_total,
        len(due),
        wallet.id,
    )
    return expired_total


def _consume_lots(wallet, credits: int, *, now) -> list[tuple[CreditLot, int]]:
    """Take *credits* out of the live lots, expiring-first. Raises when short.

    The shortfall is measured against the lots, never against the cached
    balance: the lots are what will actually be decremented, so they are what
    the refusal has to be true about.
    """
    lots = list(_live_lots(wallet, now=now))
    available = sum(lot.credits_remaining for lot in lots)
    if available < credits:
        raise InsufficientCreditsError(f"balance={available} < required={credits}")
    taken: list[tuple[CreditLot, int]] = []
    outstanding = credits
    for lot in lots:
        if outstanding <= 0:
            break
        amount = min(outstanding, lot.credits_remaining)
        lot.credits_remaining -= amount
        lot.save(update_fields=["credits_remaining"])
        taken.append((lot, amount))
        outstanding -= amount
    return taken


def _consumption_metadata(taken: list[tuple[CreditLot, int]]) -> list[dict]:
    return [{"lot_id": str(lot.id), "credits": amount} for lot, amount in taken]


def _single_lot(taken: list[tuple[CreditLot, int]]) -> Optional[CreditLot]:
    """The one lot the operation touched, or None when it spanned several."""
    return taken[0][0] if len(taken) == 1 else None


def _transaction_by_idempotency_key(wallet, key: str) -> Optional[Transaction]:
    """The row a previous call with this key wrote, or None.

    Reads the indexed ``Transaction.idempotency_key`` column first. Rows
    written before 0.11.0 carry the key only in ``metadata``, so the JSON
    lookup stays as a second query — expand-only: the new column is a
    double-write, not a replacement, and the old spelling keeps answering
    until a host has backfilled it and turned
    ``STAPEL_BILLING["LEGACY_IDEMPOTENCY_JSON_LOOKUP"]`` off.

    The JSON half is the one the audit flagged: unindexed, it scans the
    wallet's whole transaction history on every charge, and that history
    only ever grows.
    """
    existing = Transaction.objects.filter(wallet=wallet, idempotency_key=key).first()
    if existing is not None:
        return existing
    from .conf import legacy_idempotency_json_lookup

    if not legacy_idempotency_json_lookup():
        return None
    return Transaction.objects.filter(
        wallet=wallet, metadata__idempotency_key=key
    ).first()


# ─── Debts (credits owed, not credits held) ─────────────────


def open_debts(wallet):
    """This wallet's uncollected debts, oldest first — the collection order.

    Oldest-first because a debt is not a fine: the deployment took the risk
    in the order the charges arrived, and settling the newest first would
    leave the oldest one outstanding forever on a wallet that is topped up
    in small amounts.
    """
    return CreditDebt.objects.filter(
        wallet=wallet, settled_at__isnull=True, credits_outstanding__gt=0
    ).order_by("created_at", "id")


def outstanding_debt(wallet) -> int:
    """Total credits this wallet owes. 0 for the overwhelming majority."""
    return (
        open_debts(wallet).aggregate(total=Sum("credits_outstanding"))["total"] or 0
    )


def _clean_metadata(metadata: Optional[dict]) -> dict:
    """A copy without the caller's retry key.

    Copying ``idempotency_key`` onto a row the caller did not ask for would
    make the duplicate lookup in :func:`credit` / :func:`debit` find a
    settlement row and short-circuit a real charge.
    """
    return {k: v for k, v in (metadata or {}).items() if k != "idempotency_key"}


def _open_debt(
    wallet,
    *,
    credits: int,
    reason: str,
    type: str,
    description: Optional[str] = None,
    metadata: Optional[dict] = None,
    idempotency_key: Optional[str] = None,
) -> CreditDebt:
    """Record credits consumed without cover. Caller holds the wallet lock."""
    debt = CreditDebt.objects.create(
        wallet=wallet,
        credits_initial=credits,
        credits_outstanding=credits,
        reason=reason,
        type=type,
        description=description,
        metadata=_clean_metadata(metadata),
        idempotency_key=idempotency_key or "",
    )
    logger.warning(
        "wallet %s owes %s credit(s) (%s, debt %s)",
        wallet.id,
        credits,
        reason,
        debt.id,
    )
    return debt


def _settle_debts(wallet, *, now) -> int:
    """Collect what this wallet owes out of its live lots, oldest debt first.

    Runs inside :func:`credit`, under the same row lock: credits that arrive
    on a wallet with an outstanding debt are not spendable credits yet, and
    a collection that happened on a later, separate lock could be raced by
    the spend it exists to prevent.

    Each collection is a real debit — it walks the lots expiring-soonest
    first like any other spend and writes its own ledger row — so the debt
    is repaid with the credits that were about to die rather than with the
    ones the customer paid cash for.
    """
    debts = list(open_debts(wallet).select_for_update())
    if not debts:
        return 0
    collected = 0
    for debt in debts:
        available = _live_balance(wallet, now=now)
        if available <= 0:
            break
        amount = min(debt.credits_outstanding, available)
        taken = _consume_lots(wallet, amount, now=now)
        balance = _sync_balance(wallet, now=now)
        debt.credits_outstanding -= amount
        fields = ["credits_outstanding"]
        if debt.credits_outstanding <= 0:
            debt.settled_at = now
            fields.append("settled_at")
        debt.save(update_fields=fields)
        Transaction.objects.create(
            wallet=wallet,
            lot=_single_lot(taken),
            type=debt.type,
            amount_cents=None,
            credits_delta=-amount,
            balance_after=balance,
            description=debt.description or f"Debt settlement ({debt.reason})",
            metadata={
                **_clean_metadata(debt.metadata),
                "debt_id": str(debt.id),
                "debt_settlement": True,
                "debt_reason": debt.reason,
                "lots": _consumption_metadata(taken),
            },
        )
        collected += amount
        logger.info(
            "collected %s credit(s) against debt %s on wallet %s (%s left)",
            amount,
            debt.id,
            wallet.id,
            debt.credits_outstanding,
        )
    return collected


def _refund_allocations(hold: CreditHold, credits: int) -> int:
    """Hand *credits* back, newest allocation first, preserving lot expiry.

    Newest-first because consumption was expiring-first: what a partial
    capture actually used should be charged to the credits that were closest
    to dying, so the refund comes off the other end of the same walk.

    The credits come back as fresh ``hold_release`` lots carrying the source
    lot's ``expires_at`` rather than by topping the original lot back up: a
    lot that could go back up would no longer be a truthful record of what
    was alive when. A lot whose deadline has since passed is recreated
    already dead, and the expiry sweep turns it into an EXPIRATION row —
    credits that ran out the clock while reserved are gone, not renewed.
    """
    allocations = list(
        hold.allocations.select_related("lot").order_by("-created_at", "-id")
    )
    outstanding = credits
    for alloc in allocations:
        if outstanding <= 0:
            break
        amount = min(outstanding, alloc.credits)
        if amount <= 0:
            continue
        CreditLot.objects.create(
            wallet_id=hold.wallet_id,
            source=LotSource.HOLD_RELEASE,
            credits_initial=amount,
            credits_remaining=amount,
            expires_at=alloc.lot.expires_at,
        )
        alloc.credits -= amount
        alloc.save(update_fields=["credits"])
        outstanding -= amount
    return credits - outstanding


@transaction.atomic
def credit(
    *,
    user,
    credits: int,
    type: str,
    source: str,
    description: Optional[str] = None,
    amount_cents: Optional[int] = None,
    metadata: Optional[dict] = None,
    expires_at=None,
    idempotency_key: Optional[str] = None,
    settle_debts: bool = True,
) -> Transaction:
    """Add credits to a user's wallet as one lot, plus its ledger row.

    *source* is required (``LotSource``): a lot that cannot say where it came
    from cannot be reasoned about later — "which of these expire, and which
    did the customer pay cash for" is exactly the question a single balance
    could not answer, so the answer is not allowed to default.

    *expires_at* is the moment the lot dies (``None`` = never). Pass the
    subscription's ``current_period_end`` for plan bundles; leave it None for
    purchased packages.

    When *idempotency_key* is given, a duplicate call (same key, same
    wallet) short-circuits and returns the original transaction without
    granting again — safe under at-least-once service-to-service retries.
    The key is stored on the indexed ``Transaction.idempotency_key`` column
    and, for compatibility with readers written against 0.10.0, mirrored
    into ``Transaction.metadata["idempotency_key"]``.

    Credits landing on a wallet that owes credits are collected against the
    outstanding :class:`~stapel_billing.models.CreditDebt` rows first,
    oldest debt first, inside the same lock (*settle_debts*, on by
    default): a top-up on an indebted wallet is repayment before it is
    spending money.
    """
    if credits <= 0:
        raise ValueError("credits must be positive")
    now = timezone.now()
    if expires_at is not None and expires_at <= now:
        # Granting credits that are already dead is a caller bug, and a
        # silently-zero grant is the worst way to find out about it.
        raise ValueError("expires_at must be in the future")
    wallet = _lock_wallet(user)
    if idempotency_key:
        # The wallet row is locked above, so concurrent duplicates
        # serialize here and the second caller sees the first's row.
        existing = _transaction_by_idempotency_key(wallet, idempotency_key)
        if existing is not None:
            logger.info(
                "credit short-circuited by idempotency_key=%s (txn %s)",
                idempotency_key,
                existing.id,
            )
            return existing
    _expire_due_lots(wallet, now=now)
    lot = CreditLot.objects.create(
        wallet=wallet,
        source=source,
        credits_initial=credits,
        credits_remaining=credits,
        expires_at=expires_at,
    )
    balance = _sync_balance(wallet, now=now)
    metadata = dict(metadata or {})
    if idempotency_key:
        metadata["idempotency_key"] = idempotency_key
    txn = Transaction.objects.create(
        wallet=wallet,
        lot=lot,
        type=type,
        amount_cents=amount_cents,
        credits_delta=credits,
        balance_after=balance,
        description=description,
        metadata=metadata,
        idempotency_key=idempotency_key or "",
    )
    lot.granting_transaction = txn
    lot.save(update_fields=["granting_transaction"])
    if settle_debts:
        # After the grant row, so the ledger reads in the order it happened:
        # the credits arrived, then the debt took its share of them.
        _settle_debts(wallet, now=now)
    return txn


@transaction.atomic
def debit(
    *,
    user,
    credits: int,
    type: str,
    description: Optional[str] = None,
    metadata: Optional[dict] = None,
    idempotency_key: Optional[str] = None,
    allow_partial: bool = False,
):
    """Deduct credits from a user's wallet.  Raises InsufficientCreditsError.

    Spends the lots expiring-soonest first, so credits die used rather than
    unused. The per-lot split is recorded on
    ``Transaction.metadata["lots"]``; ``Transaction.lot`` names the lot
    directly when the debit came out of exactly one.

    When *idempotency_key* is given, a duplicate call (same key, same
    wallet) short-circuits and returns the original transaction without
    debiting again — safe under at-least-once service-to-service retries.
    The key is stored on the indexed ``Transaction.idempotency_key`` column
    and mirrored into ``Transaction.metadata["idempotency_key"]``.

    *allow_partial* changes the shape of the answer, and only when asked:

    * ``False`` (the default, unchanged): all-or-nothing. The wallet either
      covers the whole charge or ``InsufficientCreditsError`` is raised and
      nothing moves. Returns the :class:`Transaction`.
    * ``True``: charge what the wallet has, record the rest as a
      :class:`~stapel_billing.models.CreditDebt`, and return a
      :class:`DebitResult` describing both halves. For the consumer that
      has already done the work and is choosing between "serve it free and
      forget" and "serve it and remember what is owed" — the first is what
      an all-or-nothing debit forced, and free service that nothing records
      is indistinguishable from a bug.

    A partial debit always writes a ledger row, even when the wallet had
    nothing at all (``credits_delta = 0``): the charge happened, the ledger
    is where charges are, and a debt with no transaction pointing at it
    would be a number nobody can trace back to the work that caused it.
    """
    if credits <= 0:
        raise ValueError("credits must be positive")
    now = timezone.now()
    wallet = _lock_wallet(user)
    if idempotency_key:
        # The wallet row is locked above, so concurrent duplicates
        # serialize here and the second caller sees the first's row.
        existing = _transaction_by_idempotency_key(wallet, idempotency_key)
        if existing is not None:
            logger.info(
                "debit short-circuited by idempotency_key=%s (txn %s)",
                idempotency_key,
                existing.id,
            )
            return _replay_debit(wallet, existing, credits) if allow_partial else existing
    _expire_due_lots(wallet, now=now)
    if allow_partial:
        chargeable = min(credits, _live_balance(wallet, now=now))
    else:
        chargeable = credits
    taken = _consume_lots(wallet, chargeable, now=now) if chargeable else []
    balance = _sync_balance(wallet, now=now)
    metadata = dict(metadata or {})
    if idempotency_key:
        metadata["idempotency_key"] = idempotency_key
    metadata["lots"] = _consumption_metadata(taken)
    shortfall = credits - chargeable
    debt = None
    if shortfall:
        metadata["partial"] = True
        metadata["requested_credits"] = credits
        metadata["shortfall"] = shortfall
    txn = Transaction.objects.create(
        wallet=wallet,
        lot=_single_lot(taken),
        type=type,
        amount_cents=None,
        credits_delta=-chargeable,
        balance_after=balance,
        description=description,
        metadata=metadata,
        idempotency_key=idempotency_key or "",
    )
    if shortfall:
        debt = _open_debt(
            wallet,
            credits=shortfall,
            reason=DebtReason.PARTIAL_DEBIT,
            type=type,
            description=description,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
        debt.transaction = txn
        debt.save(update_fields=["transaction"])
        txn.metadata["debt_id"] = str(debt.id)
        txn.save(update_fields=["metadata"])
    if not allow_partial:
        return txn
    return DebitResult(
        transaction=txn,
        requested=credits,
        debited=chargeable,
        shortfall=shortfall,
        balance=balance,
        debt=debt,
    )


def _replay_debit(wallet, txn: Transaction, requested: int) -> DebitResult:
    """Rebuild the :class:`DebitResult` a short-circuited retry already got.

    The retry must be told the same thing the first call was told —
    including that part of it went on the slate — or a consumer that
    retries will report a shortfall it never saw and bill it twice.
    """
    debited = -txn.credits_delta
    metadata = txn.metadata or {}
    original = metadata.get("requested_credits")
    if not isinstance(original, int) or isinstance(original, bool):
        original = requested if requested is not None else debited
    debt = CreditDebt.objects.filter(transaction=txn).first()
    return DebitResult(
        transaction=txn,
        requested=original,
        debited=debited,
        shortfall=max(original - debited, 0),
        balance=txn.balance_after,
        debt=debt,
    )


def can_afford(*, user=None, wallet=None, credits: int) -> Affordability:
    """Would a debit or hold of *credits* go through right now? Read-only.

    No lock, no row written, nothing reserved — the pre-flight consumers
    were previously forced to fake by taking a real :func:`hold` and
    releasing it. That probe was not free: every probe fragmented the lots
    (a release returns credits as a NEW ``hold_release`` lot), so a service
    that checked affordability on each request grew its customers' lot
    tables without bound and slowed down every subsequent spend.

    Answers from the lots rather than ``Wallet.balance``: the lots are what
    a debit will actually walk, and lots whose deadline has passed but
    which the expiry sweep has not reached yet still count in the cached
    balance. This is the same "available" number :func:`debit` measures its
    refusal against.

    Concurrency is the caller's to reason about: two callers can both be
    told yes. That is inherent in a question asked without a lock — the
    caller that needs the answer to stay true takes a hold.
    """
    if credits <= 0:
        raise ValueError("credits must be positive")
    if wallet is None:
        if user is None:
            raise ValueError("can_afford needs either user or wallet")
        # Deliberately not get_or_create: a read must not write.
        wallet = Wallet.objects.filter(user=user).first()
    balance = _live_balance(wallet, now=timezone.now()) if wallet is not None else 0
    return Affordability(
        affordable=balance >= credits,
        balance=balance,
        requested=credits,
        shortfall=max(credits - balance, 0),
    )


# ─── Reservations (hold / capture / release) ────────────────


@transaction.atomic
def hold(
    *,
    user,
    credits: int,
    type: str,
    idempotency_key: str,
    description: Optional[str] = None,
    metadata: Optional[dict] = None,
    expires_at=None,
) -> CreditHold:
    """Reserve credits before doing work whose real cost is not known yet.

    The credits leave the lots immediately — a reservation that only existed
    as an aggregate ("balance minus open holds") would be recomputed by every
    caller and defended by none. No ledger row is written: nothing has been
    billed yet. :func:`capture` writes the charge, :func:`release` gives the
    credits back.

    *idempotency_key* is required and unique per wallet, so a retried request
    reserves once. *expires_at* defaults to
    ``STAPEL_BILLING["HOLD_DEFAULT_TTL_SECONDS"]`` from now — a pipeline that
    dies between hold and answer must not lock a customer's credits forever.

    The short-circuit only covers a hold that is still ``held``. A key
    whose hold has been captured, released or expired raises
    :class:`HoldKeyResolvedError`: that hold reserves nothing, so returning
    it would answer "reserved" with an empty reservation and push the
    failure to the capture — after the work was done. See the exception's
    docstring for what a caller does with it.
    """
    if credits <= 0:
        raise ValueError("credits must be positive")
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    now = timezone.now()
    wallet = _lock_wallet(user)
    existing = CreditHold.objects.filter(
        wallet=wallet, idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        if existing.status != HoldStatus.HELD:
            logger.warning(
                "hold idempotency_key=%s belongs to hold %s, which is %s — "
                "nothing is reserved under that key",
                idempotency_key,
                existing.id,
                existing.status,
            )
            raise HoldKeyResolvedError(existing)
        logger.info(
            "hold short-circuited by idempotency_key=%s (hold %s)",
            idempotency_key,
            existing.id,
        )
        return existing
    if expires_at is None:
        from .conf import hold_default_ttl_seconds

        ttl = hold_default_ttl_seconds()
        expires_at = now + timedelta(seconds=ttl) if ttl else None
    _expire_due_lots(wallet, now=now)
    taken = _consume_lots(wallet, credits, now=now)
    held = CreditHold.objects.create(
        wallet=wallet,
        credits=credits,
        type=type,
        description=description,
        metadata=metadata or {},
        idempotency_key=idempotency_key,
        status=HoldStatus.HELD,
        expires_at=expires_at,
    )
    HoldAllocation.objects.bulk_create(
        [
            HoldAllocation(hold=held, lot=lot, credits=amount)
            for lot, amount in taken
        ]
    )
    _sync_balance(wallet, now=now)
    return held


def _load_hold_for_update(hold_id) -> tuple[CreditHold, Wallet]:
    """Lock the wallet, then re-read the hold under that lock.

    The wallet row is the module's single serialisation point; reading the
    hold's status before taking it would let two capture deliveries both see
    ``held``.
    """
    held = CreditHold.objects.filter(pk=hold_id).first()
    if held is None:
        raise HoldNotFoundError(f"no credit hold {hold_id}")
    wallet = Wallet.objects.select_for_update().get(pk=held.wallet_id)
    held.refresh_from_db()
    return held, wallet


def _settle(held: CreditHold, status: str, *, now) -> None:
    held.status = status
    held.resolved_at = now
    held.save(update_fields=["status", "resolved_at"])


@transaction.atomic
def capture(
    *,
    hold_id,
    actual_credits: Optional[int] = None,
    description: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Transaction:
    """Bill a hold for what the work actually cost, and write the ledger row.

    *actual_credits* defaults to the full reserved amount. Less than that
    refunds the difference lot-by-lot with the original expiry intact; more
    takes the extra out of the live lots and may raise
    :class:`InsufficientCreditsError` — the caller decides whether to cap at
    the reserved amount instead.

    Idempotent through the hold: a second capture of an already-captured hold
    returns the charge that was written the first time.
    """
    now = timezone.now()
    held, wallet = _load_hold_for_update(hold_id)
    if held.status == HoldStatus.CAPTURED:
        txn = Transaction.objects.filter(
            wallet=wallet, metadata__hold_id=str(held.id)
        ).first()
        if txn is not None:
            logger.info("capture short-circuited: hold %s already captured", held.id)
            return txn
        raise HoldStateError(
            f"hold {held.id} is captured but its charge row is missing"
        )
    if held.status != HoldStatus.HELD:
        raise HoldStateError(f"hold {held.id} is {held.status}, not held")
    actual = held.credits if actual_credits is None else int(actual_credits)
    if actual < 0:
        raise ValueError("actual_credits must not be negative")
    _expire_due_lots(wallet, now=now)
    extra_taken: list[tuple[CreditLot, int]] = []
    if actual > held.credits:
        extra_taken = _consume_lots(wallet, actual - held.credits, now=now)
        HoldAllocation.objects.bulk_create(
            [
                HoldAllocation(hold=held, lot=lot, credits=amount)
                for lot, amount in extra_taken
            ]
        )
    elif actual < held.credits:
        _refund_allocations(held, held.credits - actual)
        # A refund can resurrect a lot whose deadline passed while the work
        # ran; the sweep turns it straight back into an EXPIRATION row.
        _expire_due_lots(wallet, now=now)
    balance = _sync_balance(wallet, now=now)
    billed = list(held.allocations.select_related("lot").order_by("created_at", "id"))
    charged = [(alloc.lot, alloc.credits) for alloc in billed if alloc.credits > 0]
    txn_metadata = dict(held.metadata or {})
    txn_metadata.update(metadata or {})
    txn_metadata["hold_id"] = str(held.id)
    txn_metadata["lots"] = _consumption_metadata(charged)
    txn = Transaction.objects.create(
        wallet=wallet,
        lot=_single_lot(charged),
        type=held.type,
        amount_cents=None,
        credits_delta=-actual,
        balance_after=balance,
        description=description or held.description,
        metadata=txn_metadata,
    )
    _settle(held, HoldStatus.CAPTURED, now=now)
    return txn


@transaction.atomic
def release(*, hold_id, expired: bool = False) -> CreditHold:
    """Give a hold's credits back, with the expiry they were taken with.

    Idempotent: releasing a hold that is already captured, released or
    expired is a no-op returning the hold as it stands — a retried release
    must not conjure credits.

    *expired* only changes the recorded outcome (``expired`` instead of
    ``released``); it is what the crash-safety sweep passes so that "the
    caller gave the credits back" and "nobody ever came back for them" stay
    distinguishable.
    """
    now = timezone.now()
    held, wallet = _load_hold_for_update(hold_id)
    if held.status != HoldStatus.HELD:
        logger.info("release is a no-op: hold %s is %s", held.id, held.status)
        return held
    _expire_due_lots(wallet, now=now)
    outstanding = sum(alloc.credits for alloc in held.allocations.all())
    _refund_allocations(held, outstanding)
    # Same reason as in capture(): credits can come back into a lot whose
    # deadline has already passed, and they must die rather than be renewed.
    _expire_due_lots(wallet, now=now)
    _sync_balance(wallet, now=now)
    _settle(held, HoldStatus.EXPIRED if expired else HoldStatus.RELEASED, now=now)
    return held


# ─── Account merge ──────────────────────────────────────────
#
# A merge is the opposite of an erasure: nothing goes away, the rows change
# owner. It lives here rather than in ``actions.py`` for the same reason
# ``erase_subject`` lives in ``gdpr.py`` — a balance is written only through
# this module, and a handler that moved credits on its own would be a second
# implementation of the wallet's invariants, free to drift from this one.


def merge_idempotency_key(from_user_id, into_user_id) -> str:
    """The deterministic ledger key for one merge of one pair of accounts.

    Derived from the pair rather than minted, so a redelivery of an
    at-least-once event computes the SAME key, finds the row the first
    delivery wrote and credits nothing — the one guard that makes the
    transfer provably once. Same idea as the erasure receipt id in
    :mod:`stapel_billing.actions`.

    Digested rather than concatenated because the value is stored: a
    readable ``merge:<from>:<into>`` would make ``idempotency_key`` a second
    copy of two account ids in a column erasure does not scrub, next to a
    ``user_id`` this package goes to some trouble to pseudonymize.
    """
    import hashlib

    digest = hashlib.sha256(
        f"{from_user_id}:{into_user_id}".encode()
    ).hexdigest()[:32]
    return f"merge:{digest}"


@transaction.atomic
def merge_wallets(*, from_user_id, into_user_id) -> dict[str, int]:
    """Fold one account's wallet into another's — the credit half of a merge.

    Auth's ``user.merged`` says ``from_user_id`` ceased to exist and every
    row that named it belongs to ``into_user_id`` now. Here that means:

    * the merged wallet's **lots move**, and with them the balance. Lots are
      moved rather than summed into a number because a lot is where the
      credits came from and when they die — a subscription bundle that was
      about to expire must not become non-expiring cash by changing owner,
      which is exactly what adding two integers would do;
    * transactions, holds and debts move with them, so the survivor's ledger
      explains the survivor's balance without a gap;
    * the survivor gets one **explicit ledger row** for the change
      (``TransactionType.ADJUSTMENT``). The balance is never written
      silently: a wallet that gained credits with no row saying why is a
      support answer nobody can give;
    * the merged wallet is closed at zero and stamped ``merged_into``, so a
      redelivery cannot find it and credit a second time.

    Returns the counts of what moved, so a redelivery's zeroes are visible
    rather than assumed. An account this module holds no wallet for is not
    an error: it moves nothing and says so.

    The survivor's wallet is created if it does not exist. That is a foreign
    key to a user this service must already know about — ``into_user_id`` is
    announced by its own projection event ahead of the merge — and if it
    does not yet, the resulting ``IntegrityError`` is deliberately NOT
    caught: the event is not poison, it is early, and redelivery is what
    at-least-once delivery is for.
    """
    counts = {
        "wallets": 0,
        "credits": 0,
        "credit_lots": 0,
        "transactions": 0,
        "credit_holds": 0,
        "credit_debts": 0,
        "subscriptions": 0,
    }
    key = merge_idempotency_key(from_user_id, into_user_id)
    if Transaction.objects.filter(idempotency_key=key).exists():
        # This merge already wrote its ledger row. Nothing below may run
        # again — the credit is the part that must happen exactly once.
        logger.info("wallet merge %s already applied — nothing to do", key)
        return counts

    merged = Wallet.objects.filter(user_id=from_user_id).first()
    if merged is None:
        counts["subscriptions"] = _merge_subscription(from_user_id, into_user_id)
        return counts

    survivor, _ = Wallet.objects.get_or_create(
        user_id=into_user_id, defaults={"currency": merged.currency}
    )
    # Locked in primary-key order, not in "merged then survivor" order: two
    # guests merging into one account concurrently would otherwise take the
    # same two rows in opposite orders and deadlock.
    locked = {
        w.pk: w
        for w in Wallet.objects.select_for_update()
        .filter(pk__in=[merged.pk, survivor.pk])
        .order_by("pk")
    }
    merged = locked[merged.pk]
    survivor = locked[survivor.pk]

    now = timezone.now()
    # Before counting: credits that died of old age must not travel as live
    # ones, and the EXPIRATION rows belong on the wallet that held them.
    _expire_due_lots(merged, now=now)
    moved_credits = _live_balance(merged, now=now)

    # A hold's idempotency key is unique per wallet, so two accounts that
    # happened to receive the same caller key cannot land on one wallet.
    # Vanishingly rare, and an IntegrityError here would be a poison
    # message: rename the arriving row instead of failing the merge.
    taken = set(
        CreditHold.objects.filter(wallet=survivor).values_list(
            "idempotency_key", flat=True
        )
    )
    if taken:
        for clash in CreditHold.objects.filter(
            wallet=merged, idempotency_key__in=taken
        ):
            clash.idempotency_key = f"{clash.idempotency_key}:merged:{merged.pk}"
            clash.save(update_fields=["idempotency_key"])

    counts["credit_lots"] = CreditLot.objects.filter(wallet=merged).update(
        wallet=survivor
    )
    counts["transactions"] = Transaction.objects.filter(wallet=merged).update(
        wallet=survivor
    )
    counts["credit_holds"] = CreditHold.objects.filter(wallet=merged).update(
        wallet=survivor
    )
    counts["credit_debts"] = CreditDebt.objects.filter(wallet=merged).update(
        wallet=survivor
    )

    merged.balance = 0
    merged.user = None
    merged.merged_into = survivor
    merged.save(update_fields=["balance", "user", "merged_into", "updated_at"])
    counts["wallets"] = 1
    counts["credits"] = moved_credits

    # Derived from the lots that just arrived, not incremented — the same
    # rule every other operation here follows. Expiry first, as everywhere
    # else: a ``balance_after`` counting credits the customer can no longer
    # spend is a ledger row that lies.
    _expire_due_lots(survivor, now=now)
    balance = _sync_balance(survivor, now=now)
    Transaction.objects.create(
        wallet=survivor,
        type=TransactionType.ADJUSTMENT,
        credits_delta=moved_credits,
        balance_after=balance,
        description="Credits merged from a folded account",
        metadata={"merge": True, "merged_wallet_id": str(merged.pk),
                  "idempotency_key": key},
        idempotency_key=key,
    )
    counts["subscriptions"] = _merge_subscription(from_user_id, into_user_id)
    # After the merge row, so the ledger reads in the order it happened: the
    # credits arrived, then any debt the survivor carries took its share.
    _settle_debts(survivor, now=now)
    return counts


def _merge_subscription(from_user_id, into_user_id) -> int:
    """Move or detach the merged account's subscription. Returns rows touched.

    ``Subscription`` is one per user, so the survivor's plan wins whenever
    it has one: the merged row is detached rather than cancelled, because
    cancelling reaches into Stripe and a merge is not a decision about
    anybody's billing. When the survivor has NO subscription the row moves
    to it whole — a plan someone is paying for must not evaporate because
    they signed in.
    """
    merged = Subscription.objects.filter(user_id=from_user_id).first()
    if merged is None:
        return 0
    if Subscription.objects.filter(user_id=into_user_id).exists():
        merged.user = None
    else:
        merged.user_id = into_user_id
    merged.save(update_fields=["user", "updated_at"])
    return 1


# ─── Plan bundles granted without a payment provider ────────
#
# Every credit this module granted before 0.11.0 came out of a Stripe
# webhook. A plan whose bundle is not sold through Stripe — the free tier,
# an invoiced enterprise agreement, a plan a host assigns from its own
# admin — therefore never granted anything at all: its
# ``monthly_credits_included`` was a number in a catalogue that no code
# path read. The grant below is that missing path.


#: How the default period key is spelled. Monthly, because
#: ``monthly_credits_included`` is monthly; a host on another cadence
#: passes its own *period_key* and *expires_at*.
_PERIOD_KEY_FORMAT = "%Y-%m"


def current_period_key(now=None) -> str:
    """The key identifying the current bundle period (``"2026-08"``).

    The period key is the idempotency unit: one bundle per (wallet, plan,
    period), whatever the sweep's cadence or how many times it runs.
    """
    return (now or timezone.now()).strftime(_PERIOD_KEY_FORMAT)


def _month_bounds(period_key: str):
    """(start, end) of a ``YYYY-MM`` key, or (None, None) if it is not one.

    A host with weekly or contract-shaped periods passes its own key and
    its own ``expires_at``; guessing an end for a key we cannot parse would
    be a fabricated deadline on real credits.
    """
    from datetime import datetime, timezone as _tz

    try:
        start = datetime.strptime(period_key, _PERIOD_KEY_FORMAT).replace(tzinfo=_tz.utc)
    except (TypeError, ValueError):
        return None, None
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


@transaction.atomic
def grant_plan_bundle(
    *,
    user=None,
    user_id=None,
    wallet=None,
    plan_slug: str,
    period_key: Optional[str] = None,
    credits: Optional[int] = None,
    expires_at=None,
    type: str = TransactionType.SUBSCRIPTION_BONUS,
    source: str = LotSource.SUBSCRIPTION,
    description: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Optional[Transaction]:
    """Grant one period's plan bundle to a wallet, without a payment provider.

    Idempotent per ``(wallet, plan, period)`` — two sweeps in one month,
    an operator re-running the job by hand and a host that also grants at
    signup all produce one bundle. The claim goes through the same unique
    row as the Stripe grants (``ProviderGrant``, ``provider="local"``), so
    the deduplication is a database constraint rather than a read-then-write
    guard two workers can both pass.

    The lot EXPIRES: a bundle that belongs to a period must die with it, or
    a free tier quietly becomes a growing balance nobody sold. *expires_at*
    defaults to the end of the ``YYYY-MM`` period the key names.

    Returns the grant's :class:`~stapel_billing.models.Transaction`, or
    ``None`` when there was nothing to grant — the plan bundles no credits,
    the period is already granted, or the period is in the past.
    """
    entry = PLANS_BY_SLUG.get(plan_slug)
    if credits is None:
        if entry is None:
            logger.error(
                "grant_plan_bundle: plan %r is not in STAPEL_BILLING['PLANS'] "
                "and no explicit credits were given — nothing granted",
                plan_slug,
            )
            return None
        credits = entry.monthly_credits_included
    if not credits or credits <= 0:
        return None
    if user is None:
        if user_id is None and wallet is not None:
            user_id = wallet.user_id
        if user_id is None:
            raise ValueError("grant_plan_bundle needs user, user_id or wallet")
        user = _get_user_model().objects.filter(pk=user_id).first()
        if user is None:
            logger.warning("grant_plan_bundle: no user %s", user_id)
            return None
    period_key = period_key or current_period_key()
    if expires_at is None:
        _, expires_at = _month_bounds(period_key)
        if expires_at is None:
            logger.error(
                "grant_plan_bundle: period_key %r is not YYYY-MM and no "
                "expires_at was given — refusing to grant credits with a "
                "deadline nobody set",
                period_key,
            )
            return None
    if expires_at <= timezone.now():
        logger.info(
            "grant_plan_bundle: period %s of plan %s is already over — "
            "nothing granted",
            period_key,
            plan_slug,
        )
        return None
    if wallet is None:
        wallet = get_or_create_wallet(user)
    # The claim is per WALLET, not per user: the wallet is what the credits
    # land in, and it is the identity that survives the owner's erasure.
    if not claim_provider_object(
        scope=ProviderGrant.SCOPE_PLAN_BUNDLE,
        external_id=f"{wallet.id}:{plan_slug}:{period_key}",
        provider="local",
    ):
        return None
    return credit(
        user=user,
        credits=credits,
        type=type,
        source=source,
        expires_at=expires_at,
        description=description or f"Plan bundle: {plan_slug} ({period_key})",
        metadata={
            **(metadata or {}),
            "plan": plan_slug,
            "period_key": period_key,
            "grant": "plan_bundle",
        },
        idempotency_key=f"plan-bundle:{plan_slug}:{period_key}",
    )


#: Statuses under which a local Subscription row speaks for its plan —
#: the same grace window the entitlement check uses.
_GRANTING_SUBSCRIPTION_STATUSES = frozenset(
    {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.TRIALING,
        SubscriptionStatus.PAST_DUE,
    }
)

#: How many wallets the default resolver holds in memory at a time.
_ENTITLEMENT_CHUNK = 500


def default_plan_bundle_entitlements() -> Iterable[dict]:
    """Who gets a non-provider plan bundle — the default the seam falls back to.

    Walks the wallets this deployment already has and yields the ones whose
    effective plan bundles credits that nothing else grants:

    * a wallet whose owner has a live ``Subscription`` row → that row's
      plan, UNLESS the row carries a ``stripe_subscription_id``, in which
      case the provider's invoices grant the bundle and granting again here
      would double it;
    * every other wallet → the ``Subscription.plan`` field default (the
      free tier on a stock deployment), which is exactly the population
      whose ``monthly_credits_included`` was dead code.

    Wallets only: a user with no wallet row has never touched billing, and
    walking the whole user table is not something a library gets to decide
    for a host. A host that wants users pre-granted at signup calls
    :func:`grant_plan_bundle` there, or replaces this resolver through
    ``STAPEL_BILLING["PLAN_BUNDLE_ENTITLEMENTS"]`` — plan membership is
    host knowledge, and this default is a guess that happens to be right
    for hosts that keep the plan on the local Subscription row.
    """
    from itertools import islice

    default_slug = Subscription._meta.get_field("plan").get_default()
    rows = (
        Wallet.objects.exclude(user=None)
        .order_by("created_at", "id")
        .values_list("id", "user_id")
        .iterator(chunk_size=_ENTITLEMENT_CHUNK)
    )
    while True:
        chunk = list(islice(rows, _ENTITLEMENT_CHUNK))
        if not chunk:
            return
        subs = {
            sub.user_id: sub
            for sub in Subscription.objects.filter(
                user_id__in=[user_id for _, user_id in chunk]
            ).only("user_id", "plan", "status", "stripe_subscription_id")
        }
        for wallet_id, user_id in chunk:
            sub = subs.get(user_id)
            if sub is not None and sub.status in _GRANTING_SUBSCRIPTION_STATUSES:
                if sub.stripe_subscription_id:
                    continue  # the provider's invoices already grant this one
                slug = sub.plan
            else:
                slug = default_slug
            entry = PLANS_BY_SLUG.get(slug)
            if entry is None or entry.monthly_credits_included <= 0:
                continue
            yield {"wallet_id": wallet_id, "user_id": user_id, "plan": slug}


def plan_bundle_entitlements() -> Iterable[dict]:
    """The configured "who is entitled" resolver, normalised.

    The seam is ``STAPEL_BILLING["PLAN_BUNDLE_ENTITLEMENTS"]`` (a dotted
    path, resolved through import_strings). It yields either mappings —
    ``{"user_id": ..., "plan": ..., optionally "wallet_id", "credits",
    "period_key", "expires_at"}`` — or ``(user_id, plan_slug)`` pairs.
    Anything else is skipped with a log line rather than crashing the
    sweep: one malformed row must not stop the month's grants.
    """
    from .conf import billing_settings

    resolver = billing_settings.PLAN_BUNDLE_ENTITLEMENTS
    if not callable(resolver):
        logger.error(
            "STAPEL_BILLING['PLAN_BUNDLE_ENTITLEMENTS'] is %r, which is not "
            "callable — no plan bundles will be granted",
            resolver,
        )
        return
    for row in resolver():
        if isinstance(row, dict):
            if row.get("user_id") is None and row.get("wallet_id") is None:
                logger.error("plan bundle entitlement without a subject: %r", row)
                continue
            if not row.get("plan"):
                logger.error("plan bundle entitlement without a plan: %r", row)
                continue
            yield dict(row)
            continue
        try:
            user_id, plan_slug = row
        except (TypeError, ValueError):
            logger.error("unusable plan bundle entitlement row: %r", row)
            continue
        yield {"user_id": user_id, "plan": plan_slug}


def reconcile_wallet_balances() -> list[dict]:
    """Report wallets whose cached balance disagrees with their lots.

    The cache is maintained under the wallet's row lock by every operation
    here, so a disagreement means something wrote a balance outside this
    module or the expiry worker is not running. Reported, never
    silently corrected: a cache quietly rewritten to match hides both.
    """
    now = timezone.now()
    drifted = (
        Wallet.objects.annotate(
            lot_total=Coalesce(
                Sum("lots__credits_remaining", filter=_live_lot_filter_for_join(now)),
                Value(0),
            )
        )
        .exclude(lot_total=F("balance"))
        .values("id", "balance", "lot_total")
    )
    report = []
    for row in drifted:
        logger.error(
            "wallet %s balance cache says %s, its live lots sum to %s — "
            "something wrote the balance outside stapel_billing.services, or "
            "expire_credit_lots is not scheduled (see stapel_billing.W105)",
            row["id"],
            row["balance"],
            row["lot_total"],
        )
        report.append(
            {
                "wallet_id": str(row["id"]),
                "cached_balance": row["balance"],
                "lot_balance": row["lot_total"],
            }
        )
    return report


def _live_lot_filter_for_join(now) -> Q:
    """:func:`_live_lot_filter` spelled across the ``lots`` reverse relation."""
    return Q(lots__credits_remaining__gt=0) & (
        Q(lots__expires_at__isnull=True) | Q(lots__expires_at__gt=now)
    )


# ─── Payment provider ───────────────────────────────────────


def get_provider() -> PaymentProvider:
    """Instantiate the configured PaymentProvider (Stripe by default)."""
    from .conf import billing_settings

    cls = billing_settings.PAYMENT_PROVIDER
    if not (isinstance(cls, type) and issubclass(cls, PaymentProvider)):
        raise TypeError(
            "STAPEL_BILLING['PAYMENT_PROVIDER'] must point to a "
            f"PaymentProvider subclass, got {cls!r}"
        )
    return cls()


def create_checkout_session(
    *, user, package: Optional[str], plan: Optional[str], success_url: str, cancel_url: str
):
    """Create a checkout session for either a one-off package or a plan.

    Returns a (checkout_url, session_id) tuple.  Delegates to the
    configured payment provider.
    """
    return get_provider().create_checkout_session(
        user=user,
        package=package,
        plan=plan,
        success_url=success_url,
        cancel_url=cancel_url,
    )


def create_customer_portal(*, customer_id: str, return_url: str) -> str:
    return get_provider().create_portal_session(
        customer_id=customer_id, return_url=return_url
    )


def cancel_provider_subscription(subscription_id: str) -> None:
    """Cancel the provider-side subscription. Raises on remote failure."""
    get_provider().cancel_subscription(subscription_id)


def verify_stripe_signature(payload: bytes, sig_header: str) -> dict:
    """Parse + verify a provider webhook payload.  Returns the event dict."""
    return get_provider().verify_webhook(payload, sig_header)


# ─── Milestones (comm actions + Django signals) ─────────────


def _epoch_to_datetime(ts):
    """Convert a Stripe unix timestamp to an aware UTC datetime, or None.

    Stripe encodes period boundaries as integer unix seconds. Anything that
    is not a real number (missing key, ``None``) yields ``None`` — the honest
    "unknown" rather than a fabricated epoch-zero timestamp.
    """
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    from datetime import datetime, timezone as _tz

    return datetime.fromtimestamp(ts, tz=_tz.utc)


def _future_or_none(value):
    """A deadline only counts while it is still ahead.

    A period end that has already passed dates nothing: it would either kill
    the bundle the customer just paid for, or (if forced through) be a
    fabricated deadline. Undated is the honest state — the next subscription
    event carries the real period and :func:`_stamp_subscription_period`
    stamps it on.
    """
    if value is None:
        return None
    return value if value > timezone.now() else None


def _invoice_period_end(obj: dict):
    """The service period a renewal invoice covers, as a datetime or None.

    Read from the invoice's own line item first: at the moment
    ``invoice.paid`` fires, the local Subscription row still carries the
    period that just ENDED — Stripe's ``customer.subscription.updated`` has
    not necessarily landed yet — so trusting the stored period would expire
    the renewal's credits the instant they were granted.
    """
    lines = ((obj.get("lines") or {}).get("data") or [])
    for line in lines:
        end = _epoch_to_datetime((line.get("period") or {}).get("end"))
        if end is not None:
            return end
    return _epoch_to_datetime(obj.get("period_end"))


def _stamp_subscription_period(sub: Subscription) -> int:
    """Date the subscription lots that were granted before the period was known.

    ``checkout.session.completed`` grants the first bundle from a session
    payload that carries no billing period, so that lot starts undated. The
    very next event (``customer.subscription.created/updated``) does carry
    it. Without this, every subscriber's first month is a bundle that never
    expires — the exact leak the two-pool design exists to prevent.
    """
    if not sub.current_period_end or not sub.stripe_subscription_id:
        return 0
    return CreditLot.objects.filter(
        wallet__user_id=sub.user_id,
        source=LotSource.SUBSCRIPTION,
        expires_at__isnull=True,
        credits_remaining__gt=0,
        granting_transaction__metadata__stripe_subscription_id=(
            sub.stripe_subscription_id
        ),
    ).update(expires_at=sub.current_period_end)


def _apply_stripe_period(sub: Subscription, obj: dict) -> list[str]:
    """Populate ``current_period_{start,end}`` on *sub* from a Stripe
    subscription object. Returns the names of the fields that actually
    changed, for ``save(update_fields=...)``.

    Only overwrites a field when the source carries a real timestamp — a
    missing period leaves the prior value intact rather than nulling it.
    """
    changed: list[str] = []
    start = _epoch_to_datetime(obj.get("current_period_start"))
    if start is not None and start != sub.current_period_start:
        sub.current_period_start = start
        changed.append("current_period_start")
    end = _epoch_to_datetime(obj.get("current_period_end"))
    if end is not None and end != sub.current_period_end:
        sub.current_period_end = end
        changed.append("current_period_end")
    return changed


def _announce_payment(*, user, txn: Transaction, amount_cents: int, currency: str, **extra) -> None:
    """Emit payment.completed + send the payment_completed signal.

    Called inside the webhook's atomic block: the comm event goes through
    the transactional outbox, so it leaves iff the transaction commits.
    """
    emit(
        "payment.completed",
        {
            "user_id": str(user.id),
            "amount_cents": int(amount_cents or 0),
            "currency": currency,
            "transaction_id": str(txn.id),
            "created_at": txn.created_at.isoformat(),
            **extra,
        },
        key=str(user.id),
    )
    payment_completed.send(
        sender=Transaction, user=user, credits=txn.credits_delta, transaction=txn
    )


def _announce_subscription(sub: Subscription) -> None:
    """Emit subscription.changed + send the subscription_changed signal.

    ``current_period_end`` carries the real renewal/expiry moment whenever it
    is known — the provider webhooks that hand us a subscription object
    (``customer.subscription.created/updated/deleted``) populate it via
    :func:`_apply_stripe_period`. It stays ``null`` only at the
    ``checkout.session.completed`` milestone, whose session payload does not
    yet carry a billing period; the immediately-following
    ``customer.subscription.created`` event fills it in. ``null`` therefore
    means "not yet known", never a fabricated zero timestamp.
    """
    emit(
        "subscription.changed",
        {
            "user_id": str(sub.user_id),
            "plan": sub.plan,
            "status": sub.status,
            "current_period_end": sub.current_period_end.isoformat()
            if sub.current_period_end
            else None,
        },
        key=str(sub.user_id),
    )
    subscription_changed.send(sender=Subscription, subscription=sub)


# ─── Grant guards ──────────────────────────────────────────


#: Checkout payment states in which the money is actually settled. Stripe
#: also completes a session whose asynchronous payment method has not
#: cleared (``unpaid``) — granting on that is granting on a promise.
_SETTLED_PAYMENT_STATUSES = frozenset({"paid", "no_payment_required"})


def claim_provider_object(*, scope: str, external_id, provider: str = "stripe") -> bool:
    """Claim one provider object for a grant. False = someone got there first.

    Runs in its own savepoint so the loser of the race can return quietly
    instead of poisoning the caller's transaction with a failed insert.
    Callers must invoke this inside the transaction that performs the
    grant, so the claim and the credit commit or roll back together.
    """
    if not external_id:
        # No stable identity to deduplicate on. Refusing is the safe half
        # of the choice: a repeat delivery would otherwise credit again.
        logger.error("provider grant claim without an external id (scope=%s)", scope)
        return False
    try:
        with transaction.atomic():
            ProviderGrant.objects.create(
                provider=provider, scope=scope, external_id=str(external_id)
            )
    except IntegrityError:
        logger.info("provider object %s:%s already granted", scope, external_id)
        return False
    return True


def _reconcile_checkout_session(
    obj: dict,
    *,
    expected_mode: str,
    expected_currency: str,
    expected_amount_cents: int | None,
) -> str | None:
    """Reason the session contradicts what we sold, or None when it matches.

    The grant is driven by ``metadata`` we set at session creation, so the
    only defence against a session that says something else is to compare
    it with the server-side catalog entry the metadata names. A missing
    field counts as a mismatch: "the provider did not say" must not read
    as "yes".

    ``expected_amount_cents`` is ``None`` for subscription mode — the first
    invoice of a subscription legitimately differs from the sticker price
    (trial, proration), and the recurring amount is governed by the
    provider's own price object plus the renewal invoices.
    """
    from .conf import strict_checkout_reconciliation

    if not strict_checkout_reconciliation():
        return None
    mode = obj.get("mode")
    if mode != expected_mode:
        return f"mode {mode!r} != expected {expected_mode!r}"
    payment_status = obj.get("payment_status")
    if payment_status not in _SETTLED_PAYMENT_STATUSES:
        return f"payment_status {payment_status!r} is not settled"
    currency = (obj.get("currency") or "").lower()
    if currency != expected_currency.lower():
        return f"currency {currency!r} != expected {expected_currency.lower()!r}"
    if expected_amount_cents is None:
        return None
    charged = obj.get("amount_total")
    if not isinstance(charged, int) or isinstance(charged, bool):
        return f"amount_total {charged!r} is not an integer"
    # A coupon lowers amount_total legitimately; the discount Stripe
    # reports is what makes the sum comparable to the catalog price again.
    discount = (obj.get("total_details") or {}).get("amount_discount") or 0
    if not isinstance(discount, int) or isinstance(discount, bool):
        return f"total_details.amount_discount {discount!r} is not an integer"
    if charged + discount != expected_amount_cents:
        return (
            f"amount_total {charged} + discount {discount} != catalog price "
            f"{expected_amount_cents}"
        )
    return None


# ─── Webhook handlers ──────────────────────────────────────


def handle_checkout_completed(event: dict) -> None:
    """Award credits for a one-off package purchase."""
    obj = event["data"]["object"]
    metadata = obj.get("metadata") or {}
    user_id = metadata.get("user_id")
    package_slug = metadata.get("package")
    plan_slug = metadata.get("plan")
    session_id = obj.get("id")
    if not user_id:
        logger.warning("checkout.session.completed without user_id")
        return
    User = _get_user_model()

    user = User.objects.filter(id=user_id).first()
    if not user:
        logger.warning("checkout.session.completed: user %s not found", user_id)
        return
    # Ownership: the session names its buyer twice, and the two must agree
    # before either one is used to pick a wallet.
    reference = obj.get("client_reference_id")
    if reference and str(reference) != str(user_id):
        logger.error(
            "checkout session %s: client_reference_id %s contradicts "
            "metadata user_id %s — refusing to grant",
            session_id,
            reference,
            user_id,
        )
        return
    if package_slug and package_slug in CREDIT_PACKAGES_BY_SLUG:
        pkg = CREDIT_PACKAGES_BY_SLUG[package_slug]
        mismatch = _reconcile_checkout_session(
            obj,
            expected_mode="payment",
            expected_currency=pkg.currency,
            expected_amount_cents=pkg.price_cents,
        )
        if mismatch:
            logger.error(
                "checkout session %s does not match package %r (%s) — "
                "refusing to grant credits",
                session_id,
                pkg.slug,
                mismatch,
            )
            return
        if not claim_provider_object(
            scope=ProviderGrant.SCOPE_CHECKOUT_SESSION, external_id=session_id
        ):
            return
        txn = credit(
            user=user,
            credits=pkg.credits,
            type=TransactionType.CREDIT_PURCHASE,
            # Bought with a card: this money does not evaporate at the end of
            # a billing period the buyer never subscribed to.
            source=LotSource.PURCHASE,
            expires_at=None,
            description=f"Purchase: {pkg.name}",
            amount_cents=pkg.price_cents,
            metadata={
                "package": pkg.slug,
                "stripe_session_id": obj.get("id"),
                # The refund path knows the payment intent, never the
                # session: `charge.refunded` carries the charge and its
                # intent, so the intent is what makes the grant findable
                # when the money is taken back (see _grant_transactions).
                "stripe_payment_intent": obj.get("payment_intent"),
            },
        )
        _announce_payment(
            user=user,
            txn=txn,
            amount_cents=pkg.price_cents,
            currency=pkg.currency.lower(),
            package=pkg.slug,
        )
    if plan_slug and plan_slug in PLANS_BY_SLUG:
        plan_entry = PLANS_BY_SLUG[plan_slug]
        mismatch = _reconcile_checkout_session(
            obj,
            expected_mode="subscription",
            expected_currency=plan_entry.currency,
            expected_amount_cents=None,
        )
        if mismatch:
            logger.error(
                "checkout session %s does not match plan %r (%s) — "
                "refusing to grant the plan",
                session_id,
                plan_slug,
                mismatch,
            )
            return
        if not claim_provider_object(
            scope=ProviderGrant.SCOPE_CHECKOUT_SESSION, external_id=session_id
        ):
            return
        sub, _ = Subscription.objects.update_or_create(
            user=user,
            defaults={
                "plan": plan_slug,
                "status": SubscriptionStatus.ACTIVE,
                "stripe_customer_id": obj.get("customer"),
                "stripe_subscription_id": obj.get("subscription"),
            },
        )
        # Stripe does not promise event order: the subscription event that
        # carries the billing period can land BEFORE this checkout. When it
        # did, its period is parked under the subscription id — claim it
        # now, before the grant below reads current_period_end, or the
        # bundle is created undated and nothing ever dates it.
        _apply_pending_period(sub)
        # Initial monthly credit grant for plans that bundle credits.
        monthly = plan_entry.monthly_credits_included
        if monthly:
            txn = credit(
                user=user,
                credits=monthly,
                type=TransactionType.SUBSCRIPTION_BONUS,
                # The plan's bundle lives as long as the period that pays for
                # it. The checkout session does not carry that period yet —
                # ``_stamp_subscription_period`` dates the lot from the
                # subscription event that arrives moments later.
                source=LotSource.SUBSCRIPTION,
                expires_at=_future_or_none(sub.current_period_end),
                description=f"Plan bonus: {plan_slug}",
                metadata={
                    "plan": plan_slug,
                    "stripe_subscription_id": sub.stripe_subscription_id,
                    # The first period's invoice, when the session names it:
                    # `invoice.paid` skips subscription_create, so without
                    # this the opening bundle is the one grant a refund of
                    # the first month could not find.
                    "stripe_invoice_id": obj.get("invoice"),
                    "stripe_session_id": obj.get("id"),
                },
            )
            _announce_payment(
                user=user,
                txn=txn,
                amount_cents=plan_entry.price_cents,
                currency=plan_entry.currency.lower(),
                plan=plan_slug,
            )
        _announce_subscription(sub)


def handle_invoice_paid(event: dict) -> None:
    """Grant the monthly plan credits on each renewal invoice.

    The initial grant happens in handle_checkout_completed; without this
    handler subscribers receive credits only in month one.
    """
    obj = event["data"]["object"]
    subscription_id = obj.get("subscription")
    invoice_id = obj.get("id")
    if not subscription_id:
        return
    # Stripe sends an invoice for the initial checkout too — that grant is
    # already handled by checkout.session.completed.
    if obj.get("billing_reason") == "subscription_create":
        return
    # The credits come from our catalog, but the reason to hand them over is
    # that this invoice was actually settled. An invoice object that carries
    # a status says so itself; anything else (open, draft, uncollectible,
    # void) is a renewal that has not been paid for.
    status = obj.get("status")
    if status is not None and status != "paid":
        logger.warning(
            "invoice %s is %r, not paid — refusing the renewal grant",
            invoice_id,
            status,
        )
        return
    sub = Subscription.objects.filter(stripe_subscription_id=subscription_id).first()
    if not sub or sub.plan not in PLANS_BY_SLUG:
        return
    plan_entry = PLANS_BY_SLUG[sub.plan]
    monthly = plan_entry.monthly_credits_included
    if not monthly:
        return
    # Idempotency per invoice: at-least-once webhook delivery must not
    # double-grant a renewal. Stripe describes one paid invoice with two
    # event types (invoice.paid and invoice.payment_succeeded), so distinct
    # event ids clear the per-event lock and race each other here; the
    # unique claim is what actually serialises them.
    if not claim_provider_object(
        scope=ProviderGrant.SCOPE_INVOICE, external_id=invoice_id
    ):
        return
    txn = credit(
        user=sub.user,
        credits=monthly,
        type=TransactionType.SUBSCRIPTION_BONUS,
        # The renewal's period end is already in hand (_apply_stripe_period),
        # so the month's bundle expires with the month it belongs to.
        source=LotSource.SUBSCRIPTION,
        expires_at=_future_or_none(
            _invoice_period_end(obj) or sub.current_period_end
        ),
        description=f"Plan renewal: {sub.plan}",
        metadata={
            "plan": sub.plan,
            "stripe_subscription_id": subscription_id,
            "stripe_invoice_id": invoice_id,
        },
    )
    amount = obj.get("amount_paid")
    if not isinstance(amount, int):
        amount = plan_entry.price_cents
    _announce_payment(
        user=sub.user,
        txn=txn,
        amount_cents=amount,
        currency=(obj.get("currency") or plan_entry.currency).lower(),
        plan=sub.plan,
    )


#: Provider status → local status. One table, read by every subscription
#: handler: two copies of it drift the day a status is added.
_STRIPE_STATUS_MAP = {
    "active": SubscriptionStatus.ACTIVE,
    "trialing": SubscriptionStatus.TRIALING,
    "past_due": SubscriptionStatus.PAST_DUE,
    "canceled": SubscriptionStatus.CANCELLED,
    "incomplete": SubscriptionStatus.INCOMPLETE,
}


def _resolve_local_subscription(obj: dict) -> Optional[Subscription]:
    """Find the local row a Stripe subscription object belongs to.

    Three routes, because the row may not yet carry the subscription id
    when the first subscription event arrives:

    1. by ``stripe_subscription_id`` — the steady state;
    2. by ``stripe_customer_id`` — a row created from a checkout that had
       no subscription id yet, adopted here;
    3. by ``metadata.user_id`` — what the host set on
       ``subscription_data.metadata``, when it did.

    ``None`` means the local row genuinely does not exist yet, and the
    caller parks the period rather than dropping it.
    """
    sub_id = obj.get("id")
    if sub_id:
        sub = Subscription.objects.filter(stripe_subscription_id=sub_id).first()
        if sub is not None:
            return sub
    customer_id = obj.get("customer")
    if customer_id:
        sub = Subscription.objects.filter(
            stripe_customer_id=customer_id
        ).first()
        if sub is not None:
            if sub_id and not sub.stripe_subscription_id:
                sub.stripe_subscription_id = str(sub_id)
                sub.save(update_fields=["stripe_subscription_id", "updated_at"])
            return sub
    user_id = (obj.get("metadata") or {}).get("user_id")
    if user_id:
        sub = Subscription.objects.filter(user_id=user_id).first()
        if sub is not None:
            changed = []
            if sub_id and not sub.stripe_subscription_id:
                sub.stripe_subscription_id = str(sub_id)
                changed.append("stripe_subscription_id")
            if customer_id and not sub.stripe_customer_id:
                sub.stripe_customer_id = str(customer_id)
                changed.append("stripe_customer_id")
            if changed:
                sub.save(update_fields=[*changed, "updated_at"])
            return sub
    return None


def _stash_subscription_period(obj: dict) -> Optional[PendingSubscriptionPeriod]:
    """Park a period whose local subscription has not been created yet.

    The alternative — what this module did until 0.11.0 — is to return
    silently. That loses the ONLY payload carrying the billing period,
    permanently: the event is marked processed, the checkout that lands a
    moment later grants an undated lot, and the bundle a cancelled
    subscriber keeps is the one nothing can expire.
    """
    sub_id = obj.get("id")
    if not sub_id:
        return None
    defaults = {
        "stripe_customer_id": str(obj.get("customer") or ""),
        "status": str(obj.get("status") or ""),
    }
    start = _epoch_to_datetime(obj.get("current_period_start"))
    end = _epoch_to_datetime(obj.get("current_period_end"))
    if start is not None:
        defaults["current_period_start"] = start
    if end is not None:
        defaults["current_period_end"] = end
    pending, _ = PendingSubscriptionPeriod.objects.update_or_create(
        stripe_subscription_id=str(sub_id), defaults=defaults
    )
    logger.info(
        "parked the billing period of subscription %s — no local row yet "
        "(the checkout has not landed); it is applied when one appears",
        sub_id,
    )
    return pending


def _apply_pending_period(sub: Subscription) -> bool:
    """Apply a parked period to *sub* and drop the stash. True if it changed.

    Also stamps the subscription lots, because the grant this period
    belongs to may already have been written undated.
    """
    if not sub.stripe_subscription_id:
        return False
    pending = PendingSubscriptionPeriod.objects.filter(
        stripe_subscription_id=sub.stripe_subscription_id
    ).first()
    if pending is None:
        return False
    changed: list[str] = []
    if (
        pending.current_period_start is not None
        and pending.current_period_start != sub.current_period_start
    ):
        sub.current_period_start = pending.current_period_start
        changed.append("current_period_start")
    if (
        pending.current_period_end is not None
        and pending.current_period_end != sub.current_period_end
    ):
        sub.current_period_end = pending.current_period_end
        changed.append("current_period_end")
    mapped = _STRIPE_STATUS_MAP.get(pending.status)
    if mapped is not None and mapped != sub.status:
        sub.status = mapped
        changed.append("status")
    if changed:
        sub.save(update_fields=[*changed, "updated_at"])
        _stamp_subscription_period(sub)
    pending.delete()
    logger.info(
        "applied the parked billing period of subscription %s to the local "
        "row (changed: %s)",
        sub.stripe_subscription_id,
        ", ".join(changed) or "nothing",
    )
    return bool(changed)


def handle_subscription_updated(event: dict) -> None:
    obj = event["data"]["object"]
    sub = _resolve_local_subscription(obj)
    if not sub:
        # Not "nothing to do": this payload carries the billing period, and
        # it is the only one that does. Park it for the checkout that has
        # not landed yet (see _stash_subscription_period).
        _stash_subscription_period(obj)
        return
    sub.status = _STRIPE_STATUS_MAP.get(obj.get("status"), sub.status)
    fields = ["status", "updated_at", *_apply_stripe_period(sub, obj)]
    sub.save(update_fields=fields)
    _stamp_subscription_period(sub)
    _announce_subscription(sub)


def handle_subscription_deleted(event: dict) -> None:
    obj = event["data"]["object"]
    sub = _resolve_local_subscription(obj)
    if not sub:
        _stash_subscription_period(obj)
        return
    from django.utils import timezone

    sub.status = SubscriptionStatus.CANCELLED
    sub.cancelled_at = timezone.now()
    fields = ["status", "cancelled_at", "updated_at", *_apply_stripe_period(sub, obj)]
    sub.save(update_fields=fields)
    # The cancellation is the last event that carries a period, so it is the
    # last chance to date a bundle granted before one was known. An undated
    # subscription lot outlives the subscription itself.
    _stamp_subscription_period(sub)
    _announce_subscription(sub)


# ─── Clawback (refund, dispute, credit note) ────────────────


def _grant_transactions(obj: dict) -> list[Transaction]:
    """The grant rows a refund-shaped provider object refers to.

    Resolution is by the provider ids the grant recorded on itself:
    the invoice (renewals), the payment intent (one-off packages) and the
    charge. A refund whose object names none of them cannot be matched to a
    grant, and guessing — "the customer's most recent purchase" — would
    claw back credits a different payment bought.
    """
    lookups = []
    for key, field in (
        ("invoice", "stripe_invoice_id"),
        ("payment_intent", "stripe_payment_intent"),
        ("charge", "stripe_charge_id"),
    ):
        value = obj.get(key)
        if isinstance(value, str) and value:
            lookups.append(Q(**{f"metadata__{field}": value}))
    if not lookups:
        return []
    condition = lookups[0]
    for extra in lookups[1:]:
        condition |= extra
    return list(
        Transaction.objects.filter(condition, credits_delta__gt=0).order_by(
            "created_at", "id"
        )
    )


def _clawback_credits(obj: dict, txn: Transaction) -> int:
    """How many of a grant's credits this object takes back.

    A partial refund takes back a proportional part of the bundle: the
    money and the credits were one transaction, so half the money back is
    half the credits back. Anything that does not report both halves of the
    ratio (a dispute, a credit note) is treated as the whole grant — the
    conservative direction for the deployment, and the one a support agent
    can undo with a manual credit.
    """
    granted = txn.credits_delta
    refunded = obj.get("amount_refunded")
    total = obj.get("amount")
    if (
        isinstance(refunded, int)
        and not isinstance(refunded, bool)
        and isinstance(total, int)
        and not isinstance(total, bool)
        and total > 0
        and 0 < refunded < total
    ):
        return max(1, min(granted, round(granted * refunded / total)))
    return granted


@transaction.atomic
def claw_back_grant(
    *,
    txn: Transaction,
    credits: Optional[int] = None,
    description: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> ClawbackResult:
    """Take back credits a grant handed out, and record what is already gone.

    Only the lots THIS grant created are touched. The alternative — taking
    the shortfall out of whatever else the wallet holds — would pay a
    refunded subscription back with the credits the customer bought for
    cash, which is a second wrong on top of the first.

    Three buckets, and the result names all three: ``reclaimed`` was still
    in the grant's lots; ``forgiven`` had already expired unspent, so the
    deployment took those credits back once already; ``outstanding`` was
    spent, and becomes a :class:`~stapel_billing.models.CreditDebt` the
    next top-up collects.
    """
    now = timezone.now()
    requested = credits if credits is not None else txn.credits_delta
    wallet = Wallet.objects.select_for_update().get(pk=txn.wallet_id)
    _expire_due_lots(wallet, now=now)
    granted_lots = list(
        CreditLot.objects.select_for_update()
        .filter(granting_transaction=txn)
        .order_by("created_at", "id")
    )
    outstanding = requested
    taken: list[tuple[CreditLot, int]] = []
    for lot in granted_lots:
        if outstanding <= 0:
            break
        amount = min(outstanding, lot.credits_remaining)
        if amount <= 0:
            continue
        lot.credits_remaining -= amount
        lot.save(update_fields=["credits_remaining"])
        taken.append((lot, amount))
        outstanding -= amount
    reclaimed = requested - outstanding
    forgiven = 0
    if outstanding > 0 and granted_lots:
        # Credits that died of old age are not credits the customer used.
        # Billing them again would claw back the same credits twice.
        expired = (
            Transaction.objects.filter(
                lot_id__in=[lot.id for lot in granted_lots],
                type=TransactionType.EXPIRATION,
            ).aggregate(total=Sum("credits_delta"))["total"]
            or 0
        )
        forgiven = min(outstanding, -expired)
        outstanding -= forgiven
    balance = _sync_balance(wallet, now=now)
    row_metadata = {
        **_clean_metadata(metadata),
        "clawback_of": str(txn.id),
        "requested": requested,
        "reclaimed": reclaimed,
        "forgiven": forgiven,
        "outstanding": outstanding,
        "lots": _consumption_metadata(taken),
    }
    row = Transaction.objects.create(
        wallet=wallet,
        lot=_single_lot(taken),
        type=TransactionType.REFUND,
        amount_cents=None,
        credits_delta=-reclaimed,
        balance_after=balance,
        description=description or f"Clawback of {txn.type}",
        metadata=row_metadata,
    )
    debt = None
    if outstanding > 0:
        debt = _open_debt(
            wallet,
            credits=outstanding,
            reason=DebtReason.CLAWBACK,
            type=TransactionType.REFUND,
            description=description or f"Clawback of {txn.type}",
            metadata=row_metadata,
        )
        debt.transaction = row
        debt.save(update_fields=["transaction"])
        row.metadata["debt_id"] = str(debt.id)
        row.save(update_fields=["metadata"])
    logger.info(
        "clawed back %s of %s credit(s) from grant %s (forgiven %s, owed %s)",
        reclaimed,
        requested,
        txn.id,
        forgiven,
        outstanding,
    )
    return ClawbackResult(
        transaction=row,
        requested=requested,
        reclaimed=reclaimed,
        forgiven=forgiven,
        outstanding=outstanding,
        debt=debt,
    )


def _handle_clawback(event: dict, *, label: str) -> None:
    """Shared body of the three money-back webhooks.

    Idempotent per EVENT (``ProviderGrant``, scope ``clawback``): a
    clawback is a spend, and at-least-once delivery must not spend twice.
    The claim is per event rather than per charge on purpose — a partial
    refund and a later second partial refund of the same charge are two
    real clawbacks.
    """
    obj = event.get("data", {}).get("object") or {}
    event_id = event.get("id")
    grants = _grant_transactions(obj)
    if not grants:
        logger.warning(
            "%s event %s does not name any grant this deployment made "
            "(invoice=%r payment_intent=%r charge=%r) — nothing to claw back",
            label,
            event_id,
            obj.get("invoice"),
            obj.get("payment_intent"),
            obj.get("charge"),
        )
        return
    if not claim_provider_object(
        scope=ProviderGrant.SCOPE_CLAWBACK, external_id=event_id
    ):
        return
    for txn in grants:
        claw_back_grant(
            txn=txn,
            credits=_clawback_credits(obj, txn),
            description=f"{label}: {txn.description or txn.type}",
            metadata={
                "clawback_reason": label,
                "stripe_event_id": event_id,
                "stripe_object_id": obj.get("id"),
            },
        )


def handle_charge_refunded(event: dict) -> None:
    """``charge.refunded`` — the money went back, so the credits do too."""
    _handle_clawback(event, label="refund")


def handle_dispute_created(event: dict) -> None:
    """``charge.dispute.created`` — the customer's bank took the money back.

    Clawed back at the moment the dispute opens rather than when it is
    resolved: the credits are spendable now, and a dispute that is later
    won is one manual credit, while a dispute lost after the credits were
    spent is unrecoverable.
    """
    _handle_clawback(event, label="dispute")


def handle_credit_note_created(event: dict) -> None:
    """``credit_note.created`` — an invoice was credited back, in part or whole."""
    _handle_clawback(event, label="credit_note")
