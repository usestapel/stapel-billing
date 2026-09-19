"""The plan catalogues the suite runs on: host-shaped, not enum-shaped.

A deployment's plan ladder is its own. Hosts configure it through
``STAPEL_BILLING["PLANS"]`` and their slugs are absent from the ``Plan``
enum this library ships — which is why a suite whose every plan is an enum
member cannot see a gate spelled ``slug in Plan.values``. One such gate
(``services.extend_subscription``, 0.20.0) refused to comp any real
customer of such a host, and every test passed.

So the suite's catalogue is the four shipped plans PLUS ``starter``, a slug
the enum does not have: commands, serializers, the admin forms and the
entitlement surface are all exercised against a plan the library does not
know by name.

ORDER IS THE LADDER (``catalog.plan_rank``), lowest tier first, which is
why ``starter`` is inserted between free and pro rather than appended.
"""

from stapel_billing.catalog import DEFAULT_PLANS, PlanCatalogEntry

#: A plan slug that exists only in configuration — never in ``models.Plan``.
HOST_PLAN = "starter"

#: A numeric entitlement the shipped ladder also declares, so the host plan
#: can be told apart by its ceiling: free allows 5, this one allows 7.
SEATS_KEY = "workspaces.members.max"

#: A boolean entitlement free denies and the host plans grant.
HOST_PLAN_KEY = "workspaces.org"

HOST_PLAN_ENTRY = PlanCatalogEntry(
    slug=HOST_PLAN,
    name="Starter",
    price_cents=900,
    monthly_credits_included=0,
    storage_limit_bytes=20 * 1024 * 1024 * 1024,
    description="A plan this library does not ship — the host sells it.",
    entitlements={
        HOST_PLAN_KEY: True,
        SEATS_KEY: 7,
        "workspaces.provision_user": False,
    },
)


def host_style_plans() -> list[PlanCatalogEntry]:
    """The shipped ladder with one host-defined plan on it (above free)."""
    plans = list(DEFAULT_PLANS)
    free_index = next(i for i, p in enumerate(plans) if p.slug == "free")
    plans.insert(free_index + 1, HOST_PLAN_ENTRY)
    return plans


# ─── A ladder that is ENTIRELY the host's ──────────────────────────────
#
# starter < growth < business: not one of them is in ``models.Plan``, and
# the differences between them are the numbers the upgrade path works in —
# the entitlement ceiling that must move when a comp governs, and the
# bundled credits whose DIFFERENCE `--grant-bundle` hands over.
#
# ``free`` stays at the bottom because it is ``Subscription.plan``'s field
# default, and boot check E102 refuses a deployment whose catalogue has no
# entry for the plan every user falls back to.

UPGRADE_LADDER_SLUGS = ("starter", "growth", "business")

STARTER_CREDITS = 1080
GROWTH_CREDITS = 2400
BUSINESS_CREDITS = 5000


def _tier(slug, name, price, credits, seats, provision=True):
    return PlanCatalogEntry(
        slug=slug,
        name=name,
        price_cents=price,
        monthly_credits_included=credits,
        storage_limit_bytes=50 * 1024 * 1024 * 1024,
        description=f"{name} — a plan only this host sells.",
        entitlements={
            HOST_PLAN_KEY: True,
            SEATS_KEY: seats,
            "workspaces.provision_user": provision,
        },
    )


def upgrade_ladder_plans() -> list[PlanCatalogEntry]:
    """free < starter < growth < business, in ladder order."""
    free = next(p for p in DEFAULT_PLANS if p.slug == "free")
    return [
        free,
        _tier("starter", "Starter", 1000, STARTER_CREDITS, 3, provision=False),
        _tier("growth", "Growth", 2500, GROWTH_CREDITS, 7),
        _tier("business", "Business", 6000, BUSINESS_CREDITS, 50),
    ]
