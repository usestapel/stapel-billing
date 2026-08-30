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
| Models (`models.py`) | `Wallet` (one per user; `balance` = maintained **cache** of the live lots), `CreditLot` (`source`, `credits_initial`/`credits_remaining`, `expires_at`, `granting_transaction`), `CreditHold` (a reservation: `credits`, `status`, `idempotency_key` unique per wallet, `expires_at`) + `HoldAllocation` (which lot each held credit came from), `Transaction` (immutable ledger: `credits_delta`, `balance_after`, `amount_cents`, `lot`, JSON `metadata`), `Subscription` (one per user, Stripe-backed; provider customer/subscription ids unique when set), `StripeWebhookEvent` (webhook idempotency log, `stripe_event_id` unique), `ProviderGrant` (one grant per provider *object* — `(provider, scope, external_id)` unique; since 0.11.0 also scopes `clawback` per refund event and `plan_bundle` per (wallet, plan, period) under `provider="local"`), `CreditDebt` (credits owed — `credits_outstanding`, `reason` `partial_debit`/`clawback`, `settled_at`; collected oldest-first by the next `credit()`), `PendingSubscriptionPeriod` (a billing period that arrived before the subscription it belongs to) |
| Services (`services.py`) | `credit()` / `debit()` (row-locked wallet ops writing ledger rows; `credit` requires `source` and takes `expires_at`, `debit` walks lots expiring-first and supports `idempotency_key`, raising `InsufficientCreditsError`), `hold()` / `capture()` / `release()` (reservations), `can_afford()` (read-only pre-flight — no lock, no rows), `grant_plan_bundle()` + `plan_bundle_entitlements()` (plan bundles no payment provider sells), `open_debts()` / `outstanding_debt()`, `claw_back_grant()`, `reconcile_wallet_balances()`, `get_provider()`, `create_checkout_session()`, `create_customer_portal()`, `cancel_provider_subscription()`, `verify_stripe_signature()`, `claim_provider_object()` (one grant per provider object), webhook handlers (`handle_checkout_completed`, `handle_invoice_paid`, `handle_subscription_updated`, `handle_subscription_deleted`, `handle_charge_refunded`, `handle_dispute_created`, `handle_credit_note_created`) |
| HTTP API (`urls.py`, `views.py`) | `wallet` (GET/PATCH), `wallet/transactions`, `products` (public catalog), `checkout`, `portal`, `subscription`, `subscription/cancel`, `webhooks/stripe`, `internal/debit` (service-to-service, `IsServiceRequest \| IsStaffUser`); every wallet/checkout/portal/subscription view denies anonymous (guest) sessions — `GuestDeniedMixin`, `stapel_anonymous_access = ANONYMOUS_DENIED` — while `products` and `webhooks/stripe` are declared `ANONYMOUS_ALLOWED` |
| Catalog (`catalog.py`) | `CreditPackage` / `PlanCatalogEntry` frozen dataclasses (`PlanCatalogEntry.entitlements: dict[str, int \| bool]` — feature switches / ceilings per plan); `CREDIT_PACKAGES`, `PLANS`, `CREDIT_PACKAGES_BY_SLUG`, `PLANS_BY_SLUG` are lazy views that re-read `STAPEL_BILLING` on every access |
| Entitlements (`entitlements.py`) | comm Functions `billing.check_entitlement` / `billing.debit` / `billing.can_afford` / `billing.hold` / `billing.capture` / `billing.release` (see "Events & functions" below); denial-reason constants `REASON_*` |
| Workers (`tasks.py`) | `expire_credit_lots` (hourly), `expire_holds` (hourly), `reconcile_wallets` (daily), `grant_plan_bundles` (daily, since 0.11.0); `get_billing_beat_schedule()` — the host composes the beat schedule explicitly. Plain callables first, Celery tasks second (cron works too). System checks `stapel_billing.W105` (expiry/holds/reconcile unscheduled) and `W106` (the default plan bundles credits with no grant worker) |
| Providers (`providers/`) | `PaymentProvider` ABC (`providers/base.py`), `StripeProvider` default implementation (`providers/stripe.py`, lazy credentials; refuses with `ProviderNotConfiguredError` when unconfigured, dev placeholder URLs only under `ALLOW_UNCONFIGURED_PAYMENT_PROVIDER`) |
| GDPR (`gdpr.py`, `apps.py`) | `BillingGDPRProvider` (section `"billing"`), registered with `stapel_core.gdpr.gdpr_registry` in `AppConfig.ready()`; export + delete (Stripe IDs cleared, wallet anonymised, ledger retained) |
| Public API (`__init__.py`, PEP 562 lazy) | the service primitives (`credit`, `debit`, `can_afford`, `hold`, `capture`, `release`, `grant_plan_bundle`, `claw_back_grant`, `get_provider`), their result types (`DebitResult`, `Affordability`, `ClawbackResult`), the exceptions (`InsufficientCreditsError`, `HoldNotFoundError`, `HoldStateError`, `HoldKeyResolvedError`), the comm Function names (`CHECK_ENTITLEMENT`, `DEBIT`, `CAN_AFFORD`, `HOLD`, `CAPTURE`, `RELEASE`), `check_entitlement`, `billing_settings`, `PaymentProvider`, `get_stripe_handler` / `stripe_handlers` |

