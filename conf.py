"""Settings namespace for stapel-billing.

All configuration is read through ``billing_settings`` (lazily, at call
time) instead of module-level ``os.getenv`` — so tests and host projects
can override any key via ``settings.STAPEL_BILLING``, a flat Django
setting of the same name, or an environment variable::

    STAPEL_BILLING = {
        "PAYMENT_PROVIDER": "myproject.billing.MyProvider",
        "STRIPE_SECRET_KEY": "sk_live_...",
        "CREDIT_PACKAGES": [
            {"slug": "mini", "name": "Mini", "credits": 100, "price_cents": 200},
        ],
        "PLANS": [...],
    }

``PAYMENT_PROVIDER`` is resolved with import_string — the dotted-path
escape hatch that makes the payment backend swappable without forking.
"""
from stapel_core.conf import AppSettings

from . import catalog as _catalog

#: AppSettings-shaped literal dict (capability-config.md §2): a top-level
#: DEFAULTS lets the capabilities.json emitter introspect axis keys/kinds
#: without re-parsing the AppSettings() call.
DEFAULTS = {
    # Dotted path to a stapel_billing.providers.base.PaymentProvider subclass.
    "PAYMENT_PROVIDER": "stapel_billing.providers.stripe.StripeProvider",
    # Read lazily at call time (never frozen at import).
    "STRIPE_SECRET_KEY": "",
    "STRIPE_WEBHOOK_SECRET": "",
    # Catalog overrides: lists of dicts (converted to the catalog
    # dataclasses) or dataclass instances. Defaults live in catalog.py.
    "CREDIT_PACKAGES": _catalog.DEFAULT_CREDIT_PACKAGES,
    "PLANS": _catalog.DEFAULT_PLANS,
    # Fallback redirect targets for Stripe Checkout / customer portal,
    # used when the request does not carry them. Empty by default;
    # when unset the fallback is derived from the flat FRONTEND_URL
    # setting, and with neither configured the request is rejected
    # with error.400.redirect_url_not_configured (no placeholder
    # domains, ever).
    "CHECKOUT_SUCCESS_URL": "",
    "CHECKOUT_CANCEL_URL": "",
    "PORTAL_RETURN_URL": "",
    # Exact origins ("https://app.example.com") a REQUEST-supplied redirect
    # target may point at, on top of the origins of the three fallbacks
    # above and of FRONTEND_URL. See redirects.py.
    "REDIRECT_ALLOWED_ORIGINS": [],
    # Escape hatch: forward request-supplied redirect URLs to the provider
    # unchecked. That is an authenticated open-redirect surface — a host
    # only turns it on to buy time while it declares its origins.
    "ALLOW_UNVALIDATED_REDIRECT_URLS": False,
    # Feature keys the deployment recognises, on top of the ones the
    # built-in plan ladder declares. An entitlement key outside this
    # vocabulary is denied at runtime and rejected by a system check when
    # a configured plan mentions it — a typo must not hand out a feature.
    "ENTITLEMENT_KEYS": [],
    # Escape hatch: restore the pre-hardening behaviour where an
    # unrecognised entitlement key was ALLOWED.
    "ALLOW_UNKNOWN_ENTITLEMENT_KEYS": False,
    # Escape hatch: restore the pre-hardening behaviour where a plan slug the
    # catalogue does not contain (a retired/renamed plan still on a live
    # Subscription row, or a default plan outside the host's own ladder)
    # carried NO entitlements — which read as "no ceiling configured" and made
    # every paid feature unrestricted for that user. See entitlements.py.
    "ALLOW_UNKNOWN_PLAN_SLUGS": False,
    # Escape hatch: let an UNCONFIGURED payment provider answer with dev
    # placeholders (a fake checkout URL and session id, a fake portal link, a
    # cancel that cancels nothing) instead of refusing. Only ever right on a
    # developer's machine: everywhere else it makes the service report
    # payments that no payment system has heard of. Boot check E104 refuses a
    # deployment whose provider has no credentials; W104 reports this hatch.
    "ALLOW_UNCONFIGURED_PAYMENT_PROVIDER": False,
    # How long a credit reservation (services.hold) stays valid when the
    # caller does not set its own deadline. The crash-safety bound: a
    # pipeline that dies between the hold and the answer locks a customer's
    # credits for at most this long. 0 (or None) means "never sweep", which
    # is a decision, not a default.
    "HOLD_DEFAULT_TTL_SECONDS": 3600,
    # Reconcile the provider's checkout object (mode, payment status,
    # currency, amount, owner) against the catalog before granting credits
    # or a plan. Off means the provider's metadata is taken on faith.
    "STRICT_CHECKOUT_RECONCILIATION": True,
}

