# stapel-billing — MODULE.md

Agent-facing map of this module: what it provides, its fork-free extension points, and
anti-patterns. Use it to classify a desired change as **app-layer override via an
extension point** vs **upstream contribution** (see `docs/stdlib-contribution-pipeline.md`
and system-design.md §8.6 in the Stapel monorepo). Stapel modules never import each
other; all cross-module communication goes through `stapel-core` (comm bus, signals,
registries). Everything below is verifiable against the code in this repo.

- Package: `stapel-billing` (PyPI), Python package `stapel_billing`, Django app label `billing`.
- Depends on `stapel-core` only (plus DRF, drf-spectacular; `stripe` SDK is optional at runtime).

## What this module provides

| Area | Contents |
|---|---|
| Models (`models.py`) | `Wallet` (one per user, integer credit `balance`), `Transaction` (immutable ledger: `credits_delta`, `balance_after`, `amount_cents`, JSON `metadata`), `Subscription` (one per user, Stripe-backed), `StripeWebhookEvent` (webhook idempotency log, `stripe_event_id` unique) |
| Services (`services.py`) | `credit()` / `debit()` (row-locked wallet ops writing ledger rows; `debit` supports `idempotency_key`, raises `InsufficientCreditsError`), `get_provider()`, `create_checkout_session()`, `create_customer_portal()`, `cancel_provider_subscription()`, `verify_stripe_signature()`, webhook handlers (`handle_checkout_completed`, `handle_invoice_paid`, `handle_subscription_updated`, `handle_subscription_deleted`) |
| HTTP API (`urls.py`, `views.py`) | `wallet` (GET/PATCH), `wallet/transactions`, `products` (public catalog), `checkout`, `portal`, `subscription`, `subscription/cancel`, `webhooks/stripe`, `internal/debit` (service-to-service, `IsServiceRequest \| IsStaffUser`) |
| Catalog (`catalog.py`) | `CreditPackage` / `PlanCatalogEntry` frozen dataclasses; `CREDIT_PACKAGES`, `PLANS`, `CREDIT_PACKAGES_BY_SLUG`, `PLANS_BY_SLUG` are lazy views that re-read `STAPEL_BILLING` on every access |
| Providers (`providers/`) | `PaymentProvider` ABC (`providers/base.py`), `StripeProvider` default implementation (`providers/stripe.py`, lazy credentials, dev placeholder URLs when unconfigured) |
| GDPR (`gdpr.py`, `apps.py`) | `BillingGDPRProvider` (section `"billing"`), registered with `stapel_core.gdpr.gdpr_registry` in `AppConfig.ready()`; export + delete (Stripe IDs cleared, wallet anonymised, ledger retained) |
| Public API (`__init__.py`, PEP 562 lazy) | `__all__ = ["InsufficientCreditsError", "PaymentProvider", "billing_settings", "credit", "debit", "get_provider"]` |

Money is always in **minor units** (`price_cents`, `amount_cents` — integers); wallet
credits are **integers, never fractional**.

## Extension points (fork-free)

### Settings — `STAPEL_BILLING` namespace (`conf.py`)

`billing_settings = AppSettings("STAPEL_BILLING", ...)` from `stapel_core.conf`.
Resolution order per key: `settings.STAPEL_BILLING[key]` → flat Django setting of the
same name → environment variable → default. All keys are read **lazily at call time**
(never frozen at import); caches invalidate on `setting_changed`.