Money is always in **minor units** (`price_cents`, `amount_cents` — integers); wallet
credits are **integers, never fractional**.

## Wallet = lots (since 0.8.0)

A wallet is **a set of `CreditLot` rows**, not one integer. Every credit enters as a
lot that remembers where it came from (`source`) and when it dies (`expires_at`,
`NULL` = never):

| Grant | `source` | `expires_at` |
|---|---|---|
| Package purchase (`handle_checkout_completed`) | `purchase` | `NULL` — bought with a card, does not evaporate |
| Plan bundle at checkout / on renewal (`handle_invoice_paid`) | `subscription` | the period end that paid for it |
| Anything a host grants | `grant` / `adjustment` | the host's call |
| Credits handed back by `release()` / a short `capture()` | `hold_release` | **the expiry of the lot it came from** |

`credit()` therefore *requires* `source` (the 0.8.0 breaking change): a lot that
cannot say where it came from cannot be reasoned about later.

**Consumption order** — `debit()` and `hold()` walk
`order_by(F("expires_at").asc(nulls_last=True), "created_at", "id")`: expiring-soonest
first, non-expiring last. Credits die used, not unused. `Transaction.lot` names the lot
when exactly one was touched; the per-lot split is always on
`Transaction.metadata["lots"]`.

**`Wallet.balance` is a maintained cache**, not the truth — the truth is
`SUM(credit_lot.credits_remaining)` over live lots. It is recomputed from the lots
inside the same `select_for_update()` block as every mutation, so read paths (API
responses, entitlement checks, host pre-flights) keep the cheap scalar they already
expect. `services.reconcile_wallet_balances()` (daily, via the beat schedule) reports
— never repairs — any wallet where the two disagree.

**Expiry** — `expire_credit_lots` zeroes lots past `expires_at` and writes one
`TransactionType.EXPIRATION` row each. Every wallet operation also sweeps the wallet it
locks, so a ledger row can never record a `balance_after` that includes dead credits.
An unscheduled expiry is a silent-retention bug, so `stapel_billing.W105` warns when a
beat-driven host has no entry for it.

### Hold lifecycle

`hold()` → `capture()` / `release()` is the pre-flight form of a charge, for work whose
real cost is known only afterwards (an LLM call, a transcription of unknown length).

```
hold(credits=15, idempotency_key=...)      # lots decremented NOW; no ledger row yet
   ├── capture(actual_credits=12)          # ledger row -12; the extra 3 go back to
   │                                       # their own lots, original expiry intact
   ├── capture(actual_credits=18)          # takes 3 more from the live lots;
   │                                       # may raise InsufficientCreditsError
   ├── release()                           # all 15 go back, expiry preserved
   └── expire_holds (expires_at passed)    # same as release, recorded as `expired`
```

* A hold is a **real withdrawal**, not an overlay. `balance` is always "spendable now",
  so a caller never computes `balance - sum(open holds)` — an aggregate two concurrent
  requests would both read before either wrote.
* **No ledger row is written by `hold()` or `release()`**: nothing was billed. The
  charge appears at `capture()`. Consequence: `balance_after` is the spendable balance
  at that moment, *not* the running sum of `credits_delta`.
* **Refunds are per-lot.** `HoldAllocation` records which lot each held credit came
  from, and the refund returns it as a `hold_release` lot carrying that lot's
  `expires_at`. Without it, a released subscription bundle would come back as credits
  that never expire. A portion whose lot expired while the work ran comes back already
  dead and is swept into an `EXPIRATION` row — reserved credits run out the clock, they
  are not renewed.
* **Idempotency**: `hold()` needs `idempotency_key` (unique per wallet). `capture()` /
  `release()` do not — `hold_id` already is one. A repeated capture returns the original
  charge; a repeated release is a no-op.
* **A resolved key is a refusal, not a hold (0.11.0).** The short-circuit only covers a
  hold that is still `held`. Until 0.11.0 it matched **any** status, so re-using a key
  whose hold had been released, captured or expired answered "reserved" with nothing
  reserved — and the capture that followed failed with `hold_not_held`, at the point
  where the work was already done and had to be given away. `hold()` now raises
  `HoldKeyResolvedError` (carrying the hold and its status), and `billing.hold` answers
  `ok=False, reason="hold_already_resolved"` with that `status`. A genuine retry of a
  finished operation has nothing left to do; a NEW operation needs a new key.
* `expires_at` defaults to `STAPEL_BILLING["HOLD_DEFAULT_TTL_SECONDS"]` from now, and
  `expire_holds` is the crash-safety net: a worker that dies between hold and answer
  must not lock a customer's credits forever.

## Credits that no payment provider grants (since 0.11.0)

Until 0.11.0 credits entered a wallet only through a Stripe webhook. A plan
whose bundle is not sold through Stripe — the free tier above all, but also an
invoiced enterprise agreement or a plan a host assigns from its own admin —
therefore granted **nothing at all**: its `monthly_credits_included` was a
number in a catalogue that no code path read, and those users started and
stayed at zero.