billing_settings = AppSettings(
    "STAPEL_BILLING",
    defaults=DEFAULTS,
    import_strings=("PAYMENT_PROVIDER",),
    # PAYMENT_PROVIDER decides which class handles money — checkout,
    # subscriptions, webhook verification. The name is generic enough that a
    # same-named env var in a shared pod or compose file could swap it
    # silently, so it resolves from the namespace dict, a flat Django setting
    # or the default only (the gateway and workspaces apply the same rule to
    # their own seams).
    no_env=("PAYMENT_PROVIDER",),
)

#: Values an environment variable may spell "yes" with. AppSettings resolves
#: env vars as raw strings (it has no per-key type) and the switches below are
#: consumed as bare truth values, so ``bool("false")`` is True — an operator
#: who set ``ALLOW_UNKNOWN_ENTITLEMENT_KEYS=false`` to turn the hatch OFF used
#: to turn it ON, and re-arm the BILL-02 open redirect the same way. Anything
#: not in this set is False.
_TRUTHY = {"1", "true", "yes", "on"}

#: The mirror image, for switches whose *safe* value is True: only an explicit
#: "no" turns them off. An unrecognised spelling keeps the gate closed rather
#: than degrading to "off", which is how a deployment ends up believing it has
#: a gate it does not have.
_FALSY = {"0", "false", "no", "off"}


def _opened(key: str) -> bool:
    """A default-off switch: on only when something explicitly says so."""
    value = getattr(billing_settings, key)
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


def _closed_unless_denied(key: str) -> bool:
    """A default-on gate: off only when something explicitly says so."""
    value = getattr(billing_settings, key)
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY
    return bool(value)


def allow_unvalidated_redirect_urls() -> bool:
    """``ALLOW_UNVALIDATED_REDIRECT_URLS`` as a bool (see :data:`_TRUTHY`)."""
    return _opened("ALLOW_UNVALIDATED_REDIRECT_URLS")


def allow_unknown_entitlement_keys() -> bool:
    """``ALLOW_UNKNOWN_ENTITLEMENT_KEYS`` as a bool (see :data:`_TRUTHY`)."""
    return _opened("ALLOW_UNKNOWN_ENTITLEMENT_KEYS")


def allow_unknown_plan_slugs() -> bool:
    """``ALLOW_UNKNOWN_PLAN_SLUGS`` as a bool (see :data:`_TRUTHY`)."""
    return _opened("ALLOW_UNKNOWN_PLAN_SLUGS")


def allow_unconfigured_payment_provider() -> bool:
    """``ALLOW_UNCONFIGURED_PAYMENT_PROVIDER`` as a bool (see :data:`_TRUTHY`)."""
    return _opened("ALLOW_UNCONFIGURED_PAYMENT_PROVIDER")


def hold_default_ttl_seconds() -> int:
    """``HOLD_DEFAULT_TTL_SECONDS`` as a non-negative int (0 = never sweep).

    Env vars arrive as text, so the value is coerced here rather than at the
    call site; anything unparseable falls back to the default instead of
    crashing a charge path, and says so.
    """
    value = billing_settings.HOLD_DEFAULT_TTL_SECONDS
    if value is None:
        return 0
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        import logging

        logging.getLogger(__name__).error(
            "STAPEL_BILLING['HOLD_DEFAULT_TTL_SECONDS']=%r is not a number — "
            "falling back to %s seconds.",
            value,
            DEFAULTS["HOLD_DEFAULT_TTL_SECONDS"],
        )
        return DEFAULTS["HOLD_DEFAULT_TTL_SECONDS"]
    return max(seconds, 0)


def strict_checkout_reconciliation() -> bool:
    """``STRICT_CHECKOUT_RECONCILIATION`` as a bool (see :data:`_FALSY`)."""
    return _closed_unless_denied("STRICT_CHECKOUT_RECONCILIATION")


__all__ = [
    "billing_settings",
    "allow_unconfigured_payment_provider",
    "hold_default_ttl_seconds",
    "allow_unknown_entitlement_keys",
    "allow_unknown_plan_slugs",
    "allow_unvalidated_redirect_urls",
    "strict_checkout_reconciliation",
]
