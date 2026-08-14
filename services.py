"""Wallet operations and payment-provider integration helpers.

The payment backend is pluggable: ``get_provider()`` resolves the
``STAPEL_BILLING["PAYMENT_PROVIDER"]`` dotted path (Stripe by default)
and the checkout/portal/webhook helpers below delegate to it.  Provider
credentials are read lazily at call time — nothing is frozen at import.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.db import IntegrityError, transaction

from stapel_core.comm import emit
from stapel_core.signals import payment_completed, subscription_changed

from .catalog import CREDIT_PACKAGES_BY_SLUG, PLANS_BY_SLUG
from .models import (
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


def _get_user_model():
    from django.contrib.auth import get_user_model

    return get_user_model()


def get_or_create_wallet(user) -> Wallet:
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


@transaction.atomic
def credit(
    *,
    user,
    credits: int,
    type: str,
    description: Optional[str] = None,
    amount_cents: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> Transaction:
    """Add credits to a user's wallet, recording an immutable ledger row."""
    if credits <= 0:
        raise ValueError("credits must be positive")
    wallet = (
        Wallet.objects.select_for_update().get_or_create(user=user)[0]
    )
    new_balance = wallet.balance + credits
    wallet.balance = new_balance
    wallet.save(update_fields=["balance", "updated_at"])
    return Transaction.objects.create(
        wallet=wallet,
        type=type,
        amount_cents=amount_cents,
        credits_delta=credits,
        balance_after=new_balance,
        description=description,
        metadata=metadata or {},
    )


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

    When *idempotency_key* is given, a duplicate call (same key, same
    wallet) short-circuits and returns the original transaction without
    debiting again — safe under at-least-once service-to-service retries.
    The key is stored on ``Transaction.metadata["idempotency_key"]``.
    """
    if credits <= 0:
        raise ValueError("credits must be positive")
    wallet = (
        Wallet.objects.select_for_update().get_or_create(user=user)[0]
    )
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
    if wallet.balance < credits:
        raise InsufficientCreditsError(
            f"balance={wallet.balance} < required={credits}"
        )
    new_balance = wallet.balance - credits
    wallet.balance = new_balance
    wallet.save(update_fields=["balance", "updated_at"])
    metadata = dict(metadata or {})
    if idempotency_key:
        metadata["idempotency_key"] = idempotency_key
    return Transaction.objects.create(
        wallet=wallet,
        type=type,
        amount_cents=None,
        credits_delta=-credits,
        balance_after=new_balance,
        description=description,
        metadata=metadata,
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
    from .conf import billing_settings

    if not billing_settings.STRICT_CHECKOUT_RECONCILIATION:
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