```python
from stapel_billing import services

services.grant_plan_bundle(user=user, plan_slug="free")          # current month
services.grant_plan_bundle(user=user, plan_slug="pro",
                           period_key="2026-09")                 # a named period
```

* **Idempotent per `(wallet, plan, period_key)`**, through the same unique
  `ProviderGrant` row the Stripe grants use (`provider="local"`, scope
  `plan_bundle`). Two sweeps in a month, an operator re-running the job and a
  host that also grants at signup all produce one bundle.
* **The lot expires** — default `expires_at` is the end of the `YYYY-MM` period
  the key names. A bundle that belongs to a period must die with it, or a free
  tier quietly becomes a growing balance nobody sold. A host on another cadence
  passes its own `period_key` *and* `expires_at`; an unparseable key with no
  explicit deadline is refused rather than granted forever.
* **Who is entitled is a seam**, `STAPEL_BILLING["PLAN_BUNDLE_ENTITLEMENTS"]`,
  because plan membership is host knowledge. The default reads the local
  `Subscription` row and **skips anything carrying a `stripe_subscription_id`** —
  those are granted by the provider's invoices, and granting again here would
  double them. It walks wallets, not users: a user with no wallet row has never
  touched billing, and scanning the whole user table is not a library's call.
* `tasks.grant_plan_bundles` drives it (daily in `get_billing_beat_schedule()`).
  Daily rather than monthly because the grant is idempotent per period, so the
  cost is one claim query per entitled wallet and the gain is that a wallet
  created on the 14th gets that period's bundle on the 14th.
* System check **`stapel_billing.W106`** fires when the default plan bundles
  credits and a beat-driven host has no entry for the worker.

## Partial charges and tracked debt (since 0.11.0)

`debit()` is all-or-nothing by default and stays that way. But a consumer that
has **already done the work** had only two options when the wallet came up
short: refuse a completed job, or serve it and let the shortfall vanish. Free
service that nothing records is indistinguishable from a bug.

```python
result = services.debit(user=user, credits=25, type=..., allow_partial=True)
result.debited      # 10 — what the wallet actually covered
result.shortfall    # 15 — what it did not
result.debt_id      # the CreditDebt row that remembers it
```

* Default behaviour is untouched: without `allow_partial` the call still raises
  `InsufficientCreditsError` and returns a `Transaction`. With it, the return
  value is a `DebitResult`.
* A partial debit **always writes a ledger row**, even at `credits_delta = 0`:
  the charge happened, and a debt with no transaction behind it is a number
  nobody can trace to the work that caused it.
* **A debt is not a negative balance.** The balance counts credits that exist,
  and credits already spent do not come back into existence because a charge
  failed. Driving the balance below zero would make every lot, every expiry and
  every `balance_after` in the ledger untrue; the shortfall gets its own row
  instead.
* **Collection is automatic**: every `credit()` (and therefore every grant,
  renewal, purchase and admin grant) settles the wallet's outstanding debts
  oldest-first, inside the same row lock, before the credits become spendable.
  Each collection is a real debit — it walks the lots expiring-soonest first and
  writes its own ledger row. Pass `settle_debts=False` to opt one grant out.
* The wallet endpoint publishes `debts[]` and `debt_outstanding`: an owner told
  only the balance cannot find out why their next top-up partly disappeared.

## Refund, dispute and credit-note clawback (since 0.11.0)

Three built-in webhook handlers — `charge.refunded`, `charge.dispute.created`,
`credit_note.created` — take back the credits a refunded payment bought. Before
0.11.0 nothing handled money going the other way: the card was refunded and the
credits stayed spendable.

The grant is found by the provider ids it recorded on itself (invoice, payment
intent, charge); an event naming none of them is logged and left alone rather
than guessed at. `claw_back_grant()` then splits the amount three ways:

| Bucket | Meaning |
|---|---|
| `reclaimed` | still sitting in the lots **that grant created** — taken straight back |
| `forgiven` | already expired unspent — the deployment took those credits back once already, and charging for them twice would be a second clawback of the same credits |
| `outstanding` | already spent — becomes a `CreditDebt` the next top-up collects |

Only the refunded grant's own lots are touched. Taking the shortfall out of
whatever else the wallet holds would pay a refunded subscription back with the
credits the customer bought for cash. Idempotent per **event** (scope
`clawback`) rather than per charge: a partial refund followed later by a second
partial refund of the same charge is two real clawbacks.

## Read-only affordability (since 0.11.0)

`services.can_afford(user=..., credits=N)` / `billing.can_afford` answers "would
this charge go through" **without** taking a lock, creating a hold, a lot or an
allocation. It exists because consumers were pre-flighting with a real `hold()`
they immediately released — and every such probe returned the credits as a
*new* `hold_release` lot, so an affordability check per request grew the
customer's lot table without bound and slowed every later spend.

It answers from the lots, not from `Wallet.balance`: lots past their deadline
that the sweep has not reached yet still count in the cache, and a debit would
refuse them. Nothing is locked, so two callers can both be told yes — the
caller that needs the answer to *stay* true takes a hold.

## Extension points (fork-free)

