"""URL configuration for the billing app."""

from django.urls import path

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
]
