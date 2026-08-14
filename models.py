"""
Billing domain: Wallet (per-user credit balance), Transaction
(immutable ledger), Subscription (Stripe-backed plan).

Charge / refund operations go through `wallet_service.debit()` /
`.credit()` which write a Transaction row and update Wallet.balance
inside a DB transaction.  Stripe webhooks land at
`/billing/api/v1/webhooks/stripe/` and are validated by signature.
"""

import uuid
from django.conf import settings
from django.db import models

from stapel_core.access import access


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
        # Provider identifiers route incoming webhooks to exactly one
        # local subscription. Two rows sharing one id make that routing
        # ambiguous — the lookups below are `.first()`, so the loser of a
        # duplicate silently stops receiving lifecycle updates while the
        # customer keeps paying. Partial: NULL/"" mean "no provider object
        # yet" and many rows legitimately sit in that state.
        constraints = [
            models.UniqueConstraint(
                fields=["stripe_customer_id"],
                condition=~models.Q(stripe_customer_id=None)
                & ~models.Q(stripe_customer_id=""),
                name="billing_subscription_unique_stripe_customer",
            ),
            models.UniqueConstraint(
                fields=["stripe_subscription_id"],
                condition=~models.Q(stripe_subscription_id=None)
                & ~models.Q(stripe_subscription_id=""),
                name="billing_subscription_unique_stripe_subscription",
            ),
        ]

    def __str__(self):
        return f"{self.user_id}: {self.plan} ({self.status})"


@access.ops
class ProviderGrant(models.Model):
    """One provider object, one grant — enforced by the database.

    ``StripeWebhookEvent`` deduplicates *events*; this row deduplicates the
    *business object* an event talks about. The provider may describe one
    paid invoice or one completed checkout session in several distinct
    events, and two of those can land concurrently: a
    read-then-credit guard sees no prior grant in either worker and both
    credit the wallet. Inserting the claim under the unique constraint in
    the same transaction as the credit makes the second one lose.
    """

    #: Grant scopes. The value is the kind of provider object claimed, not
    #: the event that carried it.
    SCOPE_CHECKOUT_SESSION = "checkout_session"
    SCOPE_INVOICE = "invoice"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=32, default="stripe")
    scope = models.CharField(max_length=32)
    external_id = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_provider_grant"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "scope", "external_id"],
                name="billing_provider_grant_unique_object",
            )
        ]

    def __str__(self):
        return f"{self.provider}:{self.scope}:{self.external_id}"


@access.ops
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