### Settings — `STAPEL_BILLING` namespace (`conf.py`)

`billing_settings = AppSettings("STAPEL_BILLING", ...)` from `stapel_core.conf`.
Resolution order per key: `settings.STAPEL_BILLING[key]` → flat Django setting of the
same name → environment variable → default. All keys are read **lazily at call time**
(never frozen at import); caches invalidate on `setting_changed`.

Two rules the boolean keys and `PAYMENT_PROVIDER` add to that order:

* **Environment values are text, not truth.** An env var arrives as the string an
  operator typed, and `bool("false")` is `True`. The default-off switches
  (`ALLOW_*`) are on only for `1` / `true` / `yes` / `on`; the default-on gate
  (`STRICT_CHECKOUT_RECONCILIATION`) is off only for `0` / `false` / `no` / `off`,
  so a stray or empty value cannot open a hole in either direction. A Python `bool`
  in `settings.STAPEL_BILLING` is used as-is.
* **`PAYMENT_PROVIDER` is `no_env`.** It names the class that handles money; the
  name is generic enough that a same-named variable in a shared pod could swap it.
  It resolves from `settings.STAPEL_BILLING`, a flat Django setting, or the default
  — an environment variable is ignored.

| Key | Default | What it customizes |
|---|---|---|
| `PAYMENT_PROVIDER` | `"stapel_billing.providers.stripe.StripeProvider"` | The payment backend. In `import_strings` — resolved via `import_string`, must be a `PaymentProvider` subclass (enforced by `get_provider()`). |
| `STRIPE_SECRET_KEY` | `""` | Stripe API key for the default provider (read lazily per call). Empty means the provider is **unconfigured**: checkout/portal/cancel refuse (502) and boot check `stapel_billing.E104` fails. |
| `STRIPE_WEBHOOK_SECRET` | `""` | Stripe webhook signature secret (read lazily per call). |
| `CREDIT_PACKAGES` | `catalog.DEFAULT_CREDIT_PACKAGES` (starter/standard/bulk) | One-off credit package catalog. List of dicts or `CreditPackage` instances. |
| `PLANS` | `catalog.DEFAULT_PLANS` (free/pro/team/enterprise) | Subscription plan catalog. List of dicts or `PlanCatalogEntry` instances. |
| `CHECKOUT_SUCCESS_URL` | `""` | Fallback Stripe Checkout success redirect when the request carries none. Empty → derived from the flat `FRONTEND_URL` setting/env (`{FRONTEND_URL}/billing/success`); with neither configured the request fails with `error.400.redirect_url_not_configured`. |
| `CHECKOUT_CANCEL_URL` | `""` | Same resolution for the cancel redirect (`{FRONTEND_URL}/billing/cancel`). |
| `PORTAL_RETURN_URL` | `""` | Same resolution for the customer-portal return URL (`{FRONTEND_URL}/billing`). |
| `REDIRECT_ALLOWED_ORIGINS` | `[]` | Exact origins (`"https://app.example.com"`) a **request-supplied** `success_url` / `cancel_url` / `return_url` may point at, on top of the origins of the three fallbacks above and of `FRONTEND_URL`. Anything else is refused with `error.400.redirect_url_not_allowed` (`redirects.py`) — the provider renders the link, so an unchecked one turns checkout into a phishing hop. |
| `ALLOW_UNVALIDATED_REDIRECT_URLS` | `False` | Escape hatch: forward request-supplied redirect URLs unchecked. That is an authenticated open redirect; system check `stapel_billing.W102` says so on every boot. |
| `ENTITLEMENT_KEYS` | `[]` | Feature keys this deployment recognises, on top of the ones the shipped plan ladder declares. A key outside the vocabulary is denied by `billing.check_entitlement` (`reason="unknown_key"`), and a configured plan that mentions one fails the boot check `stapel_billing.E101`. |
| `ALLOW_UNKNOWN_ENTITLEMENT_KEYS` | `False` | Escape hatch: let an unrecognised entitlement key be allowed again (and silence `E101`). System check `stapel_billing.W101` names it on every boot, the way `W102` names the redirect hatch. |
| `ALLOW_UNKNOWN_PLAN_SLUGS` | `False` | Escape hatch: let a plan slug that is **not** in `PLANS` (a renamed/retired plan still on a live subscription, or a default plan outside the host's ladder) be treated as "no ceilings" again instead of `reason="unknown_plan"` (and silence `E102`). |
| `ALLOW_UNCONFIGURED_PAYMENT_PROVIDER` | `False` | Escape hatch for a developer's machine: let an unconfigured provider answer with dev placeholders (fake checkout session, portal link to nowhere, cancel that cancels nothing) instead of refusing. Silences `E104`, reported by `W104`. |
| `HOLD_DEFAULT_TTL_SECONDS` | `3600` | How long a `services.hold()` reservation stays valid when the caller sets no deadline of its own — the bound on how long a crashed pipeline can keep a customer's credits reserved. `expire_holds` releases holds past it. `0` (or `None`) means "never sweep", which is a decision, not a default. |
| `STRIPE_WEBHOOK_HANDLERS` | `{}` | Stripe event type → handler, merged **over** `webhooks.BUILTIN_STRIPE_HANDLERS` (last-wins per type — same canon as `STAPEL_NOTIFICATIONS["TYPES"]`). A value is a callable taking the event dict, a dotted path to one, or `None` to switch a built-in off. This is how a host adds an event type the library does not carry, or replaces what `checkout.session.completed` does — without forking the view. An entry that does not import is boot check `stapel_billing.E106`. |
| `PLAN_BUNDLE_ENTITLEMENTS` | `"stapel_billing.services.default_plan_bundle_entitlements"` | Who receives a plan bundle no payment provider grants. In `import_strings` and `no_env` (it decides who is handed free credits). A zero-argument callable yielding `{"user_id": ..., "plan": <slug>}` mappings — optionally `wallet_id` / `credits` / `period_key` / `expires_at` — or `(user_id, plan_slug)` pairs. Plan membership is HOST knowledge; the default reads the local `Subscription` row and skips Stripe-billed ones. |
| `LEGACY_IDEMPOTENCY_JSON_LOOKUP` | `True` | Keep answering the pre-0.11.0 spelling of the retry key (`metadata->>'idempotency_key'`) after the indexed `Transaction.idempotency_key` column misses. Expand-only: rows written before 0.11.0 carry the key only in `metadata`, so turning this off before backfilling the column makes every old key a fresh charge. |
| `STRICT_CHECKOUT_RECONCILIATION` | `True` | Reconcile the provider's checkout session (mode, payment status, currency, amount vs. the catalog price, buyer) before granting credits or a plan. Off means the session's metadata is taken on faith. |

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
seam). `Wallet`, `CreditLot`, `CreditHold`, `HoldAllocation`, `Transaction`, `Subscription`,
`StripeWebhookEvent`, `ProviderGrant` have fixed `db_table` names (`billing_*`). Extend user-visible billing data in an app-layer model
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
live in `schemas/emits/`, `schemas/consumes/` and `schemas/functions/`.