| Key | Default | What it customizes |
|---|---|---|
| `PAYMENT_PROVIDER` | `"stapel_billing.providers.stripe.StripeProvider"` | The payment backend. In `import_strings` — resolved via `import_string`, must be a `PaymentProvider` subclass (enforced by `get_provider()`). |
| `STRIPE_SECRET_KEY` | `""` | Stripe API key for the default provider (read lazily per call). |
| `STRIPE_WEBHOOK_SECRET` | `""` | Stripe webhook signature secret (read lazily per call). |
| `CREDIT_PACKAGES` | `catalog.DEFAULT_CREDIT_PACKAGES` (starter/standard/bulk) | One-off credit package catalog. List of dicts or `CreditPackage` instances. |
| `PLANS` | `catalog.DEFAULT_PLANS` (free/pro/team/enterprise) | Subscription plan catalog. List of dicts or `PlanCatalogEntry` instances. |
| `CHECKOUT_SUCCESS_URL` | `""` | Fallback Stripe Checkout success redirect when the request carries none. Empty → derived from the flat `FRONTEND_URL` setting/env (`{FRONTEND_URL}/billing/success`); with neither configured the request fails with `error.400.redirect_url_not_configured`. |
| `CHECKOUT_CANCEL_URL` | `""` | Same resolution for the cancel redirect (`{FRONTEND_URL}/billing/cancel`). |
| `PORTAL_RETURN_URL` | `""` | Same resolution for the customer-portal return URL (`{FRONTEND_URL}/billing`). |

### Payment providers (dotted-path swap)

Implement the ABC `stapel_billing.providers.base.PaymentProvider` and point the setting
at it — no fork:

```python
# myproject/billing.py
from stapel_billing import PaymentProvider

class AcmeProvider(PaymentProvider):
    name = "acme"
    ...

# settings.py
STAPEL_BILLING = {"PAYMENT_PROVIDER": "myproject.billing.AcmeProvider"}
```

ABC contract (all abstract, all must be implemented):

| Method | Signature | Contract |
|---|---|---|
| `create_checkout_session` | `(*, user, package, plan, success_url, cancel_url) -> (checkout_url, session_id)` | Start checkout for a one-off *package* or recurring *plan*. |
| `create_portal_session` | `(*, customer_id, return_url) -> str` | Self-service billing portal URL. |
| `cancel_subscription` | `(subscription_id) -> None` | Cancel at period end. No-op when unconfigured; **must raise** on failed remote call (callers refuse to mark cancelled locally otherwise). |
| `verify_webhook` | `(payload: bytes, signature: str) -> dict` | Parse + verify an incoming webhook; raises `ValueError` on bad signature / unconfigured provider. |

Providers must read credentials lazily (at call time, via `billing_settings`), never at
import — see `StripeProvider.api_key` / `.webhook_secret` properties for the pattern.

### Swappable models

None. This module defines no swappable models of its own; it binds to the host user
model via `settings.AUTH_USER_MODEL` (standard Django swap — that is the only model
seam). `Wallet`, `Transaction`, `Subscription`, `StripeWebhookEvent` have fixed
`db_table` names (`billing_*`). Extend user-visible billing data in an app-layer model
with a FK/OneToOne to the user or to these tables — do not fork to add fields.

### Serializer seams (`views.py`)

Every billing view mixes in `SerializerSeamMixin` with class attributes
`request_serializer_class` / `response_serializer_class` and overridable getters
`get_request_serializer_class()` / `get_response_serializer_class()`. To change a
request/response payload shape: subclass the view, set the class attribute (or override
the getter for per-request decisions), and mount your subclass in the host project's
URLconf instead of the stock route — HTTP method bodies stay untouched.

| View | Route (name) | Request serializer | Response serializer |
|---|---|---|---|
| `WalletView` | `wallet` | `WalletUpdateRequestSerializer` | `WalletResponseSerializer` |
| `TransactionListView` | `wallet-transactions` | — | `TransactionListResponseSerializer` |
| `CatalogView` | `catalog` | — | `CatalogResponseSerializer` |
| `CheckoutView` | `checkout` | `CheckoutRequestSerializer` | `CheckoutResponseSerializer` |
| `CustomerPortalView` | `customer-portal` | — | `CustomerPortalResponseSerializer` |
| `SubscriptionView` | `subscription` | — | `SubscriptionResponseSerializer` |
| `SubscriptionCancelView` | `subscription-cancel` | — | `SubscriptionResponseSerializer` |
| `StripeWebhookView` | `stripe-webhook` | — | — |
| `InternalDebitView` | `internal-debit` | `CreditDebitRequestSerializer` | `CreditOperationResponseSerializer` |

