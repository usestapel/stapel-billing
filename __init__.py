"""stapel-billing — wallets, credit ledger and pluggable payment providers.

Public API (lazily resolved, PEP 562 — importing this package pulls in
no Django code until an attribute is actually accessed):

    billing_settings          — the ``STAPEL_BILLING`` settings namespace
    credit                    — add credits to a user's wallet
    debit                     — deduct credits (raises InsufficientCreditsError)
    hold                      — reserve credits before work of unknown cost
    capture                   — bill a hold for what the work actually cost
    release                   — hand a hold's credits back, expiry preserved
    get_provider              — instantiate the configured PaymentProvider
    PaymentProvider           — base class for custom payment backends
    InsufficientCreditsError  — raised by ``debit`` on insufficient balance
    CHECK_ENTITLEMENT         — name of the ``billing.check_entitlement``
                                comm Function (call via stapel_core.comm.call)
    DEBIT                     — name of the ``billing.debit`` comm Function
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
    "CAPTURE",
    "CHECK_ENTITLEMENT",
    "DEBIT",
    "HOLD",
    "HoldNotFoundError",
    "HoldStateError",
    "InsufficientCreditsError",
    "PaymentProvider",
    "RELEASE",
    "billing_settings",
    "capture",
    "check_entitlement",
    "credit",
    "debit",
    "get_provider",
    "get_stripe_handler",
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
    "InsufficientCreditsError": (".services", "InsufficientCreditsError"),
    "HoldNotFoundError": (".services", "HoldNotFoundError"),
    "HoldStateError": (".services", "HoldStateError"),
    "PaymentProvider": (".providers.base", "PaymentProvider"),
    "CHECK_ENTITLEMENT": (".entitlements", "CHECK_ENTITLEMENT"),
    "DEBIT": (".entitlements", "DEBIT"),
    "HOLD": (".entitlements", "HOLD"),
    "CAPTURE": (".entitlements", "CAPTURE"),
    "RELEASE": (".entitlements", "RELEASE"),
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
