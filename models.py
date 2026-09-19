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

from .catalog import plan_choices


# =====================================================================
# Provider-owned strings
# =====================================================================

#: Width of every column whose CONTENT the payment provider chooses.
#:
#: Stripe documents object ids as opaque strings of up to 255 characters
#: and reserves the right to grow them; the same is true of the status
#: and event-type strings it sends. A column narrower than that is a
#: decision this library is not entitled to make, and the cost of getting
#: it wrong is not a truncated string — Postgres refuses the INSERT, the
#: webhook answers 500, and the subscription behind it never activates.
PROVIDER_STRING_MAX_LENGTH = 255


class ProviderStringField(models.CharField):
    """A column holding a string the payment provider chose, never we.

    Ids (``sub_``/``cus_``/``in_``/``cs_``/``evt_``…), the raw status
    word, the event type: values we echo, do not generate, and cannot
    bound. They are all 255 by construction, so "how wide should this
    one be?" stops being a per-field judgement call — which is exactly
    the judgement call this class was added to retire, after a
    ``varchar(16)`` copy of a local enum's width was handed the
    provider's raw status and killed a paying customer's activation.

    ``tests/test_provider_string_widths.py`` fails on any new field that
    looks like a provider string and is not one of these.

    Deconstructs as a plain ``CharField`` on purpose: migrations stay
    readable and portable, and nothing in a host's migration history has
    to import this class.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", PROVIDER_STRING_MAX_LENGTH)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, _path, args, kwargs = super().deconstruct()
        return name, "django.db.models.CharField", args, kwargs


# =====================================================================
# Enums
# =====================================================================


class Plan(models.TextChoices):
    """The ladder this library SHIPS — never the test of what a plan is.

    A host sells its own plans through ``STAPEL_BILLING["PLANS"]`` and its
    slugs are absent from here by design, so membership is asked of
    ``catalog.get_plan()``; these members remain because deployments store
    them and code reads them by name.
    """

    FREE = "free", "Free"
    PRO = "pro", "Pro"
    TEAM = "team", "Team"
    ENTERPRISE = "enterprise", "Enterprise"


class SubscriptionStatus(models.TextChoices):
    """The provider's subscription lifecycle, mirrored locally.

    Every Stripe status has a member here. A status the map cannot place
    used to leave the local row at whatever it held before — so a
    subscription that went ``unpaid`` or ``incomplete_expired`` at the
    provider kept reading ``active`` here, and nothing said so.

    ``CANCELLED`` keeps the British spelling it was born with even though
    Stripe writes ``canceled``: the value is in the database of every
    deployment that already runs this app, and renaming it would need a
    data migration to buy nothing. The translation lives in one place
    (``services._map_stripe_status``).
    """

    ACTIVE = "active", "Active"
    TRIALING = "trialing", "Trialing"
    PAST_DUE = "past_due", "Past Due"
    CANCELLED = "cancelled", "Cancelled"
    INCOMPLETE = "incomplete", "Incomplete"
    #: The first invoice was never paid and the window closed. Terminal.
    INCOMPLETE_EXPIRED = "incomplete_expired", "Incomplete Expired"
    #: Retries are exhausted; the provider has stopped collecting. Not
    #: cancelled — the subscription still exists and can be recovered.
    UNPAID = "unpaid", "Unpaid"
    #: Collection is paused (a paused trial, or a pause_collection
    #: schedule). Service is not owed while it lasts.
    PAUSED = "paused", "Paused"


#: Statuses that entitle the subscriber to the plan right now. Read by
#: ``Subscription.is_active`` and by every host that asks "may this user
#: have the plan" — one list, because two copies disagree the day a status
#: is added.
ENTITLING_SUBSCRIPTION_STATUSES = frozenset(
    {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING}
)


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


class DebtReason(models.TextChoices):
    """Why a wallet owes credits it never had.

    The two ways a balance can legitimately go below zero are opposite in
    time — one is service handed out before it was paid for, the other is
    payment taken back after the credits were spent — and a support answer
    ("why does this wallet owe 40") has to be able to tell them apart.
    """

    #: :func:`services.debit` was called with ``allow_partial=True`` and the
    #: wallet could not cover the charge. The work was served anyway.
    PARTIAL_DEBIT = "partial_debit", "Partial Debit"
    #: A refund, dispute or credit note took back money whose credits were
    #: already spent, so there was nothing left to claw back.
    CLAWBACK = "clawback", "Clawback"


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
    merged_into = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="merged_wallets",
        help_text=(
            "The wallet this one's credits and ledger were moved into when "
            "its owner was folded into another account (auth's user.merged). "
            "Non-NULL means this wallet is CLOSED at zero: it is kept as the "
            "record that the guest's credits existed and where they went, "
            "not deleted. NULL for every live wallet."
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
        permissions = [
            # "May look at wallets" and "may move credits" are different
            # rights, and until 0.17.0 they were the same one by accident.
            #
            # Django does not filter a custom admin action by permission
            # unless the action declares `allowed_permissions`, so the
            # Grant-credits action was available to anyone who could see the
            # changelist. A deployment that granted its operators
            # `view_wallet` — deliberately narrow, no add/change/delete, so
            # that nobody could hand-edit a balance — handed them the ability
            # to create credits out of nothing in the same breath, and could
            # not separate the two without taking the changelist away too.
            #
            # A model-level permission rather than a settings flag: it is the
            # thing an operator is granted, it belongs in the same fixture as
            # every other permission they hold, and `has_perm` is where a
            # reviewer already looks.
            ("grant_credits", "Can grant credits by hand"),
        ]

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
    idempotency_key = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "The caller's retry key, mirrored out of metadata into an "
            "indexed column. Empty when the caller supplied none. The "
            "duplicate lookup in services.credit/debit reads this column; "
            "metadata['idempotency_key'] stays the compatible spelling for "
            "rows written before 0.11.0."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_transaction"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["wallet", "-created_at"]),
            models.Index(fields=["type"]),
            # The idempotency short-circuit, which every debit and credit
            # runs before it moves a credit. Until 0.11.0 the only spelling
            # of the key was ``metadata->>'idempotency_key'``, so the guard
            # scanned the wallet's whole transaction history on each charge —
            # an unbounded, ever-growing table for the busiest wallets.
            models.Index(
                fields=["wallet", "idempotency_key"],
                name="billing_txn_wallet_idem_idx",
            ),
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


class CreditDebt(models.Model):
    """Credits a wallet owes: service already given, or money taken back.

    A balance is a count of credits that exist, and credits that were spent
    do not come back into existence because a charge failed — so "this
    wallet is 40 credits short" cannot be expressed by driving the balance
    negative without making every lot, every expiry and every
    ``balance_after`` in the ledger a lie. The shortfall is its own row
    instead: the lots stay a truthful record of what was alive, and the debt
    stays a truthful record of what was consumed without cover.

    Two writers create these (see :class:`DebtReason`): a partial debit,
    where the consumer chose to serve the work rather than refuse it, and a
    clawback, where a refund arrived after the granted credits were spent.

    Debts are collected, not chased: the next :func:`services.credit` into
    this wallet settles them oldest-first before the credits become
    spendable, under the same row lock every other operation takes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: PROTECT, like Transaction: an unpaid debt is not disposable data.
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="debts")
    credits_initial = models.IntegerField(help_text="Credits this debt was opened for.")
    credits_outstanding = models.IntegerField(
        help_text="Credits still owed. 0 once settled_at is set."
    )
    reason = models.CharField(
        max_length=16,
        choices=DebtReason.choices,
        help_text="Why the wallet owes this (see DebtReason).",
    )
    type = models.CharField(
        max_length=32,
        choices=TransactionType.choices,
        help_text=(
            "Transaction type the settlement is billed under — the type the "
            "uncovered charge would have carried."
        ),
    )
    description = models.CharField(max_length=255, null=True, blank=True)
    #: Caller context; carried onto the settlement rows.
    metadata = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="The debiting caller's retry key, when it supplied one.",
    )
    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="debts",
        help_text="The ledger row that recorded the uncovered part.",
    )
    settled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the last outstanding credit was collected. NULL = open.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_credit_debt"
        # Oldest first: the collection order, and the order a support answer
        # reads them in.
        ordering = ["created_at", "id"]
        indexes = [
            # The collection walk: one wallet's open debts, oldest first.
            models.Index(fields=["wallet", "settled_at", "created_at"]),
            models.Index(fields=["wallet", "idempotency_key"]),
        ]

    def __str__(self):
        state = "settled" if self.settled_at else "open"
        return f"CreditDebt({self.reason}, {state}): {self.credits_outstanding}/{self.credits_initial}"


