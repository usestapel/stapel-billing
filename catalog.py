"""
Credit packages and subscription plan catalogue.

The lists below are *defaults*.  Host projects override them through the
``STAPEL_BILLING`` settings namespace without forking::

    STAPEL_BILLING = {
        "CREDIT_PACKAGES": [
            {"slug": "mini", "name": "Mini", "credits": 100, "price_cents": 200},
        ],
        "PLANS": [
            {"slug": "free", "name": "Free", "price_cents": 0,
             "monthly_credits_included": 0, "storage_limit_bytes": 0,
             "description": "...",
             "entitlements": {"workspaces.org": False,
                              "workspaces.members.max": 5}},
        ],
    }

Entries may be dicts (converted to the dataclasses below) or dataclass
instances.  The module-level names ``CREDIT_PACKAGES`` / ``PLANS`` /
``CREDIT_PACKAGES_BY_SLUG`` / ``PLANS_BY_SLUG`` keep working — they are
lazy views that re-read the configuration on every access.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Callable, Iterator


@dataclass(frozen=True)
class CreditPackage:
    slug: str
    name: str
    credits: int
    price_cents: int
    currency: str = "USD"


@dataclass(frozen=True)
class PlanCatalogEntry:
    """One subscription plan in the catalogue.

    ``entitlements`` maps feature keys to either a ``bool`` (feature
    switch, e.g. ``"workspaces.org": False``) or an ``int`` (numeric
    ceiling, e.g. ``"workspaces.members.max": 5``). Consumed by the
    ``billing.check_entitlement`` comm Function (see ``entitlements.py``).

    A key *absent* from this plan is unrestricted, which is how the upper
    plans express "unlimited". A key absent from the deployment's whole
    vocabulary (``stapel_billing.entitlements.declared_keys``) is a
    different thing and is denied — a host that introduces its own feature
    keys lists them in ``STAPEL_BILLING["ENTITLEMENT_KEYS"]``, so that a
    typo cannot read as "no ceiling configured, go ahead".
    """

    slug: str
    name: str
    price_cents: int
    monthly_credits_included: int
    storage_limit_bytes: int
    description: str
    currency: str = "USD"
    entitlements: dict[str, int | bool] = field(default_factory=dict)


DEFAULT_CREDIT_PACKAGES = [
    CreditPackage("starter", "Starter", credits=500, price_cents=500),
    CreditPackage("standard", "Standard", credits=2200, price_cents=2000),
    CreditPackage("bulk", "Bulk", credits=6000, price_cents=5000),
]

DEFAULT_PLANS = [
    PlanCatalogEntry(
        slug="free",
        name="Free",
        price_cents=0,
        monthly_credits_included=0,
        storage_limit_bytes=5 * 1024 * 1024 * 1024,
        description="5h upload per month, Fast ASR only, community support.",
        entitlements={
            "workspaces.org": False,
            "workspaces.members.max": 5,
            "workspaces.provision_user": False,
        },
    ),
    PlanCatalogEntry(
        slug="pro",
        name="Pro",
        price_cents=1500,
        monthly_credits_included=300,
        storage_limit_bytes=100 * 1024 * 1024 * 1024,
        description="Unlimited upload, Accurate ASR, 300 credits/mo.",
        entitlements={
            "workspaces.org": True,
            "workspaces.members.max": 25,
            "workspaces.provision_user": True,
        },
    ),
    PlanCatalogEntry(
        slug="team",
        name="Team",
        price_cents=2500,
        monthly_credits_included=600,
        storage_limit_bytes=500 * 1024 * 1024 * 1024,
        description="Pro plus team roles, 600 credits/mo, SSO.",
        entitlements={
            "workspaces.org": True,
            "workspaces.members.max": 100,
            "workspaces.provision_user": True,
        },
    ),
    PlanCatalogEntry(
        slug="enterprise",
        name="Enterprise",
        price_cents=0,
        monthly_credits_included=0,
        storage_limit_bytes=10 * 1024 * 1024 * 1024 * 1024,
        description="Custom pricing.",
        # No "workspaces.members.max": an absent key is unrestricted
        # (see PlanCatalogEntry docstring) — enterprise seats are unlimited.
        entitlements={
            "workspaces.org": True,
            "workspaces.provision_user": True,
        },
    ),
]


def _coerce(entry, cls):
    if isinstance(entry, cls):
        return entry
    if isinstance(entry, Mapping):
        return cls(**entry)
    raise TypeError(
        f"catalog entries must be {cls.__name__} instances or dicts, got {entry!r}"
    )


def get_credit_packages() -> list[CreditPackage]:
    """Resolve the credit-package catalogue through STAPEL_BILLING config."""
    from .conf import billing_settings

    raw = billing_settings.CREDIT_PACKAGES
    return [_coerce(e, CreditPackage) for e in raw]


def get_plans() -> list[PlanCatalogEntry]:
    """Resolve the plan catalogue through STAPEL_BILLING config."""
    from .conf import billing_settings

    raw = billing_settings.PLANS
    return [_coerce(e, PlanCatalogEntry) for e in raw]


class _LazyCatalogList(Sequence):
    """Sequence view over a loader — re-reads configuration on access."""

    def __init__(self, loader: Callable[[], list]) -> None:
        self._loader = loader

    def __getitem__(self, index):
        return self._loader()[index]

    def __len__(self) -> int:
        return len(self._loader())

    def __iter__(self) -> Iterator:
        return iter(self._loader())

    def __repr__(self) -> str:
        return repr(self._loader())


class _LazyBySlug(Mapping):
    """Mapping view keyed by slug — re-reads configuration on access."""

    def __init__(self, loader: Callable[[], list]) -> None:
        self._loader = loader

    def _mapping(self) -> dict:
        return {entry.slug: entry for entry in self._loader()}

    def __getitem__(self, key):
        return self._mapping()[key]

    def __iter__(self) -> Iterator:
        return iter(self._mapping())

    def __len__(self) -> int:
        return len(self._mapping())

    def __repr__(self) -> str:
        return repr(self._mapping())


CREDIT_PACKAGES = _LazyCatalogList(get_credit_packages)
PLANS = _LazyCatalogList(get_plans)
CREDIT_PACKAGES_BY_SLUG = _LazyBySlug(get_credit_packages)
PLANS_BY_SLUG = _LazyBySlug(get_plans)
