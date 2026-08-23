"""DTOs for the billing API."""

from dataclasses import dataclass, field
from typing import List, Optional
from uuid import UUID


@dataclass
class CreditLotResponse:
    """One live batch of credits — where it came from, when it dies.

    A lot only ever shrinks, so ``credits_remaining`` against
    ``credits_initial`` is how much of this batch is already gone.

    Attributes:
        id: Lot UUID; the same id travels in transaction metadata.
        credits_remaining: Credits still unspent and unreserved.
        credits_initial: Credits the lot was created with.
        source: purchase, subscription, grant, adjustment or hold_release.
        expires_at: ISO 8601 deadline; null = these credits never expire.
        created_at: ISO 8601.
    """

    id: UUID
    credits_remaining: int
    credits_initial: int
    source: str
    expires_at: Optional[str]
    created_at: str


@dataclass
class CreditHoldResponse:
    """A reservation: credits already out of the spendable balance.

    A hold is a real withdrawal, not an overlay — its credits are gone from
    ``balance`` from the moment it is created, and come back only if it is
    released or expires.

    Attributes:
        id: Hold UUID; also the capture/release handle.
        credits: Credits reserved by this hold.
        type: Transaction type the capture will be billed under.
        description: Caller-supplied label carried onto the capture, or null.
        status: held, captured, released or expired.
        expires_at: ISO 8601 deadline expire_holds sweeps; null = never swept.
        created_at: ISO 8601.
    """

    id: UUID
    credits: int
    type: str
    description: Optional[str]
    status: str
    expires_at: Optional[str]
    created_at: str


@dataclass
class ExpiringCreditsResponse:
    """The wallet's nearest credit deadline.

    Attributes:
        credits: Credits in the earliest-expiring live lot.
        expires_at: ISO 8601 — when they die.
    """

    credits: int
    expires_at: str


@dataclass
class WalletResponse:
    """Wallet snapshot.

    Attributes:
        user_id: Wallet owner UUID.
        balance: Spendable integer credits — live reservations already out.
        currency: ISO 4217 currency code.
        auto_recharge_enabled: Auto-recharge flag.
        auto_recharge_threshold: Trigger balance.
        auto_recharge_package: Package slug used for top-up.
        low_balance_alert: Alert threshold.
        updated_at: ISO 8601.
        lots: Live lots in spend order — expiring soonest, non-expiring last.
        holds: Open reservations (status=held); settled holds are history.
        expiring_soon: The earliest-expiring live lot; null if none expires.
    """

    user_id: UUID
    balance: int
    currency: str
    auto_recharge_enabled: bool
    auto_recharge_threshold: int
    auto_recharge_package: Optional[str]
    low_balance_alert: int
    updated_at: str
    lots: List[CreditLotResponse] = field(default_factory=list)
    holds: List[CreditHoldResponse] = field(default_factory=list)
    expiring_soon: Optional[ExpiringCreditsResponse] = None


@dataclass
class WalletUpdateRequest:  # noqa: R004
    auto_recharge_enabled: Optional[bool] = None
    auto_recharge_threshold: Optional[int] = None
    auto_recharge_package: Optional[str] = None
    low_balance_alert: Optional[int] = None


@dataclass
class TransactionResponse:  # noqa: R004
    id: UUID
    type: str
    amount_cents: Optional[int]
    credits_delta: int
    balance_after: int
    description: Optional[str]
    metadata: dict
    created_at: str


@dataclass
class TransactionListResponse:  # noqa: R004
    transactions: List[TransactionResponse] = field(default_factory=list)
    next_cursor: Optional[str] = None
    has_more: bool = False


@dataclass
class PackageResponse:  # noqa: R004
    slug: str
    name: str
    credits: int
    price_cents: int
    currency: str


@dataclass
class PlanResponse:  # noqa: R004
    slug: str
    name: str
    price_cents: int
    currency: str
    monthly_credits_included: int
    storage_limit_bytes: int
    description: str


@dataclass
class CatalogResponse:  # noqa: R004
    packages: List[PackageResponse] = field(default_factory=list)
    plans: List[PlanResponse] = field(default_factory=list)


@dataclass
class CheckoutRequest:
    """Initiate a Stripe Checkout session.

    Attributes:
        package: Credit-package slug (one-time top-up).
        plan: Subscription plan slug (recurring); mutually exclusive with package.
        success_url: Where Stripe redirects on success.
        cancel_url: Where Stripe redirects on cancel.
    """

    package: Optional[str] = None
    plan: Optional[str] = None
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


@dataclass
class CheckoutResponse:  # noqa: R004
    checkout_url: str
    session_id: str


@dataclass
class SubscriptionResponse:  # noqa: R004
    plan: str
    status: str
    stripe_subscription_id: Optional[str]
    current_period_start: Optional[str]
    current_period_end: Optional[str]
    cancelled_at: Optional[str]


@dataclass
class CustomerPortalResponse:  # noqa: R004
    portal_url: str


@dataclass
class CreditDebitRequest:
    """Service-to-service debit (transcription/AI charge).

    Attributes:
        idempotency_key: Optional caller-supplied key; retries with the
            same key return the original transaction instead of debiting
            twice.
    """

    user_id: UUID
    credits: int
    type: str
    description: Optional[str] = None
    metadata: Optional[dict] = None
    idempotency_key: Optional[str] = None


@dataclass
class CreditOperationResponse:  # noqa: R004
    transaction_id: UUID
    balance_after: int
