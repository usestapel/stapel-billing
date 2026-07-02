"""Wallet operations and payment-provider integration helpers.

The payment backend is pluggable: ``get_provider()`` resolves the
``STAPEL_BILLING["PAYMENT_PROVIDER"]`` dotted path (Stripe by default)
and the checkout/portal/webhook helpers below delegate to it.  Provider
credentials are read lazily at call time — nothing is frozen at import.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.db import transaction

from stapel_core.comm import emit
from stapel_core.signals import payment_completed, subscription_changed

from .catalog import CREDIT_PACKAGES_BY_SLUG, PLANS_BY_SLUG
from .models import (
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
    """Emit subscription.changed + send the subscription_changed signal."""
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


# ─── Webhook handlers ──────────────────────────────────────


def handle_checkout_completed(event: dict) -> None:
    """Award credits for a one-off package purchase."""
    obj = event["data"]["object"]
    metadata = obj.get("metadata") or {}
    user_id = metadata.get("user_id")
    package_slug = metadata.get("package")
    plan_slug = metadata.get("plan")
    if not user_id:
        logger.warning("checkout.session.completed without user_id")
        return
    User = _get_user_model()

    user = User.objects.filter(id=user_id).first()
    if not user:
        logger.warning("checkout.session.completed: user %s not found", user_id)
        return
    if package_slug and package_slug in CREDIT_PACKAGES_BY_SLUG:
        pkg = CREDIT_PACKAGES_BY_SLUG[package_slug]
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
    sub = Subscription.objects.filter(stripe_subscription_id=subscription_id).first()
    if not sub or sub.plan not in PLANS_BY_SLUG:
        return
    plan_entry = PLANS_BY_SLUG[sub.plan]
    monthly = plan_entry.monthly_credits_included
    if not monthly:
        return
    # Idempotency per invoice: at-least-once webhook delivery must not
    # double-grant a renewal.
    already = Transaction.objects.filter(
        wallet__user=sub.user,
        type=TransactionType.SUBSCRIPTION_BONUS,
        metadata__stripe_invoice_id=invoice_id,
    ).exists()
    if already:
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
    sub.save(update_fields=["status", "updated_at"])
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
    sub.save(update_fields=["status", "cancelled_at", "updated_at"])
    _announce_subscription(sub)
