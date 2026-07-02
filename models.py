"""
Billing domain: Wallet (per-user credit balance), Transaction
(immutable ledger), Subscription (Stripe-backed plan).

Charge / refund operations go through `wallet_service.debit()` /
`.credit()` which write a Transaction row and update Wallet.balance
inside a DB transaction.  Stripe webhooks land at
`/billing/api/webhooks/stripe/` and are validated by signature.
"""

import uuid
from django.conf import settings
from django.db import models


# =====================================================================
# Enums
# =====================================================================


class Plan(models.TextChoices):
    FREE = "free", "Free"
    PRO = "pro", "Pro"
    TEAM = "team", "Team"
    ENTERPRISE = "enterprise", "Enterprise"


class SubscriptionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    TRIALING = "trialing", "Trialing"
    PAST_DUE = "past_due", "Past Due"
    CANCELLED = "cancelled", "Cancelled"
    INCOMPLETE = "incomplete", "Incomplete"


class TransactionType(models.TextChoices):
    CREDIT_PURCHASE = "credit_purchase", "Credit Purchase"
    TRANSCRIPTION_CHARGE = "transcription_charge", "Transcription Charge"
    AI_CHARGE = "ai_charge", "AI Charge"
    SUBSCRIPTION_BONUS = "subscription_bonus", "Subscription Bonus"
    REFUND = "refund", "Refund"
    ADJUSTMENT = "adjustment", "Manual Adjustment"


# =====================================================================
# Models
# =====================================================================


class Wallet(models.Model):
    """Per-user credit balance. One wallet per User."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet"
    )
    balance = models.IntegerField(default=0, help_text="Integer credits — never fractional.")
    currency = models.CharField(max_length=3, default="USD")
    auto_recharge_enabled = models.BooleanField(default=False)
    auto_recharge_threshold = models.IntegerField(default=0)
    auto_recharge_package = models.CharField(max_length=32, null=True, blank=True)
    low_balance_alert = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_wallet"

    def __str__(self):
        return f"Wallet({self.user_id}): {self.balance} {self.currency}"


class Transaction(models.Model):
    """Immutable credit ledger entry."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(
        Wallet, on_delete=models.PROTECT, related_name="transactions"
    )
    type = models.CharField(max_length=32, choices=TransactionType.choices)
    amount_cents = models.IntegerField(null=True, blank=True)
    credits_delta = models.IntegerField(
        help_text="Positive = credit, negative = debit."
    )
    balance_after = models.IntegerField()
    description = models.CharField(max_length=255, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_transaction"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["wallet", "-created_at"]),
            models.Index(fields=["type"]),
        ]

    def __str__(self):
        return f"{self.type}: {self.credits_delta:+d} → {self.balance_after}"


class Subscription(models.Model):
    """Stripe-backed subscription. One per User."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.CharField(max_length=16, choices=Plan.choices, default=Plan.FREE)
    status = models.CharField(
        max_length=16, choices=SubscriptionStatus.choices, default=SubscriptionStatus.ACTIVE
    )
    stripe_subscription_id = models.CharField(max_length=255, null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=255, null=True, blank=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_subscription"
        indexes = [
            models.Index(fields=["stripe_customer_id"]),
            models.Index(fields=["stripe_subscription_id"]),
        ]

    def __str__(self):
        return f"{self.user_id}: {self.plan} ({self.status})"


class StripeWebhookEvent(models.Model):
    """Idempotency log for incoming Stripe webhooks."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stripe_event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=64)
    payload = models.JSONField()
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_stripe_webhook_event"
        indexes = [models.Index(fields=["event_type", "-received_at"])]
