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
ERR_400_REDIRECT_URL_NOT_CONFIGURED = "error.400.redirect_url_not_configured"
ERR_400_REDIRECT_URL_NOT_ALLOWED = "error.400.redirect_url_not_allowed"
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
    ERR_400_REDIRECT_URL_NOT_CONFIGURED: (
        "Redirect URL not provided and no fallback configured"
    ),
    ERR_400_REDIRECT_URL_NOT_ALLOWED: (
        "Redirect URL origin is not allowlisted for this deployment"
    ),
    ERR_400_INVALID_WEBHOOK_PAYLOAD: "Invalid Stripe webhook payload",
    ERR_403_FORBIDDEN_BILLING: "Forbidden: cannot manage this billing account",
    ERR_409_DUPLICATE_WEBHOOK: "Stripe event already processed",
}

# Machine-readable recovery hints (remediation) — the canonical "what to do"
# for each key, emitted into the errors.json codegen artifact and consumed by
# the frontend/LLM (frontend-core-architecture §2.5). Vocabulary: retry |
# wait_and_retry | reauthenticate | verify | fix_input | contact_support | bug.
# Declared here (backend = canon) rather than left to the status+name heuristic,
# which lies for several billing keys. Rationale per key:
#
#   * 404 *_not_found (wallet/subscription/transaction) → fix_input, NOT the
#     heuristic's retry-for-404-not_found: retrying the same lookup just loops
#     the failing request. The honest recovery is to correct the identifier
#     (same canon override the notifications/profiles pairs made).
#   * 402 insufficient_credits → fix_input, NOT the heuristic's fall-through
#     retry. This is the payment case: retrying the same spend with the same
#     balance loops forever, and it is not a support/bug matter — the user
#     self-serves by topping up credits or lowering the amount. fix_input is the
#     "the request as-is can't succeed, change what you control" signal; the host
#     maps it to surfacing the top-up / amount affordance.
#   * 400 invalid_package / invalid_plan / amount_invalid → fix_input. Genuine
#     bad-input from the user's request body; matches the heuristic (declared for
#     completeness so every key's canon is explicit).
#   * 400 invalid_stripe_signature / invalid_webhook_payload → contact_support,
#     NOT the heuristic's fix_input. These are machine-to-machine (Stripe → us):
#     there is no user field to "fix". A bad signature/payload means a
#     misconfigured webhook secret or a broken integration — an operator must
#     investigate (precedent: auth's sso_not_configured → contact_support).
#   * 400 redirect_url_not_configured → contact_support, NOT fix_input. This is
#     an operator/deployment misconfiguration (no checkout redirect URL and no
#     FRONTEND_URL fallback), not user input.
#   * 400 redirect_url_not_allowed → contact_support, NOT fix_input. The caller
#     is a first-party client sending its own return URL; a legitimate one that
#     is refused means the deployment has not declared that origin. There is no
#     end-user field to correct, and inviting the client to "try another URL"
#     is exactly the probing the allowlist exists to stop.
#   * 403 forbidden_billing → contact_support, NOT the heuristic's retry-for-403.
#     A genuine authorization boundary ("cannot manage this billing account");
#     retrying loops and re-auth won't grant access — the user asks an admin.
#   * 409 duplicate_webhook_event → retry, NOT the heuristic's fix_input. A
#     re-delivered Stripe event is a benign idempotency signal the service
#     already no-ops; fix_input would falsely flag a field. retry marks it as
#     transient/safe (the actual handler returns 200 for duplicates).
BILLING_REMEDIATION = {
    ERR_404_WALLET_NOT_FOUND: "fix_input",
    ERR_404_SUBSCRIPTION_NOT_FOUND: "fix_input",
    ERR_404_TRANSACTION_NOT_FOUND: "fix_input",
    ERR_402_INSUFFICIENT_CREDITS: "fix_input",
    ERR_400_INVALID_PACKAGE: "fix_input",
    ERR_400_INVALID_PLAN: "fix_input",
    ERR_400_AMOUNT_INVALID: "fix_input",
    ERR_400_INVALID_STRIPE_SIGNATURE: "contact_support",
    ERR_400_REDIRECT_URL_NOT_CONFIGURED: "contact_support",
    ERR_400_REDIRECT_URL_NOT_ALLOWED: "contact_support",
    ERR_400_INVALID_WEBHOOK_PAYLOAD: "contact_support",
    ERR_403_FORBIDDEN_BILLING: "contact_support",
    ERR_409_DUPLICATE_WEBHOOK: "retry",
}

register_service_errors(BILLING_ERRORS, remediation=BILLING_REMEDIATION)


class BillingErrorKeysView(ErrorKeysView):
    def get_service_errors(self):
        return BILLING_ERRORS