class Subscription(models.Model):
    """Stripe-backed subscription. One per User.

    A row exists for every user the API has ever been asked about,
    including the ones who never paid: ``GET /subscription`` creates the
    free-plan row on first read. That row is NOT a subscription in the
    provider's sense — it has no ``stripe_subscription_id``, no period and
    no money behind it — and ``status`` says ``active`` only because
    "active" is the default of a column that mirrors a provider object
    this row does not have.

    So ``status`` alone cannot answer the two questions every caller
    actually asks. :attr:`is_paid` ("is there a provider subscription
    behind this at all") and :attr:`is_active` ("does it entitle the user
    right now") do, and they are the fields the API publishes. A client
    that reasons from ``plan``/``status`` reinvents them, and gets the free
    plan wrong — which is how a "Cancel subscription" button came to be
    offered to 111 accounts that had nothing to cancel.
    """

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
    #: ``choices`` is the CONFIGURED catalogue, not the shipped enum: a
    #: host's own slug is stored here today (the ORM never checks choices
    #: on save) and a form that offered only the enum would refuse to edit
    #: the row it is showing. The default stays the shipped free plan —
    #: boot check E102 makes a host that has no such plan say so.
    plan = models.CharField(max_length=16, choices=plan_choices, default=Plan.FREE)
    status = models.CharField(
        max_length=32,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.ACTIVE,
        help_text=(
            "Mirrors the provider's subscription status. 32 chars because "
            "'incomplete_expired' is 18 — the old 16 could not hold it."
        ),
    )
    stripe_subscription_id = ProviderStringField(null=True, blank=True)
    stripe_customer_id = ProviderStringField(null=True, blank=True)
    last_provider_event_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Provider-clock time of the newest lifecycle event already "
            "applied to this row (the event's `created`, or the moment a "
            "reconcile re-read the provider). Stripe does not promise "
            "delivery order: without this, a `customer.subscription.created` "
            "carrying `incomplete` that arrives AFTER an `.updated` carrying "
            "`active` overwrites the live plan with the older word, and the "
            "paying subscriber is shown the paywall. NULL means 'nothing "
            "applied yet — accept the next event'."
        ),
    )
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(
        default=False,
        help_text=(
            "The subscriber asked to stop and the provider will not renew. "
            "Stripe keeps `status` at 'active' for the whole paid-for "
            "remainder — service is owed until current_period_end — so this "
            "flag is the ONLY thing that distinguishes 'subscribed' from "
            "'leaving on the 30th'. Without it a UI must either keep "
            "offering Cancel to someone who already cancelled, or cut off a "
            "period the customer paid for."
        ),
    )
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
        # The gate is WHO, never a setting: comp time is service given away,
        # so it is its own permission rather than a side effect of being
        # able to open the changelist (same canon as Wallet.grant_credits).
        permissions = [("extend_subscription", "Can give comp subscription time")]

    def __str__(self):
        return f"{self.user_id}: {self.plan} ({self.status})"

    @property
    def is_paid(self) -> bool:
        """Is there a provider subscription behind this row at all?

        Both halves are required. ``plan != free`` alone admits a row a
        failed checkout left on a paid plan with no provider object; a
        Stripe id alone admits a row whose plan was moved back to free.
        Neither is something to offer a cancel button for.
        """
        default_plan = self._meta.get_field("plan").get_default()
        return bool(self.plan != default_plan and self.stripe_subscription_id)

    @property
    def is_active(self) -> bool:
        """Does this subscription entitle the user *right now*?

        Status first, then the clock: a row whose period ended and whose
        renewal never arrived reads ``active`` forever otherwise, and
        that is the exact shape of a webhook this deployment missed. A
        row with no period end is trusted on its status alone — that is
        the honest answer when nothing told us when it runs out.
        """
        if self.status not in ENTITLING_SUBSCRIPTION_STATUSES:
            return False
        if self.current_period_end is not None:
            from django.utils import timezone

            return self.current_period_end > timezone.now()
        return True


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
    #: One provider event that takes money back (refund, dispute, credit
    #: note). The clawback it drives is a spend, so a redelivery must not
    #: run it twice.
    SCOPE_CLAWBACK = "clawback"
    #: One (wallet, plan, period) bundle granted without a provider —
    #: :func:`services.grant_plan_bundle`. ``provider`` is ``"local"``
    #: there: nothing external issued it, and the deduplication is the
    #: same problem regardless.
    SCOPE_PLAN_BUNDLE = "plan_bundle"

    #: One notification already sent about a thing — see
    #: :mod:`stapel_billing.notifications`. Taken under
    #: ``provider=PROVIDER_NOTIFY`` so the claim "we told the customer about
    #: invoice X" can never collide with the claim "we granted credits for
    #: invoice X": the two are different questions about the same
    #: identifier, and sharing a row would make a granted invoice silently
    #: unnotifiable (and vice versa).
    SCOPE_NOTIFY_PAYMENT = "notify_payment"
    SCOPE_NOTIFY_PAYMENT_FAILED = "notify_payment_failed"
    SCOPE_NOTIFY_SUBSCRIPTION_ENDING = "notify_subscription_ending"

    #: The pseudo-provider the notification claims above are taken under.
    #: Same precedent as ``"local"`` for SCOPE_PLAN_BUNDLE: the column
    #: names whose namespace the ``external_id`` lives in, and a
    #: send-once claim lives in its own.
    PROVIDER_NOTIFY = "notify"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=32, default="stripe")
    scope = models.CharField(max_length=32)
    external_id = ProviderStringField()
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
class PendingSubscriptionPeriod(models.Model):
    """A billing period that arrived before the subscription it belongs to.

    Stripe does not promise event order. ``customer.subscription.created``
    can land before the ``checkout.session.completed`` that creates the
    local row, and until 0.11.0 the handler simply returned when it found
    no row — throwing away the only payload that carries the period. The
    checkout then granted an UNDATED subscription lot, and the event that
    would have dated it had already been consumed and marked processed, so
    the bundle never expired: a cancelled subscriber kept it forever.

    A stash is not a second source of truth — it is the same fact, parked
    under the provider id it names, and deleted the moment the local
    subscription claims it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stripe_subscription_id = ProviderStringField(unique=True)
    stripe_customer_id = ProviderStringField(blank=True, default="")
    #: The provider's RAW status word — not our mapped enum, which is why
    #: it is a provider string and not a 16/32-wide local column. It was
    #: born as a copy of Subscription.status's then-16, and when 0.13.0
    #: widened THAT field to hold 'incomplete_expired' (18 chars) this
    #: copy was left behind: on Postgres the stash INSERT then failed with
    #: StringDataRightTruncation, the webhook answered 500, and the
    #: subscription whose period it was carrying never activated. 0.19.1.
    status = ProviderStringField(
        blank=True,
        default="",
        help_text=(
            "Provider-side status string, applied when the row lands. "
            "Stored raw and full-width: the provider picks this word."
        ),
    )
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    provider_event_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Provider-clock time of the event this stash was written from. "
            "A stash is overwritten only by a NEWER event, and applied only "
            "if it is newer than what the subscription already carries — "
            "out-of-order delivery must not park a stale period either."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_pending_subscription_period"
        indexes = [models.Index(fields=["stripe_customer_id"])]

    def __str__(self):
        return f"PendingSubscriptionPeriod({self.stripe_subscription_id})"


@access.ops
class StripeWebhookEvent(models.Model):
    """Idempotency log for incoming Stripe webhooks."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stripe_event_id = ProviderStringField(unique=True)
    #: Also the provider's own string. 64 has never truncated a Stripe
    #: event type, but "has not yet" is not a width — the provider adds
    #: event types without asking, and this column is on the path every
    #: webhook takes.
    event_type = ProviderStringField()
    payload = models.JSONField()
    processed_at = models.DateTimeField(null=True, blank=True)
    ignored_stale = models.BooleanField(
        default=False,
        help_text=(
            "The event was accepted and acknowledged, but its lifecycle "
            "payload was OLDER than what had already been applied, so it "
            "was not applied. Processed and ignored is a third outcome: "
            "without it, 'processed_at is set' would claim the row reflects "
            "this event when it deliberately does not."
        ),
    )
    error = models.TextField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_stripe_webhook_event"
        indexes = [models.Index(fields=["event_type", "-received_at"])]


