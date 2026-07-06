# Changelog

All notable changes to stapel-billing are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.4.4 — 2026-07-06

### Changed
- Pinned `stapel-core` to the `>=0.8,<0.9` window (library-standard §7.1: one
  minor window; floor `0.8.0` is published on PyPI — no pin into the void).
- CI: added the release-track job (library-standard §7.4) — installs the package
  the way an end user does (`pip install .`, dependencies resolved from PyPI
  strictly by the declared pins, no git-main core, no editable siblings), asserts
  `stapel-core` resolves inside the `0.8` window, and runs an import smoke.
  Advisory (continue-on-error) until the whole stapel graph is on PyPI; becomes
  the blocking precondition for a `vX.Y.Z` tag once it is.


## 0.4.3 — 2026-07-06

### Packaging
- Tests excluded from the built wheel/sdist (the `stapel_billing.tests`
  subpackage is no longer listed in `[tool.setuptools] packages`). Added
  `[project.urls]`, completed the trove classifiers (MIT/OSI, Python 3.13,
  `Typing :: Typed`, OS Independent, `3 :: Only`, Development Status) and a
  `[tool.ruff]` lint section (single source shared with the git hooks/CI).


## 0.4.2 — 2026-07-05

### Changed
- OpenAPI: `@extend_schema` for `SubscriptionCancel` + `StripeWebhook`
  (external Stripe event body documented as opaque object).


## 0.4.1 — 2026-07-05

### Fixed
- `user_id` in comm schemas typed uuid, was integer — rejected valid
  `user.deleted` events. `schemas/consumes/user.deleted.json` and
  `schemas/consumes/user.deletion_initiated.json` now type `user_id` as
  `{"type": "string", "format": "uuid"}`, matching the UUID-pk canonical
  user and the auth/gdpr producers.


## 0.4.0 — 2026-07-04
### Changed
- **Checkout/portal redirect URLs no longer fall back to `example.com`.**
  Resolution order is now: request value → `STAPEL_BILLING` keys
  (`CHECKOUT_SUCCESS_URL`, `CHECKOUT_CANCEL_URL`, `PORTAL_RETURN_URL`,
  all new, empty by default) → derived from the flat `FRONTEND_URL`
  setting/env (`{FRONTEND_URL}/billing/success`, `/billing/cancel`,
  `/billing`). With none configured the request is rejected with the new
  `error.400.redirect_url_not_configured` instead of silently sending
  users to a placeholder domain.

## 0.3.0 — 2026-07-03

No functional changes — version alignment with the Stapel 0.3
release train; stapel-core dependency now `>=0.3.0,<0.4`.


## [0.2.0] - 2026-07-02

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
