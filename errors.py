"""Custom error keys for the billing service."""

from stapel_core.django.api.errors import ErrorKeysView, register_service_errors

ERR_404_WALLET_NOT_FOUND = "error.404.wallet_not_found"
ERR_404_SUBSCRIPTION_NOT_FOUND = "error.404.subscription_not_found"
ERR_404_TRANSACTION_NOT_FOUND = "error.404.transaction_not_found"
ERR_402_INSUFFICIENT_CREDITS = "error.402.insufficient_credits"
ERR_400_INVALID_PACKAGE = "error.400.invalid_package"
ERR_400_INVALID_PLAN = "error.400.invalid_plan"
ERR_400_AMOUNT_INVALID = "error.400.amount_invalid"
ERR_400_INVALID_STRIPE_SIGNATURE = "error.400.invalid_stripe_signature"
ERR_400_INVALID_WEBHOOK_PAYLOAD = "error.400.invalid_webhook_payload"
ERR_403_FORBIDDEN_BILLING = "error.403.forbidden_billing"
ERR_409_DUPLICATE_WEBHOOK = "error.409.duplicate_webhook_event"

BILLING_ERRORS = {
    ERR_404_WALLET_NOT_FOUND: "Wallet not found",
    ERR_404_SUBSCRIPTION_NOT_FOUND: "Subscription not found",
    ERR_404_TRANSACTION_NOT_FOUND: "Transaction not found",
    ERR_402_INSUFFICIENT_CREDITS: "Insufficient credits",
    ERR_400_INVALID_PACKAGE: "Invalid credit package",
    ERR_400_INVALID_PLAN: "Invalid subscription plan",
    ERR_400_AMOUNT_INVALID: "Amount must be a positive integer",
    ERR_400_INVALID_STRIPE_SIGNATURE: "Invalid Stripe webhook signature",
    ERR_400_INVALID_WEBHOOK_PAYLOAD: "Invalid Stripe webhook payload",
    ERR_403_FORBIDDEN_BILLING: "Forbidden: cannot manage this billing account",
    ERR_409_DUPLICATE_WEBHOOK: "Stripe event already processed",
}

register_service_errors(BILLING_ERRORS)


class BillingErrorKeysView(ErrorKeysView):
    def get_service_errors(self):
        return BILLING_ERRORS
