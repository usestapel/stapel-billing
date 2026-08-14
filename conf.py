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
    # Reconcile the provider's checkout object (mode, payment status,
    # currency, amount, owner) against the catalog before granting credits
    # or a plan. Off means the provider's metadata is taken on faith.
    "STRICT_CHECKOUT_RECONCILIATION": True,
}

billing_settings = AppSettings(
    "STAPEL_BILLING",
    defaults=DEFAULTS,
    import_strings=("PAYMENT_PROVIDER",),
)

__all__ = ["billing_settings"]
