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

billing_settings = AppSettings(
    "STAPEL_BILLING",
    defaults={
        # Dotted path to a stapel_billing.providers.base.PaymentProvider subclass.
        "PAYMENT_PROVIDER": "stapel_billing.providers.stripe.StripeProvider",
        # Read lazily at call time (never frozen at import).
        "STRIPE_SECRET_KEY": "",
        "STRIPE_WEBHOOK_SECRET": "",
        # Catalog overrides: lists of dicts (converted to the catalog
        # dataclasses) or dataclass instances. Defaults live in catalog.py.
        "CREDIT_PACKAGES": _catalog.DEFAULT_CREDIT_PACKAGES,
        "PLANS": _catalog.DEFAULT_PLANS,
    },
    import_strings=("PAYMENT_PROVIDER",),
)

__all__ = ["billing_settings"]
