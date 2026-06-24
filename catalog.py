"""
Credit packages and subscription plan catalogue.

In a future iteration this moves into a Stripe-managed product catalog
(Stripe Products + Prices).  For R1 we keep it in code so the UI has
something to show without depending on Stripe configuration.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CreditPackage:
    slug: str
    name: str
    credits: int
    price_cents: int
    currency: str = "USD"


@dataclass(frozen=True)
class PlanCatalogEntry:
    slug: str
    name: str
    price_cents: int
    monthly_credits_included: int
    storage_limit_bytes: int
    description: str
    currency: str = "USD"


CREDIT_PACKAGES = [
    CreditPackage("starter", "Starter", credits=500, price_cents=500),
    CreditPackage("standard", "Standard", credits=2200, price_cents=2000),
    CreditPackage("bulk", "Bulk", credits=6000, price_cents=5000),
]

CREDIT_PACKAGES_BY_SLUG = {p.slug: p for p in CREDIT_PACKAGES}

PLANS = [
    PlanCatalogEntry(
        slug="free",
        name="Free",
        price_cents=0,
        monthly_credits_included=0,
        storage_limit_bytes=5 * 1024 * 1024 * 1024,
        description="5h upload per month, Fast ASR only, community support.",
    ),
    PlanCatalogEntry(
        slug="pro",
        name="Pro",
        price_cents=1500,
        monthly_credits_included=300,
        storage_limit_bytes=100 * 1024 * 1024 * 1024,
        description="Unlimited upload, Accurate ASR, 300 credits/mo.",
    ),
    PlanCatalogEntry(
        slug="team",
        name="Team",
        price_cents=2500,
        monthly_credits_included=600,
        storage_limit_bytes=500 * 1024 * 1024 * 1024,
        description="Pro plus team roles, 600 credits/mo, SSO.",
    ),
    PlanCatalogEntry(
        slug="enterprise",
        name="Enterprise",
        price_cents=0,
        monthly_credits_included=0,
        storage_limit_bytes=10 * 1024 * 1024 * 1024 * 1024,
        description="Custom pricing.",
    ),
]

PLANS_BY_SLUG = {p.slug: p for p in PLANS}
