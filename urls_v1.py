"""URL configuration for the billing app."""

from typing import NamedTuple

from django.urls import path

from .errors import BillingErrorKeysView
from .views import (
    CatalogView,
    CheckoutView,
    CustomerPortalView,
    InternalDebitView,
    StripeWebhookView,
    SubscriptionCancelView,
    SubscriptionView,
    TransactionListView,
    WalletView,
)


urlpatterns = [
    # Wallet
    path("wallet", WalletView.as_view(), name="wallet"),
    path("wallet/transactions", TransactionListView.as_view(), name="wallet-transactions"),

    # Catalogue
    path("products", CatalogView.as_view(), name="catalog"),

    # Checkout / portal
    path("checkout", CheckoutView.as_view(), name="checkout"),
    path("portal", CustomerPortalView.as_view(), name="customer-portal"),

    # Subscription
    path("subscription", SubscriptionView.as_view(), name="subscription"),
    path("subscription/cancel", SubscriptionCancelView.as_view(), name="subscription-cancel"),

    # Stripe webhook
    path("webhooks/stripe", StripeWebhookView.as_view(), name="stripe-webhook"),

    # Internal service-to-service debit
    path("internal/debit", InternalDebitView.as_view(), name="internal-debit"),

    # Error-key registry for the stapel-translate collector (service/staff only).
    path("error-keys/", BillingErrorKeysView.as_view(), name="error-keys"),
]


class GateEntry(NamedTuple):
    """One gated URL block: which flags gate which url patterns (capability-config.md §2 p.2).

    ``flags`` compose with OR — the block is mounted while ANY flag is on,
    and disappears only when ALL of them are off. Empty flags = always on.
    """
    name: str
    flags: tuple
    patterns: tuple


#: Gate registry (capability-config.md §2 p.2): billing has no per-method
#: config gates (PAYMENT_PROVIDER swaps the backend, it never unmounts
#: endpoints) — the whole URL surface is a single always-on block. Declared
#: as a registry entry (rather than left implicit) so the capabilities.json
#: emitter has a uniform mechanism across every module.
GATE_REGISTRY: dict = {
    'billing.api': GateEntry('billing.api', (), tuple(urlpatterns)),
}