**Emits** (from `services.py`, keyed by `user_id`):

| Event | Payload (required) | When |
|---|---|---|
| `payment.completed` | `user_id`, `amount_cents`, `currency`, `transaction_id`, `created_at` (+ optional `package` / `plan`) | Package purchase, initial plan bonus, plan renewal grant |
| `subscription.changed` | `user_id`, `plan`, `status` (+ `current_period_end`) | Subscription created / status updated / deleted |
| `gdpr.section.erased` | `owner`, `subject_type`, `subject_key`, `correlation_id`, `receipt_id`, `counts` | Receipt for one erasure — same transaction as the erasure (see **Erasure**) |
| `gdpr.owner.alive` | `owner`, `subject_types` (+ `correlation_id`) | Answer to the liveness probe |

**Consumes** (`actions.py`, `@on_action`; handlers must be idempotent — delivery is at-least-once):

| Event | Handler | Effect |
|---|---|---|
| `gdpr.erasure.requested` | `handle_erasure_requested` | Erase this owner's slice of the subject, receipt `gdpr.section.erased` with counts |
| `gdpr.owner.probe` | `handle_owner_probe` | Answer `gdpr.owner.alive` — from the same module, which is what makes it evidence |
| `user.deleted` | `handle_user_deleted` | DEPRECATED account signal, routed through the same `erase_subject("account", …)` |
| `user.merged` | `handle_user_merged` | The other half of an account's life cycle (stapel-auth 0.30.0), routed through `services.merge_wallets`. **Merge policy: the credits MOVE and the ledger says so** — the merged wallet's lots move (never a summed integer: a lot carries its own expiry, and adding two balances would turn an expiring bundle into non-expiring cash), transactions/holds/debts follow, ONE explicit `ADJUSTMENT` row on the survivor explains the balance change, and the merged wallet is closed at zero with a `merged_into` stamp. **Exactly once**: the row carries a deterministic key derived from the pair of ids (`services.merge_idempotency_key`), so a redelivery credits nothing. Answering only `user.deleted` is `stapel_core.lifecycle.E001` |

(`schemas/consumes/user.deletion_initiated.json` is declared, but `actions.py` does not
subscribe to it.)

**Functions provided** (`entitlements.py`, registered in `apps.ready()`; payload
contracts in `schemas/functions/`):

