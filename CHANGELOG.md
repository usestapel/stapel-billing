# Changelog

All notable changes to stapel-billing are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **PaymentProvider abstraction** (`providers/base.py`): ABC with
  `create_checkout_session`, `create_portal_session`, `cancel_subscription`,
  `verify_webhook(payload, signature) -> event dict` and a `name` attribute.
  `providers/stripe.py` ships `StripeProvider` with the previous behavior
  from `services.py`. Swap the backend without forking via
  `STAPEL_BILLING = {"PAYMENT_PROVIDER": "dotted.path.ToProvider"}`.
- **Settings namespace** (`conf.py`): `billing_settings = AppSettings("STAPEL_BILLING", ...)`
  from stapel-core. Keys: `PAYMENT_PROVIDER` (import string),
  `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `CREDIT_PACKAGES`, `PLANS`.
  Resolution order: `settings.STAPEL_BILLING` dict → flat Django setting →
  environment variable → default.
- **Configurable catalog**: `catalog.py` defaults can be overridden through
  `STAPEL_BILLING={"CREDIT_PACKAGES": [...], "PLANS": [...]}` (dicts are
  converted to the `CreditPackage` / `PlanCatalogEntry` dataclasses).
  Module-level names `CREDIT_PACKAGES`, `PLANS`, `CREDIT_PACKAGES_BY_SLUG`,
  `PLANS_BY_SLUG` keep working as lazy views; `get_credit_packages()` /
  `get_plans()` are the functional API.
- **Business milestones**: webhook handlers now emit
  `payment.completed` / `subscription.changed` comm actions (inside the
  webhook's atomic block, via the transactional outbox) and send
  `stapel_core.signals.payment_completed` / `subscription_changed` Django
  signals. `payment.completed` is emitted for package purchases,
  initial plan grants and invoice renewals; `subscription.changed` for
  checkout, `customer.subscription.updated/created/deleted`.
- **Idempotent internal debit**: `POST /billing/api/internal/debit` accepts
  an optional `idempotency_key`. It is stored on
  `Transaction.metadata["idempotency_key"]`; a duplicate call for the same
  wallet short-circuits under the wallet row lock and returns the original
  transaction (no migration needed — JSON metadata lookup).
- `py.typed` marker (PEP 561) shipped in package data.

### Changed
- **Stripe keys are read lazily at call time** through the conf layer.
  Previously `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` were frozen at
  import via `os.getenv`, so test/host overrides of the Django settings
  silently did nothing (the `stripe_disabled` fixture passed for the wrong
  reason). The fixture now clears both the settings and the env vars and
  is verified by tests.
- `services.py` checkout/portal/webhook helpers delegate to the configured
  provider; `SubscriptionCancelView` uses `services.cancel_provider_subscription`
  instead of poking the stripe module directly.
- `models.py` references the user model via `settings.AUTH_USER_MODEL`
  instead of importing the concrete `User` class (migration state is
  unchanged — the initial migration already used the swappable dependency).
  `services.py` / `views.py` resolve users via `get_user_model()`.

### Fixed
- `schemas/emits/payment.completed.json` and `subscription.changed.json`:
  `user_id` / `transaction_id` are UUID strings (were wrongly `integer`),
  `current_period_end` is nullable via `type: ["string", "null"]`, and
  `additionalProperties: true` to allow enrichment (`package`, `plan`).
