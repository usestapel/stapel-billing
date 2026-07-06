# Errors — English

`53` error keys. Canonical texts live in the code (`register_service_errors`); localized texts in `translations/errors.en.json`.

| Code | Status | Params | Remediation | Text |
|---|---|---|---|---|
| `error.400.amount_invalid` | 400 | — | `fix_input` | Amount must be a positive integer |
| `error.400.bad_request` | 400 | — | `fix_input` | Bad request |
| `error.400.captcha_invalid` | 400 | — | `retry` | Captcha verification failed. Please try again. |
| `error.400.captcha_required` | 400 | — | `retry` | Captcha token is required. |
| `error.400.expected_list` | 400 | — | `fix_input` | Expected a list of items |
| `error.400.field.blank` | 400 | `field` | `fix_input` | {field} may not be blank |
| `error.400.field.does_not_exist` | 400 | `field` | `fix_input` | {field} does not exist |
| `error.400.field.invalid` | 400 | `field` | `fix_input` | {field} is invalid |
| `error.400.field.invalid_choice` | 400 | `field` | `fix_input` | {field} is not a valid choice |
| `error.400.field.max_length` | 400 | `field`, `max_length` | `fix_input` | {field} must be at most {max_length} characters |
| `error.400.field.max_value` | 400 | `field`, `max_value` | `fix_input` | {field} must be at most {max_value} |
| `error.400.field.min_length` | 400 | `field`, `min_length` | `fix_input` | {field} must be at least {min_length} characters |
| `error.400.field.min_value` | 400 | `field`, `min_value` | `fix_input` | {field} must be at least {min_value} |
| `error.400.field.null` | 400 | `field` | `fix_input` | {field} may not be null |
| `error.400.field.required` | 400 | `field` | `fix_input` | {field} is required |
| `error.400.field.unique` | 400 | `field` | `fix_input` | {field} must be unique |
| `error.400.invalid_ad_id` | 400 | — | `fix_input` | Invalid advertisement ID |
| `error.400.invalid_package` | 400 | — | `fix_input` | Invalid credit package |
| `error.400.invalid_plan` | 400 | — | `fix_input` | Invalid subscription plan |
| `error.400.invalid_stripe_signature` | 400 | — | `contact_support` | Invalid Stripe webhook signature |
| `error.400.invalid_webhook_payload` | 400 | — | `contact_support` | Invalid Stripe webhook payload |
| `error.400.redirect_url_not_configured` | 400 | — | `contact_support` | Redirect URL not provided and no fallback configured |
| `error.400.validation_error` | 400 | — | `fix_input` | Validation error |
| `error.400.verification_failed` | 400 | — | `verify` | Verification failed |
| `error.400.verification_invalid_factor` | 400 | — | `verify` | This verification factor is not available |
| `error.401.unauthorized` | 401 | — | `reauthenticate` | Authentication required |
| `error.402.insufficient_credits` | 402 | — | `fix_input` | Insufficient credits |
| `error.402.payment_required` | 402 | — | `retry` | Payment required |
| `error.403.forbidden` | 403 | — | `retry` | You do not have permission to perform this action |
| `error.403.forbidden_billing` | 403 | — | `contact_support` | Forbidden: cannot manage this billing account |
| `error.403.network_blocked` | 403 | — | `contact_support` | Requests from this network are not allowed |
| `error.403.verification_enrollment_required` | 403 | — | `verify` | Verification factor enrollment required |
| `error.403.verification_required` | 403 | — | `verify` | Additional verification required |
| `error.404.ad_not_found` | 404 | — | `retry` | Listing not found |
| `error.404.not_found` | 404 | — | `retry` | Requested resource not found |
| `error.404.subscription_not_found` | 404 | — | `fix_input` | Subscription not found |
| `error.404.transaction_not_found` | 404 | — | `fix_input` | Transaction not found |
| `error.404.verification_challenge_not_found` | 404 | — | `verify` | Verification challenge not found or expired |
| `error.404.wallet_not_found` | 404 | — | `fix_input` | Wallet not found |
| `error.405.method_not_allowed` | 405 | — | `retry` | Method not allowed |
| `error.406.not_acceptable` | 406 | — | `retry` | Not acceptable |
| `error.408.request_timeout` | 408 | — | `retry` | Request timeout |
| `error.409.conflict` | 409 | — | `fix_input` | Resource already exists |
| `error.409.duplicate_webhook_event` | 409 | — | `retry` | Stripe event already processed |
| `error.410.gone` | 410 | — | `retry` | Resource has been permanently removed |
| `error.413.payload_too_large` | 413 | — | `retry` | Request body is too large |
| `error.415.unsupported_media_type` | 415 | — | `retry` | Unsupported media type |
| `error.422.unprocessable_entity` | 422 | — | `wait_and_retry` | Unprocessable entity |
| `error.423.locked` | 423 | — | `wait_and_retry` | Resource is locked |
| `error.423.verification_locked` | 423 | — | `wait_and_retry` | Too many failed attempts — verification locked |
| `error.429.rate_limit` | 429 | `retry_after_minutes` | `wait_and_retry` | Too many attempts. Try again in {retry_after_minutes} minutes. |
| `error.429.too_many_requests` | 429 | — | `wait_and_retry` | Too many requests. Please try again later. |
| `error.500.internal` | 500 | — | `contact_support` | Something went wrong |
