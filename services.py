"""Wallet operations and Stripe integration helpers."""

from __future__ import annotations

import logging
import os
from typing import Optional

from django.db import transaction

from .catalog import CREDIT_PACKAGES_BY_SLUG, PLANS_BY_SLUG
from .models import (
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    Wallet,
)

logger = logging.getLogger(__name__)


class InsufficientCreditsError(Exception):
    pass


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
) -> Transaction:
    """Deduct credits from a user's wallet.  Raises InsufficientCreditsError."""
    if credits <= 0:
        raise ValueError("credits must be positive")
    wallet = (
        Wallet.objects.select_for_update().get_or_create(user=user)[0]
    )
    if wallet.balance < credits:
        raise InsufficientCreditsError(
            f"balance={wallet.balance} < required={credits}"
        )
    new_balance = wallet.balance - credits
    wallet.balance = new_balance
    wallet.save(update_fields=["balance", "updated_at"])
    return Transaction.objects.create(
        wallet=wallet,
        type=type,
        amount_cents=None,
        credits_delta=-credits,
        balance_after=new_balance,
        description=description,
        metadata=metadata or {},
    )


# ─── Stripe ─────────────────────────────────────────────────


STRIPE_API_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


def _stripe_module():
    try:
        import stripe  # type: ignore
    except ImportError:
        return None
    if STRIPE_API_KEY:
        stripe.api_key = STRIPE_API_KEY
    return stripe


def create_checkout_session(
    *, user, package: Optional[str], plan: Optional[str], success_url: str, cancel_url: str
):
    """Create a Stripe Checkout session for either a one-off package or a plan.

    Returns a (checkout_url, session_id) tuple.  Falls back to a dev
    placeholder URL when Stripe is not configured — the call still
    succeeds so the rest of the flow is testable end-to-end.
    """
    stripe = _stripe_module()
    if package:
        pkg = CREDIT_PACKAGES_BY_SLUG[package]
        if not stripe or not STRIPE_API_KEY:
            return _placeholder_checkout("package", pkg.slug)
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=str(user.id),
            metadata={"user_id": str(user.id), "package": pkg.slug},
            line_items=[
                {
                    "price_data": {
                        "currency": pkg.currency.lower(),
                        "product_data": {"name": f"Credit pack: {pkg.name}"},
                        "unit_amount": pkg.price_cents,
                    },
                    "quantity": 1,
                }
            ],
        )
        return session["url"], session["id"]

    plan_entry = PLANS_BY_SLUG[plan]
    if not stripe or not STRIPE_API_KEY:
        return _placeholder_checkout("plan", plan_entry.slug)
    session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(user.id),
        metadata={"user_id": str(user.id), "plan": plan_entry.slug},
        line_items=[
            {
                "price_data": {
                    "currency": plan_entry.currency.lower(),
                    "recurring": {"interval": "month"},
                    "product_data": {"name": plan_entry.name},
                    "unit_amount": plan_entry.price_cents,
                },
                "quantity": 1,
            }
        ],
    )
    return session["url"], session["id"]


def _placeholder_checkout(kind: str, slug: str):
    """Used in dev/staging when Stripe is unconfigured."""
    return (f"https://example.invalid/stripe-not-configured/{kind}/{slug}", f"cs_dev_{slug}")


def create_customer_portal(*, customer_id: str, return_url: str) -> str:
    stripe = _stripe_module()
    if not stripe or not STRIPE_API_KEY or not customer_id:
        return "https://example.invalid/stripe-portal-not-configured"
    portal = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return portal["url"]


def verify_stripe_signature(payload: bytes, sig_header: str) -> dict:
    """Parse + verify a Stripe webhook payload.  Returns the event dict."""
    stripe = _stripe_module()
    if not stripe or not STRIPE_WEBHOOK_SECRET:
        raise ValueError("Stripe is not configured")
    return stripe.Webhook.construct_event(
        payload, sig_header, STRIPE_WEBHOOK_SECRET
    )


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
    from stapel_core.django.users.models import User

    user = User.objects.filter(id=user_id).first()
    if not user:
        logger.warning("checkout.session.completed: user %s not found", user_id)
        return
    if package_slug and package_slug in CREDIT_PACKAGES_BY_SLUG:
        pkg = CREDIT_PACKAGES_BY_SLUG[package_slug]
        credit(
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
    if plan_slug and plan_slug in PLANS_BY_SLUG:
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
        monthly = PLANS_BY_SLUG[plan_slug].monthly_credits_included
        if monthly:
            credit(
                user=user,
                credits=monthly,
                type=TransactionType.SUBSCRIPTION_BONUS,
                description=f"Plan bonus: {plan_slug}",
                metadata={"plan": plan_slug, "stripe_subscription_id": sub.stripe_subscription_id},
            )


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
    monthly = PLANS_BY_SLUG[sub.plan].monthly_credits_included
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
    credit(
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
