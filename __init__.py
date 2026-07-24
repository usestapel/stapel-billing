"""stapel-billing — wallets, credit ledger and pluggable payment providers.

Public API (lazily resolved, PEP 562 — importing this package pulls in
no Django code until an attribute is actually accessed):

    billing_settings          — the ``STAPEL_BILLING`` settings namespace
    credit                    — add credits to a user's wallet
    debit                     — deduct credits (raises InsufficientCreditsError)
    get_provider              — instantiate the configured PaymentProvider
    PaymentProvider           — base class for custom payment backends
    InsufficientCreditsError  — raised by ``debit`` on insufficient balance
    CHECK_ENTITLEMENT         — name of the ``billing.check_entitlement``
                                comm Function (call via stapel_core.comm.call)
    DEBIT                     — name of the ``billing.debit`` comm Function
    check_entitlement         — the ``billing.check_entitlement`` provider

(The ``billing.debit`` provider itself is ``entitlements.debit`` — not
re-exported here because the package-level ``debit`` name is the service
function; callers use ``comm.call(DEBIT, ...)`` anyway.)
"""

__all__ = [
    "CHECK_ENTITLEMENT",
    "DEBIT",
    "InsufficientCreditsError",
    "PaymentProvider",
    "billing_settings",
    "check_entitlement",
    "credit",
    "debit",
    "get_provider",
]

# name -> (relative module, attribute)
_EXPORTS = {
    "billing_settings": (".conf", "billing_settings"),
    "credit": (".services", "credit"),
    "debit": (".services", "debit"),
    "get_provider": (".services", "get_provider"),
    "InsufficientCreditsError": (".services", "InsufficientCreditsError"),
    "PaymentProvider": (".providers.base", "PaymentProvider"),
    "CHECK_ENTITLEMENT": (".entitlements", "CHECK_ENTITLEMENT"),
    "DEBIT": (".entitlements", "DEBIT"),
    "check_entitlement": (".entitlements", "check_entitlement"),
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