| Function | Payload | Returns |
|---|---|---|
| `billing.check_entitlement` | `user_id`, `key` (+ `quantity`=1 — the prospective total; billing tracks no usage) | `{allowed, limit, reason}` — checks `key` against the effective plan's `PlanCatalogEntry.entitlements`. `bool` value = feature switch, `int` = ceiling (`quantity <= value`), key absent from *this plan* but part of the deployment's vocabulary = **allowed, no limit** (how the upper plans say "unlimited"), key outside the vocabulary = **denied** with `reason="unknown_key"` (see `ENTITLEMENT_KEYS`). No/cancelled/incomplete subscription resolves to the default plan (`free`). |
| `billing.debit` | `user_id`, `credits`, `idempotency_key` (required — comm is at-least-once; + optional `type`/`description`/`metadata`/`allow_partial`) | `{ok, balance, reason, transaction_id?}` — wrapper over `services.debit` with its idempotency contract; failures are structural (`ok=false` + `insufficient_credits` / `user_not_found`), never exceptions. With `allow_partial=true` the wallet is charged what it has and the answer also carries `requested` / `debited` / `shortfall` / `debt_id`. |
| `billing.can_afford` | `user_id`, `credits` | `{ok, affordable, balance, shortfall, reason}` — a pure read: no lock, no hold, no lot. `ok=false` means the question was unanswerable (`user_not_found`); `ok=true, affordable=false` is the real answer "no" (`insufficient_credits`). Nothing is reserved, so two callers can both be told yes. |
| `billing.hold` | `user_id`, `credits`, `idempotency_key` (required, unique per wallet; + optional `type`/`description`/`metadata`/`expires_in_seconds`) | `{ok, hold_id, balance, reason}` — reserves the credits out of the lots; `reason` is `insufficient_credits` / `user_not_found` / `hold_already_resolved` (the key names a hold that is captured, released or expired — the answer carries that hold's `hold_id` and `status`, and **nothing is reserved**). |
| `billing.capture` | `hold_id` (+ optional `actual_credits`/`description`/`metadata`) | `{ok, balance, transaction_id, reason}` — bills the hold and writes the ledger row; `reason` is `hold_not_found` / `hold_not_held` / `insufficient_credits` (the last leaves the hold *held*, so the caller may capture at the reserved amount instead). |
| `billing.release` | `hold_id` | `{ok, balance, status, reason}` — gives the credits back with their original expiry; a hold that is already resolved is a no-op. `reason` is `hold_not_found`. |

Callers that must degrade when billing is absent catch `FunctionNotRegistered` /
`FunctionRouteNotConfigured` and treat it as "allowed" (see stapel-workspaces).
The HTTP endpoint `internal/debit` remains for deployments routing over HTTP (pass
`idempotency_key` — duplicates short-circuit to the original transaction).

## Erasure

This module is a GDPR **data owner**. Its name is `billing` — the string on
`BillingGDPRProvider.section`, and the one a host must use in
`STAPEL_GDPR["DATA_OWNERS"]`, because an owner with two names is an owner whose
receipts land on nobody's part.

**Erasure removes the person; the bill stays.** A wallet's history is the product's
financial record — what was purchased, what was spent, what expired — so nothing here is
deleted. `stapel_billing.gdpr.erase_subject("account", user_id)` is the one operation,
reached identically by the in-process provider (monolith) and by the
`gdpr.erasure.requested` subscriber (fleet):

| What | Erasure does |
|---|---|
| `Wallet.user`, `Subscription.user` | detached (`NULL`); `user_pseudonym` = `erased:<hmac>` — a keyed HMAC-SHA256 under `SECRET_KEY`, the `stapel_video.presence.pseudonymize_user` funnel, truncated to 32 hex |
| `Transaction.description`, `CreditHold.description` | blanked — the argument is free-form, and a host that files a meeting title there has filed content |
| `Transaction.metadata`, `CreditHold.metadata` | cut to `LEDGER_METADATA_KEYS` (`package`, `plan`, `lot_id`, `lots`, `hold_id`, `source`, `expires_at`). Allowlist, not denylist: a key nobody anticipated falls on the erasing side |
| `Subscription.stripe_customer_id` / `stripe_subscription_id`, and the `StripeWebhookEvent.payload` rows keyed on them | cleared / replaced with `{"erased": true}` — a Stripe payload carries the person's email, address and card details. The webhook **row** stays: its `stripe_event_id` is the unique key that stops a redelivery from crediting a wallet twice |
| amounts, credit counts, `balance_after`, lot expiries, timestamps | untouched — an erasure that restated a closed reporting period would be a bookkeeping change wearing a privacy costume |

`CreditLot` and `HoldAllocation` are deliberately absent: they carry no free text and no
id of their own, so the wallet's pseudonymization *is* their erasure, and counting them
would inflate a receipt with work that was not done.

**Subject types: `account` only.** Every row in this package hangs off a `Wallet` or a
`Subscription` keyed by exactly one id — the user's. There is no `workspace_id` column
and no `billing.*` payload that carries an entity id, so a `workspace` or `meeting`
erasure has no key to match on here; claiming the type would produce a receipt for work
nobody could have done. The probe answers what this module can really erase, not what a
settings file hoped for.

**The receipt.** `gdpr.section.erased` carries `counts = {wallets, subscriptions,
transactions, credit_holds, webhook_events}` — rows *touched* — and is emitted inside the
same transaction as the erasure, so a rollback takes the receipt with it. Redelivery is
expected (at-least-once): the second run finds a detached wallet, touches nothing, and
receipts zeroes under the same deterministic `receipt_id`.

**Schema note (0.9.0).** `Wallet.user` / `Subscription.user` became nullable and moved
from `CASCADE` to `SET_NULL` (migration `0004`). Both changes were preconditions, not
polish: the 0.8.x provider ran `update(user_id=None)` against a NOT NULL column inside a
bare `except Exception: pass`, so it raised on every wallet and reported success; and a
`CASCADE` from the auth user row hit `Transaction.wallet`'s `PROTECT` and raised
`ProtectedError`, failing the account deletion on the very rows it was meant to preserve.

### Django signals

Sent via `stapel_core.signals` (in-process, host apps connect receivers freely):

| Signal | Sender | Kwargs | When |
|---|---|---|---|
| `payment_completed` | `Transaction` | `user`, `credits`, `transaction` | Alongside every `payment.completed` emit |
| `subscription_changed` | `Subscription` | `subscription` | Alongside every `subscription.changed` emit |

### Stripe webhook routing (registry seam)

`StripeWebhookView` dispatches through `stapel_billing.webhooks`, not an if/elif chain: the effective map is `STAPEL_BILLING["STRIPE_WEBHOOK_HANDLERS"]` merged over `BUILTIN_STRIPE_HANDLERS` (last-wins per event type). A value is a callable taking the raw event dict, a dotted path to one, or `None` to disable a built-in; readers are `get_stripe_handler(event_type)`, `stripe_handlers()` and `registered_stripe_events()`. Built-ins are dotted paths resolved at dispatch, so replacing `services.handle_*` actually replaces what runs. An event type with no handler is acknowledged and logged, exactly as before.

What the registry does **not** own is the point: signature verification, the idempotency claim, the row lock and the `processed_at` mark stay in the view and wrap every handler, so an override cannot opt out of the guarantees that keep one paid checkout from being credited twice.

### Webhook idempotency (contract, not a seam)

`StripeWebhookView` verifies the signature via the configured provider, then claims the
event: `StripeWebhookEvent.get_or_create(stripe_event_id=...)` → already-processed
events (with `processed_at` set) return `{"status": "duplicate"}`; a row **without**
`processed_at` is a previous failed attempt and is reprocessed on the provider's retry.
The handler runs under `select_for_update()` on the event row, and the `processed_at`
mark commits atomically with the handler's effects (credit grants, emits). That claim covers one *event*; the business object an
event describes is claimed separately by `services.claim_provider_object()` under the
`ProviderGrant` unique constraint, because Stripe describes one paid invoice (or one
completed session) in several distinct events that can be delivered concurrently.
Checkout grants are additionally reconciled against the catalog entry the session's
metadata names — mode, payment status, currency, amount (net of coupon) and buyer —
before any credit is written (`STRICT_CHECKOUT_RECONCILIATION`). The refund handlers
claim per **event** (scope `clawback`), because two partial refunds of one charge are
two real clawbacks.

**Ordering is not promised** (0.11.0). `customer.subscription.*` can land before the
checkout that creates the local row, and that payload is the only one carrying the
billing period. The handler used to return silently, so the checkout granted an
*undated* subscription lot and the event that would have dated it was already marked
processed — the bundle a cancelled subscriber kept never expired. Now the period is
parked in `PendingSubscriptionPeriod` under the provider's subscription id, and the
checkout claims it before it reads `current_period_end`. `handle_subscription_deleted`
also stamps the period, since a cancellation is the last event that carries one.

### Admin categories (`stapel_core.access`, admin-suite AS-5)

`StripeWebhookEvent` and `ProviderGrant` are decorated `@access.ops` (read-only journal — add/change/delete
forbidden for everyone including superuser at the admin layer; view requires HIGH
clearance) and its `ModelAdmin` subclasses `stapel_core.django.admin.base.
StapelModelAdmin`. It is an idempotency/delivery log for inbound Stripe webhooks
(`stripe_event_id`, `payload`, `processed_at`) that staff never edit — mutation only
happens through the webhook claim protocol above. `Wallet`, `CreditLot`, `CreditHold`, `HoldAllocation`, `Transaction`, and
`Subscription` are business tables and stay undecorated (implicit `@access.standard`);
their `ModelAdmin`s make the lot/hold fields read-only, because moving credits outside
`services.py` skips the lock and the ledger row that explains the move.
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

### Contract emission — the `schema` + `flows` + `errors` triad

This module emits its **own** machine-readable API contract, per-module, so the
frontend codegen reads a committed, version-pinned artifact instead of checking
out the monolith aggregate at floating `main` (contract-pipeline.md §2, verdict
**A**: contract = a reviewable commit, like `docs/errors.json` always was; copied
from stapel-auth's reference implementation, contract-pipeline.md §7 Wave 1). The
triad lives in `docs/`:

```
docs/schema.json   drf-spectacular OpenAPI, this module only, canonical /billing/api/ prefix
docs/flows.json    generate_flow_docs machine artifact — [] (no @flow_step in this module)
docs/errors.json   generate_error_keys registry (unchanged by this — already the etalon)
```

The emitted `schema.json` is **byte-identical to the monolith aggregate's billing
slice** — the 9 paths under `/billing/api/` plus the transitive `$ref` component
closure (14 components) they reference.
`tests/test_contract.py::test_matches_monolith_billing_slice` asserts it in the
workspace (skipped in module CI, where the monolith isn't checked out).

**Harness** (`_codegen_settings.py` / `codegen_urls.py` / `_codegen.py`, copied
from stapel-auth): `codegen_urls.py` mounts only `stapel_billing.urls` — unlike
auth+gdpr, billing has no sibling co-mounted under the same monolith prefix.
`_codegen.py` pins `spectacular_settings.SCHEMA_PATH_PREFIX = "/"` (same
multi-module-common-prefix reproduction as auth) and, **billing-specific**, also
calls `stapel_core.django.openapi.swagger._register_jwt_auth_extension()`
directly. That registration is normally a side effect of some *other* module's
`urls.py` calling `get_swagger_urls()` (auth+gdpr both do) — process-global, so
it silently applies to every operation drf-spectacular sees in the same schema
pass, including billing's, in the monolith. Billing has no such sibling to
co-mount (one under `billing/api/` would incorrectly add its paths to this
module's own schema), so the harness reproduces the side effect directly instead
of borrowing a sibling's endpoints. Without it, drf-spectacular logs "could not
resolve authenticator" and every operation is missing its `security` /
`JWTCookieAuth` block relative to the monolith slice.

**Gate:** `make contract` re-emits; `make contract-check` regenerates into a temp
dir and diffs. Regenerate after any serializer/view/url/error change:

    make contract        # or: python -m stapel_billing._codegen --out docs

then commit `docs/{schema,flows,errors}.json`.

## Anti-patterns

- **Don't fork to add a payment provider.** Subclass `PaymentProvider` in the app layer
  and point `STAPEL_BILLING["PAYMENT_PROVIDER"]` at the dotted path.
- **Don't bypass webhook idempotency.** Never call `services.handle_*` from a custom
  endpoint without the `StripeWebhookEvent` claim + `select_for_update` protocol, and
  never accept webhook payloads without `verify_webhook`. At-least-once delivery will
  double-grant credits otherwise.
- **Don't guard a grant by reading the ledger.** "Has this invoice been credited?"
  answered with a `filter(...).exists()` over `Transaction.metadata` is a
  read-then-write: two deliveries that both read before either writes both pay out.
  Claim the object with `services.claim_provider_object()` inside the granting
  transaction and let the unique constraint pick the winner.
- **Don't grant on provider metadata alone.** Metadata says what we asked for; the
  session's `mode`, `payment_status`, `currency` and `amount_total` say what actually
  happened. Reconcile them against the catalog entry before crediting — an unpaid or
  underpaid session completes too.
- **Don't import other `stapel-*` modules.** Cross-module effects go through comm
  events (`payment.completed`, `subscription.changed`), core signals, or HTTP. This
  module imports only `stapel_core`.
- **Don't touch `Wallet.balance`, `CreditLot.credits_remaining` or create `Transaction`
  rows directly.** Use `credit()` / `debit()` / `hold()` / `capture()` / `release()` —
  they take the wallet row lock, walk the lots in the one sanctioned order and
  recompute the balance cache from what they actually moved. A hand-set balance is a
  wallet with nothing in it to spend, and `reconcile_wallets` will name it.
- **Don't compute `available = balance - sum(open holds)`.** `balance` is already
  spendable-now: `hold()` really removes the credits. Recomputing an overlay is a
  read-then-write two concurrent requests both win.
- **Don't debit an estimate and refund the difference later.** Use `hold()` +
  `capture(actual_credits=...)`; a refund that never runs because the worker died is a
  charge for work nobody did.
- **Don't credit released credits back with `credit()`.** `release()` restores them
  per-lot with the original expiry; a plain `credit()` would turn an expiring
  subscription bundle into credits the deployment can never reclaim.
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
- **Don't pre-flight with a real `hold()` you then release.** Use `can_afford()` /
  `billing.can_afford`. Every released hold returns the credits as a *new*
  `hold_release` lot, so probing on each request fragments the wallet's lots without
  bound and slows every later spend.
- **Don't re-use a `hold()` idempotency key for a new operation.** A key belongs to one
  reservation for the life of the wallet; once its hold is captured, released or
  expired, `hold()` refuses (`HoldKeyResolvedError` / `hold_already_resolved`) instead
  of handing back a hold that reserves nothing.
- **Don't hand out work for free when the wallet is short.** Either refuse (the default
  `debit()`), or serve it with `allow_partial=True` so the shortfall becomes a
  `CreditDebt` the next top-up collects. Unrecorded free service is indistinguishable
  from a bug, and nothing will ever collect it.
- **Don't write off a `CreditDebt` by hand, and don't drive a balance negative.** Debts
  are collected by `credit()` under the wallet's row lock; a hand-edited row moves
  credits with no ledger entry behind it.
- **Don't claw a refund back out of the wallet's other lots.** `claw_back_grant()`
  touches only the lots the refunded grant created; the rest becomes a debt. Anything
  else pays a refunded subscription back with credits the customer bought for cash.
- **Don't grant a plan bundle with a bare `credit()` in a loop.** Use
  `grant_plan_bundle()`: it claims `(wallet, plan, period)` under a unique constraint
  and dates the lot to the period. A loop that grants on its own will double-grant the
  first time it is re-run.
- **Don't grant a plan bundle for a Stripe-billed subscription.** The invoices already
  do it; the default entitlement resolver skips any `Subscription` carrying a
  `stripe_subscription_id`, and a custom resolver must too.

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
