"""stapel-billing — wallets, credit ledger and pluggable payment providers.

Public API (lazily resolved, PEP 562 — importing this package pulls in
no Django code until an attribute is actually accessed):

    billing_settings          — the ``STAPEL_BILLING`` settings namespace
    credit                    — add credits to a user's wallet
    debit                     — deduct credits (raises InsufficientCreditsError,
                                or charges partially and records the debt)
    can_afford                — read-only: would a charge of N go through?
    grant_plan_bundle         — grant one period's plan bundle without a
                                payment provider (the free-tier path)
    claw_back_grant           — take back a refunded/disputed grant's credits
    hold                      — reserve credits before work of unknown cost
    capture                   — bill a hold for what the work actually cost
    release                   — hand a hold's credits back, expiry preserved
    get_provider              — instantiate the configured PaymentProvider
    PaymentProvider           — base class for custom payment backends
    InsufficientCreditsError  — raised by ``debit`` on insufficient balance
    HoldKeyResolvedError      — raised by ``hold`` when the idempotency key
                                names a hold that is already over
    CHECK_ENTITLEMENT         — name of the ``billing.check_entitlement``
                                comm Function (call via stapel_core.comm.call)
    DEBIT                     — name of the ``billing.debit`` comm Function
    CAN_AFFORD                — name of the ``billing.can_afford`` Function
    HOLD / CAPTURE / RELEASE  — names of the reservation comm Functions
    check_entitlement         — the ``billing.check_entitlement`` provider
    get_stripe_handler        — the handler the Stripe webhook registry
                                resolves for one event type
    stripe_handlers           — the effective registry (settings merged
                                over ``BUILTIN_STRIPE_HANDLERS``)

(The ``billing.debit`` provider itself is ``entitlements.debit`` — not
re-exported here because the package-level ``debit`` name is the service
function; callers use ``comm.call(DEBIT, ...)`` anyway.)
"""

__all__ = [
    "CAN_AFFORD",
    "CAPTURE",
    "CHECK_ENTITLEMENT",
    "DEBIT",
    "HOLD",
    "Affordability",
    "ClawbackResult",
    "DebitResult",
    "HoldKeyResolvedError",
    "HoldNotFoundError",
    "HoldStateError",
    "InsufficientCreditsError",
    "PaymentProvider",
    "RELEASE",
    "billing_settings",
    "can_afford",
    "capture",
    "check_entitlement",
    "claw_back_grant",
    "credit",
    "debit",
    "get_provider",
    "get_stripe_handler",
    "grant_plan_bundle",
    "hold",
    "release",
    "stripe_handlers",
]

# name -> (relative module, attribute)
_EXPORTS = {
    "billing_settings": (".conf", "billing_settings"),
    "credit": (".services", "credit"),
    "debit": (".services", "debit"),
    "get_provider": (".services", "get_provider"),
    "hold": (".services", "hold"),
    "capture": (".services", "capture"),
    "release": (".services", "release"),
    "can_afford": (".services", "can_afford"),
    "grant_plan_bundle": (".services", "grant_plan_bundle"),
    "claw_back_grant": (".services", "claw_back_grant"),
    "Affordability": (".services", "Affordability"),
    "ClawbackResult": (".services", "ClawbackResult"),
    "DebitResult": (".services", "DebitResult"),
    "InsufficientCreditsError": (".services", "InsufficientCreditsError"),
    "HoldNotFoundError": (".services", "HoldNotFoundError"),
    "HoldStateError": (".services", "HoldStateError"),
    "HoldKeyResolvedError": (".services", "HoldKeyResolvedError"),
    "PaymentProvider": (".providers.base", "PaymentProvider"),
    "CHECK_ENTITLEMENT": (".entitlements", "CHECK_ENTITLEMENT"),
    "DEBIT": (".entitlements", "DEBIT"),
    "HOLD": (".entitlements", "HOLD"),
    "CAPTURE": (".entitlements", "CAPTURE"),
    "RELEASE": (".entitlements", "RELEASE"),
    "CAN_AFFORD": (".entitlements", "CAN_AFFORD"),
    "check_entitlement": (".entitlements", "check_entitlement"),
    "get_stripe_handler": (".webhooks", "get_stripe_handler"),
    "stripe_handlers": (".webhooks", "stripe_handlers"),
}


def __getattr__(name):
    try:
        module_path, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None
    from importlib import import_module

    value = getattr(import_module(module_path, __name__), attr)
    globals()[name] = value  # cache: subsequent lookups skip __getattr__
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