@access.ops
class CompPeriod(models.Model):
    """Subscription time the operator gave away, not the provider sold.

    "Put this customer back on Pro for a month, on us" had no sanctioned
    shape before 0.20.0. What people reached for was
    ``current_period_end``, which is a MIRROR of the provider's period:
    the next subscription webhook re-reads it from Stripe and the comp
    quietly disappears, usually days later, usually noticed by the
    customer first.

    A comp period is its own row, so nothing the provider says can erase
    it, and it is honoured by ``entitlements`` only while the subscription
    itself is not entitling — the paid plan governs whenever there is one,
    and comp time is what carries the account after the provider period
    lapses.

    Written by ``services.extend_subscription`` (the management command
    ``billing_extend_subscription`` and the admin action are both callers
    of that one function), never by hand: the row records WHO granted it,
    WHEN and WHY, and those three are the reason it is allowed to exist.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: CASCADE on purpose: a comp period is an attribute of the local
    #: subscription row, and carries no personal data of its own — the
    #: subject's name lives on Subscription/Wallet, which erasure handles.
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="comp_periods",
        help_text="The local subscription this comp time belongs to.",
    )
    plan = models.CharField(
        max_length=16,
        # Same column shape as Subscription.plan, deliberately: the slug
        # written here is copied from that row (or named on the command
        # line), so a comp window must be able to hold every plan a
        # subscription can — the host's catalogue, not the shipped enum.
        choices=plan_choices,
        help_text=(
            "The plan the comp window entitles to. Stored rather than read "
            "from the subscription: the comp is usually granted BECAUSE the "
            "subscription stopped saying 'pro', and a window that resolved "
            "its plan at read time would hand back the free one."
        ),
    )
    starts_at = models.DateTimeField(help_text="When the comp window opens.")
    ends_at = models.DateTimeField(help_text="When the comp window closes.")
    reason = models.CharField(
        max_length=255,
        help_text="Why this was given. Required — an unexplained comp is the row an audit stops on.",
    )
    granted_by = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="The operator who granted it (admin username, or the shell user for a command run).",
    )
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set to withdraw the window without deleting the record of it.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_comp_period"
        indexes = [models.Index(fields=["subscription", "-ends_at"])]

    def __str__(self):
        return f"CompPeriod({self.subscription_id}: {self.plan} until {self.ends_at})"

    def is_live(self, now=None) -> bool:
        """Is this window open right now?"""
        from django.utils import timezone

        now = now or timezone.now()
        return (
            self.revoked_at is None and self.starts_at <= now < self.ends_at
        )