### Events & functions (comm surface)

Transport-agnostic via `stapel_core.comm` (in-process in a monolith, bus consumer in
microservices — same code). Emits go through the transactional outbox inside the
webhook's atomic block: an event leaves iff the DB transaction commits. JSON Schemas
live in `schemas/emits/` and `schemas/consumes/`.

**Emits** (from `services.py`, keyed by `user_id`):

| Event | Payload (required) | When |
|---|---|---|
| `payment.completed` | `user_id`, `amount_cents`, `currency`, `transaction_id`, `created_at` (+ optional `package` / `plan`) | Package purchase, initial plan bonus, plan renewal grant |
| `subscription.changed` | `user_id`, `plan`, `status` (+ `current_period_end`) | Subscription created / status updated / deleted |

**Consumes** (`actions.py`, `@on_action`; handlers must be idempotent — delivery is at-least-once):

| Event | Handler | Effect |
|---|---|---|
| `user.deleted` | `handle_user_deleted` | `BillingGDPRProvider().delete(user_id)` — erase billing PII |

(`schemas/consumes/user.deletion_initiated.json` is declared, but `actions.py` currently
subscribes only to `user.deleted`.)

**Functions provided:** none. The module registers no `stapel_core.comm` functions;
the service-to-service surface is the HTTP endpoint `internal/debit` (pass
`idempotency_key` — duplicates short-circuit to the original transaction).

### Django signals

Sent via `stapel_core.signals` (in-process, host apps connect receivers freely):

| Signal | Sender | Kwargs | When |
|---|---|---|---|
| `payment_completed` | `Transaction` | `user`, `credits`, `transaction` | Alongside every `payment.completed` emit |
| `subscription_changed` | `Subscription` | `subscription` | Alongside every `subscription.changed` emit |

### Webhook idempotency (contract, not a seam)

`StripeWebhookView` verifies the signature via the configured provider, then claims the
event: `StripeWebhookEvent.get_or_create(stripe_event_id=...)` → already-processed
events (with `processed_at` set) return `{"status": "duplicate"}`; a row **without**
`processed_at` is a previous failed attempt and is reprocessed on the provider's retry.
The handler runs under `select_for_update()` on the event row, and the `processed_at`
mark commits atomically with the handler's effects (credit grants, emits). Renewal
grants are additionally idempotent per invoice (`metadata__stripe_invoice_id` check in
`handle_invoice_paid`).

### Admin categories (`stapel_core.access`, admin-suite AS-5)

`StripeWebhookEvent` is decorated `@access.ops` (read-only journal — add/change/delete
forbidden for everyone including superuser at the admin layer; view requires HIGH
clearance) and its `ModelAdmin` subclasses `stapel_core.django.admin.base.
StapelModelAdmin`. It is an idempotency/delivery log for inbound Stripe webhooks
(`stripe_event_id`, `payload`, `processed_at`) that staff never edit — mutation only
happens through the webhook claim protocol above. `Wallet`, `Transaction`, and
`Subscription` are business tables and stay undecorated (implicit `@access.standard`).
No model in this repo stores a secret/token/credential value — `STRIPE_SECRET_KEY` /
`STRIPE_WEBHOOK_SECRET` are process settings read lazily via `billing_settings`, never
persisted to a DB field — so `@access.secret` does not apply anywhere here.

### Error localization

