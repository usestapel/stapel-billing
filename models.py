"""
Billing domain: Wallet (per-user credit balance), CreditLot (where those
credits came from and when they die), Transaction (immutable ledger),
CreditHold (a reservation against future spend), Subscription
(Stripe-backed plan).

Charge / refund operations go through `services.debit()` / `.credit()` /
`.hold()` / `.capture()` / `.release()`, which move credits between lots
and write a Transaction row inside the wallet's row lock.  Stripe webhooks
land at `/billing/api/v1/webhooks/stripe/` and are validated by signature.

Since 0.8.0 a wallet is a **set of lots**, not one integer. Credits enter
as a lot that remembers its source and its expiry; spend walks the lots
expiring-soonest first so a subscription bundle is used before the
non-expiring credits the user paid cash for. ``Wallet.balance`` survives as
a maintained cache of the live lots' remaining credits — every read path
(API responses, entitlement checks, host pre-flights) wants a cheap scalar,
and re-aggregating the lot table on each of them would be a tax on reads to
pay for a property writes can maintain for free under the lock they already
take.
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
    # Credits that died of old age rather than being spent. It is its own
    # type because the accounting question "where did the month's credits
    # go" has two very different answers — the customer used them, or the
    # deployment took them back — and a refund-shaped negative row cannot
    # tell them apart.
    EXPIRATION = "expiration", "Credit Expiration"


class LotSource(models.TextChoices):
    """Why a lot of credits exists.

    The source is what a support answer, a refund decision and a revenue
    report all key off — "these 3000 came with February's subscription and
    expire with it" is a different fact from "these 500 were bought with a
    card and never expire", and once the two are summed into one balance
    neither is recoverable.
    """

    PURCHASE = "purchase", "Purchase"
    SUBSCRIPTION = "subscription", "Subscription"
    GRANT = "grant", "Grant"
    ADJUSTMENT = "adjustment", "Adjustment"
    # Credits handed back when a reservation was not fully spent. They are
    # a distinct source because they are not new money: they carry the
    # expiry of the lot they were taken from (see services.release).
    HOLD_RELEASE = "hold_release", "Hold Release"


class HoldStatus(models.TextChoices):
    HELD = "held", "Held"
    CAPTURED = "captured", "Captured"
    RELEASED = "released", "Released"
    EXPIRED = "expired", "Expired"


# =====================================================================
# Models
# =====================================================================


class Wallet(models.Model):
    """Per-user credit balance. One wallet per User.

    ``balance`` is a **cache**, not the truth: the truth is the set of live
    :class:`CreditLot` rows. Every operation that moves credits updates both
    inside the same ``select_for_update()`` block, so the two cannot drift
    while the process is doing its job; ``services.reconcile_wallet_balances()``
    (wired into ``get_billing_beat_schedule()``) is the periodic proof that
    they did not drift anyway.

    ``balance`` is *spendable* credits — the credits reserved by a live
    :class:`CreditHold` have already left it. That is the number every
    caller means by "can this user afford it".
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wallet",
        help_text=(
            "NULL once the owner has been erased — the ledger outlives the "
            "person (see stapel_billing.gdpr). SET_NULL and not CASCADE "
            "because Transaction protects its wallet: a cascade from the "
            "user row would raise ProtectedError and block the account "
            "deletion it was supposed to serve."
        ),
    )
    user_pseudonym = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text=(
            "The erased owner's stable pseudonym ('erased:<hmac>'), written "
            "by stapel_billing.gdpr.erase_subject. Empty for a live wallet. "
            "Keeps one subject's history one subject without naming them."
        ),
    )
    balance = models.IntegerField(
        default=0,
        help_text=(
            "Integer credits — never fractional. Maintained cache of "
            "SUM(credit_lot.credits_remaining) over live lots; write it "
            "only through stapel_billing.services."
        ),
    )
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


