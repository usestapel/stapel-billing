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
from datetime import timedelta
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from stapel_core.comm import emit
from stapel_core.signals import payment_completed, subscription_changed

from .catalog import CREDIT_PACKAGES_BY_SLUG, PLANS_BY_SLUG
from .models import (
    CreditHold,
    CreditLot,
    HoldAllocation,
    HoldStatus,
    LotSource,
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
) -> Transaction:
    """Add credits to a user's wallet as one lot, plus its ledger row.

    *source* is required (``LotSource``): a lot that cannot say where it came
    from cannot be reasoned about later — "which of these expire, and which
    did the customer pay cash for" is exactly the question a single balance
    could not answer, so the answer is not allowed to default.

    *expires_at* is the moment the lot dies (``None`` = never). Pass the
    subscription's ``current_period_end`` for plan bundles; leave it None for
    purchased packages.
    """
    if credits <= 0:
        raise ValueError("credits must be positive")
    now = timezone.now()
    if expires_at is not None and expires_at <= now:
        # Granting credits that are already dead is a caller bug, and a
        # silently-zero grant is the worst way to find out about it.
        raise ValueError("expires_at must be in the future")
    wallet = _lock_wallet(user)
    _expire_due_lots(wallet, now=now)
    lot = CreditLot.objects.create(
        wallet=wallet,
        source=source,
        credits_initial=credits,
        credits_remaining=credits,
        expires_at=expires_at,
    )
    balance = _sync_balance(wallet, now=now)
    txn = Transaction.objects.create(
        wallet=wallet,
        lot=lot,
        type=type,
        amount_cents=amount_cents,
        credits_delta=credits,
        balance_after=balance,
        description=description,
        metadata=metadata or {},
    )
    lot.granting_transaction = txn
    lot.save(update_fields=["granting_transaction"])
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
) -> Transaction:
    """Deduct credits from a user's wallet.  Raises InsufficientCreditsError.

    Spends the lots expiring-soonest first, so credits die used rather than
    unused. The per-lot split is recorded on
    ``Transaction.metadata["lots"]``; ``Transaction.lot`` names the lot
    directly when the debit came out of exactly one.

    When *idempotency_key* is given, a duplicate call (same key, same
    wallet) short-circuits and returns the original transaction without
    debiting again — safe under at-least-once service-to-service retries.
    The key is stored on ``Transaction.metadata["idempotency_key"]``.
    """
    if credits <= 0:
        raise ValueError("credits must be positive")
    now = timezone.now()
    wallet = _lock_wallet(user)
    if idempotency_key:
        # The wallet row is locked above, so concurrent duplicates
        # serialize here and the second caller sees the first's row.
        existing = Transaction.objects.filter(
            wallet=wallet,
            metadata__idempotency_key=idempotency_key,
        ).first()
        if existing is not None:
            logger.info(
                "debit short-circuited by idempotency_key=%s (txn %s)",
                idempotency_key,
                existing.id,
            )
            return existing
    _expire_due_lots(wallet, now=now)
    taken = _consume_lots(wallet, credits, now=now)
    balance = _sync_balance(wallet, now=now)
    metadata = dict(metadata or {})
    if idempotency_key:
        metadata["idempotency_key"] = idempotency_key
    metadata["lots"] = _consumption_metadata(taken)
    return Transaction.objects.create(
        wallet=wallet,
        lot=_single_lot(taken),
        type=type,
        amount_cents=None,
        credits_delta=-credits,
        balance_after=balance,
        description=description,
        metadata=metadata,
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
                metadata={"plan": plan_slug, "stripe_subscription_id": sub.stripe_subscription_id},
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


def handle_subscription_updated(event: dict) -> None:
    obj = event["data"]["object"]
    sub = Subscription.objects.filter(
        stripe_subscription_id=obj.get("id")
    ).first()
    if not sub:
        return
    status_map = {
        "active": SubscriptionStatus.ACTIVE,
        "trialing": SubscriptionStatus.TRIALING,
        "past_due": SubscriptionStatus.PAST_DUE,
        "canceled": SubscriptionStatus.CANCELLED,
        "incomplete": SubscriptionStatus.INCOMPLETE,
    }
    sub.status = status_map.get(obj.get("status"), sub.status)
    fields = ["status", "updated_at", *_apply_stripe_period(sub, obj)]
    sub.save(update_fields=fields)
    _stamp_subscription_period(sub)
    _announce_subscription(sub)


def handle_subscription_deleted(event: dict) -> None:
    obj = event["data"]["object"]
    sub = Subscription.objects.filter(
        stripe_subscription_id=obj.get("id")
    ).first()
    if not sub:
        return
    from django.utils import timezone

    sub.status = SubscriptionStatus.CANCELLED
    sub.cancelled_at = timezone.now()
    fields = ["status", "cancelled_at", "updated_at", *_apply_stripe_period(sub, obj)]
    sub.save(update_fields=fields)
    _announce_subscription(sub)