`docs/errors.json` is the existing en canon (the `generate_error_keys` codegen
artifact — the array of `{code, status, params, remediation, en}` the frontend
error bundle is generated from). **Error localization** (i18n-shipping.md §5):
ru ships as a flat `translations/errors.ru.json` catalog with a
`translations/.state.json` provenance sidecar, and human-readable references
[Errors (EN)](docs/errors.en.md) · [Ошибки (RU)](docs/errors.ru.md). Semantics
of the i18n seams (library-standard §3.3 — MODULE.md states the merge
semantics of each key): the **error registry** is `dict.update`/**last-wins**
(a host `errors.py` autodiscovered after ours overrides an en text — and its
raise-time render — without a fork); the **locale catalogs** are discovered
over INSTALLED_APPS and merged **later-wins** (a host app's
`translations/errors.<lang>.json` overrides our texts, and an override MUST
keep the canon's `{param}` slots — gated). ru provenance is honest: 50 keys
seeded from the curated `stapel-translate` builtin fixtures (`origin:
seed:stapel-builtin`, no tokens spent), 3 billing-only keys
machine-translated (`origin: llm`, unreviewed — the gate's W-counter, cleared
by `translate_catalogs --approve`). Gate + regenerate: `tests/test_error_i18n.py`
(`check_translation_catalogs` — E on missing/stale/params/byte-instability);
regenerate with `STAPEL_REGEN_ERROR_I18N=1 pytest
tests/test_error_i18n.py::test_regen` and commit `translations/errors.ru.json`,
`translations/.state.json`, `docs/errors.{en,ru}.md`.

## Anti-patterns

- **Don't fork to add a payment provider.** Subclass `PaymentProvider` in the app layer
  and point `STAPEL_BILLING["PAYMENT_PROVIDER"]` at the dotted path.
- **Don't bypass webhook idempotency.** Never call `services.handle_*` from a custom
  endpoint without the `StripeWebhookEvent` claim + `select_for_update` protocol, and
  never accept webhook payloads without `verify_webhook`. At-least-once delivery will
  double-grant credits otherwise.
- **Don't import other `stapel-*` modules.** Cross-module effects go through comm
  events (`payment.completed`, `subscription.changed`), core signals, or HTTP. This
  module imports only `stapel_core`.
- **Don't touch `Wallet.balance` or create `Transaction` rows directly.** Use
  `credit()` / `debit()` — they take the wallet row lock and keep the
  `balance_after` ledger invariant. Direct writes corrupt the ledger.
- **Money stays in minor units.** `price_cents` / `amount_cents` are integers; wallet
  credits are integers, never fractional. No floats, no Decimal-in-major-units.
- **Don't read provider credentials at import time.** Read through `billing_settings`
  at call time (tests and multi-tenant hosts change settings after import).
- **Don't edit `catalog.py` to change packages/plans.** Override
  `STAPEL_BILLING["CREDIT_PACKAGES"]` / `["PLANS"]` — the `*_BY_SLUG` views re-read
  config on every access.
- **Don't rewrite view bodies to change payload shapes.** Use the serializer seam
  (subclass + `request_serializer_class` / `response_serializer_class` + remount URL).
- **Don't mark a subscription cancelled locally when the provider call failed.**
  `SubscriptionCancelView` returns 502 and keeps local state — preserve that: the user
  must not believe billing stopped while the provider keeps charging.
- **Don't debit service-to-service without an `idempotency_key`.** Retries under
  at-least-once semantics will double-charge the wallet.

## App-layer override vs upstream contribution — rule of thumb

**App-layer override** (client-owned, no fork) when the change fits an extension point
above: a new payment backend (`PAYMENT_PROVIDER`), different packages/plans
(`CREDIT_PACKAGES` / `PLANS`), credentials, request/response payload shape
(serializer seams + URL remount), reacting to payments/subscriptions (connect to the
signals or subscribe to the comm events), extra billing-adjacent data (new app-layer
model with a FK).

**Upstream contribution** (Stapel-owned, via the contribution pipeline) when the change
alters module-owned contracts or invariants: new fields/indexes on `Wallet` /
`Transaction` / `Subscription` (migrations live here), new `TransactionType` or `Plan`
enum values, new webhook event types in `StripeWebhookView`'s routing, changes to the
`PaymentProvider` ABC surface, new emitted events or schema changes, ledger/idempotency
logic, new endpoints, bug fixes anywhere in this repo.

If a needed seam does not exist (e.g. a configurable webhook handler map or an
extensible `TransactionType`), the seam itself is an upstream contribution; the code
that plugs into it stays app-layer.