class CreditLot(models.Model):
    """One batch of credits with a common origin and a common expiry.

    A lot only ever shrinks: ``credits_remaining`` walks down from
    ``credits_initial`` as the credits are spent, reserved or expired, and
    credits handed back come in as a *new* lot rather than as a refill (see
    :class:`LotSource.HOLD_RELEASE`). Keeping lots monotonic is what makes
    "was this credit still alive at time T" answerable at all — a lot that
    could go back up would erase the moment its predecessor died.
    """

    #: Opaque id: lot ids travel in transaction metadata and GDPR exports.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: PROTECT, like Transaction: a wallet with lots is not deletable data.
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="lots")
    #: Where these credits came from (LotSource) — see the class docstring.
    source = models.CharField(max_length=16, choices=LotSource.choices)
    credits_initial = models.IntegerField(help_text="Credits this lot was created with.")
    credits_remaining = models.IntegerField(help_text="Credits still unspent and unreserved.")
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When these credits die. NULL = never (the paid-cash case).",
    )
    granting_transaction = models.ForeignKey(
        "Transaction",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_lots",
        help_text="The ledger row that brought this lot into existence.",
    )
    #: Breaks ties in the consumption walk, so the order is deterministic.
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_credit_lot"
        ordering = ["created_at"]
        indexes = [
            # The consumption walk: live lots of one wallet, expiring first.
            models.Index(fields=["wallet", "expires_at"]),
            # The expiry sweep, which scans across wallets by deadline.
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        horizon = self.expires_at.date().isoformat() if self.expires_at else "never"
        return f"CreditLot({self.source}): {self.credits_remaining}/{self.credits_initial} → {horizon}"


class Transaction(models.Model):
    """Immutable credit ledger entry.

    ``balance_after`` is the wallet's *spendable* balance the moment the row
    was written. Since 0.8.0 that is not the running sum of
    ``credits_delta``: :func:`services.hold` moves credits out of the
    spendable balance without writing a ledger row, because a reservation is
    not yet a charge — the ledger records what was actually billed, and the
    charge appears when the hold is captured.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(
        Wallet, on_delete=models.PROTECT, related_name="transactions"
    )
    lot = models.ForeignKey(
        CreditLot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions",
        help_text=(
            "The single lot this row moved credits in or out of. NULL when "
            "the operation spanned several lots — the per-lot split is then "
            "in metadata['lots']."
        ),
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


class CreditHold(models.Model):
    """A reservation: credits already taken out of the spendable balance.

    A hold is a real withdrawal, not an accounting overlay — the lots it
    names are decremented the moment it is created. That is the only version
    of "reserve" that survives two concurrent requests: an overlay
    (``available = balance - sum(open holds)``) recomputed per caller is a
    read-then-write race no row lock on the wallet alone can close, because
    the number being defended lives in a different table's aggregate.

    The lifecycle is closed on every path: ``capture`` bills the actual
    amount, ``release`` hands the credits back, and ``expires_at`` plus the
    ``expire_holds`` worker close the one that neither happened — a pipeline
    that died between the reservation and the answer must not leave a
    customer's credits locked up forever.
    """

    #: The capture/release handle: hold_id is also the idempotency key for both.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: PROTECT: an open reservation is a claim on money, not disposable data.
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="holds")
    credits = models.IntegerField(help_text="Credits reserved by this hold.")
    type = models.CharField(
        max_length=32,
        choices=TransactionType.choices,
        help_text="Transaction type the capture will be billed under.",
    )
    #: Carried onto the capture's ledger row when the caller passes none.
    description = models.CharField(max_length=255, null=True, blank=True)
    #: Caller context, merged into the capture transaction's metadata.
    metadata = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(
        max_length=255,
        help_text="Caller-supplied key; unique per wallet, so a retried hold reserves once.",
    )
    #: held → captured | released | expired. Only `held` reserves credits.
    status = models.CharField(
        max_length=16, choices=HoldStatus.choices, default=HoldStatus.HELD
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When expire_holds releases this hold. NULL = never swept.",
    )
    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the hold stopped being held (captured, released or expired).",
    )
    #: How long the work took, read against resolved_at when tuning the TTL.
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_credit_hold"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["wallet", "-created_at"]),
            # The crash-safety sweep: open holds past their deadline.
            models.Index(fields=["status", "expires_at"]),
        ]
        constraints = [
            # The idempotency short-circuit reads this key under the wallet's
            # row lock; without the constraint two deliveries that arrive
            # before either commits would both insert and both reserve.
            models.UniqueConstraint(
                fields=["wallet", "idempotency_key"],
                name="billing_credit_hold_unique_idempotency_key",
            )
        ]

    def __str__(self):
        return f"CreditHold({self.status}): {self.credits}"


class HoldAllocation(models.Model):
    """How much of a hold came out of which lot.

    Without this row a release could only put the credits back as fresh,
    non-expiring credits — which quietly converts a subscription bundle that
    was about to expire into money the deployment can never reclaim. The
    allocation is what lets the credits go back where they came from, with
    the expiry they came with.
    """

    #: Internal bookkeeping id; allocations are never addressed from outside.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: CASCADE: an allocation has no meaning without the hold that made it.
    hold = models.ForeignKey(
        CreditHold, on_delete=models.CASCADE, related_name="allocations"
    )
    #: PROTECT: the refund walk needs this lot's expiry to still be readable.
    lot = models.ForeignKey(
        CreditLot, on_delete=models.PROTECT, related_name="allocations"
    )
    credits = models.IntegerField(
        help_text=(
            "Credits this hold currently draws from this lot — a partial "
            "capture or a release reduces it, so what remains is what was "
            "actually billed against the lot."
        )
    )
    #: Records the consumption order, which the refund walk reverses.
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_hold_allocation"
        # Consumption order is expiring-soonest first, so the refund walk
        # (which gives back what was NOT used) reverses this ordering.
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["hold"])]

    def __str__(self):
        return f"HoldAllocation({self.hold_id}): {self.credits} from lot {self.lot_id}"


class Subscription(models.Model):
    """Stripe-backed subscription. One per User."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subscription",
        help_text="NULL once the owner has been erased — see stapel_billing.gdpr.",
    )
    user_pseudonym = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text=(
            "The erased owner's stable pseudonym ('erased:<hmac>'). Empty "
            "for a live subscription."
        ),
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
