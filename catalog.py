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


def get_plan(slug) -> PlanCatalogEntry | None:
    """The configured plan with this slug, or ``None``.

    The ONE answer to "is this a plan?" in the library. The ``Plan`` enum
    in ``models`` is the *shipped* ladder, not the deployment's: a host
    that sells ``starter``/``growth``/``scale`` configures them in
    ``STAPEL_BILLING["PLANS"]`` and never appears in that enum, so any
    gate spelled ``slug in Plan.values`` refuses the host's real
    customers (it refused every comp grant on such a host until 0.21.0).
    Ask here instead — catalogue membership is a configuration question.
    """
    if not slug:
        return None
    for entry in get_plans():
        if entry.slug == slug:
            return entry
    return None


def plan_slugs() -> list[str]:
    """Every configured plan slug, in catalogue order — for error messages."""
    return [entry.slug for entry in get_plans()]


def plan_rank(slug) -> int | None:
    """Where this plan sits on the deployment's ladder, or ``None``.

    THE LADDER IS THE ORDER THE CATALOGUE IS CONFIGURED IN — position in
    ``STAPEL_BILLING["PLANS"]``, lowest tier first. Nothing is derived from
    price or bundled credits: an enterprise plan is priced ``0`` here
    because it is invoiced, and a plan can legitimately cost more while
    bundling fewer credits, so either derivation would rank somebody's
    ladder upside down without saying so. A list is something a host
    writes once and can read back.

    Used to decide which of two plans governs when a comp window and a
    paid provider subscription are both live (``services.effective_plan``).
    """
    for index, entry in enumerate(get_plans()):
        if entry.slug == slug:
            return index
    return None


def plan_choices() -> list[tuple[str, str]]:
    """Model-field ``choices`` over the CONFIGURED catalogue.

    Passed to the ``plan`` columns as a *callable*, so the choices are the
    deployment's plans at the moment they are asked for rather than the
    shipped enum frozen at import. A model form (Django admin) validates
    against these, and a host slug is a legal value there for the same
    reason it is a legal value everywhere else.
    """
    return [(entry.slug, entry.name) for entry in get_plans()]


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
