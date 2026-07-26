# Changelog

All notable changes to stapel-billing are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.5.1] — 2026-07-26

### Added — `error-keys/` is finally mounted

`BillingErrorKeysView` has existed since the port but no `urls*.py` ever mounted it — in
*any* stapel library. stapel-translate's `error_collector` polls
`/{prefix}/api/v1/error-keys/` on every service, so the whole endpoint class
answered 404 from Django's URL resolver and the collector harvested nothing
while reporting a plain `HTTP 404`. It is now mounted in `urls_v1.py` at
`error-keys/` (v1 canon), service/staff-gated as the base view declares.

Deliberately **not** in the contract triad: `ErrorKeysView` sets
`schema = None` and `/error-keys` is on the flows allowlist, so `make
contract` is a no-op diff — this is infrastructure, not product surface.

## [0.5.0] - 2026-07-24

Entitlements seam (workspaces-org program §D1, Wave 1). All additive — no
schema/DB changes (entitlements live in the frozen catalogue dataclass,
not in models; `docs/` contract triad unchanged).

### Added
- `PlanCatalogEntry.entitlements: dict[str, int | bool]` (default `{}`) —
  per-plan feature switches (`bool`) and ceilings (`int`). Default-plan
  ladder: free `{workspaces.org: False, workspaces.members.max: 5,
  workspaces.provision_user: False}`; pro 25 members / team 100 members
  with org+provision `True`; enterprise org+provision `True` with no
  member ceiling (an absent key is unrestricted).
- New `entitlements.py` with comm Functions (registered in
  `apps.ready()`, payload contracts in `schemas/functions/`):
  - `billing.check_entitlement` `{user_id, key, quantity=1}` →
    `{allowed, limit, reason}` — resolves the user's effective plan
    (subscription in `active`/`trialing`/`past_due`; none or
    `cancelled`/`incomplete` → the default `free` plan) and checks the
    key. Keys not declared by the plan **allow** (unknown keys never
    deny — conservative OSS default). `quantity` is the prospective
    total (caller counts usage; billing doesn't).
  - `billing.debit` `{user_id, credits, idempotency_key, metadata?,
    description?, type?="adjustment"}` → `{ok, balance, reason,
    transaction_id?}` — comm wrapper over `services.debit` (previously
    internal-HTTP-only) with the same idempotency short-circuit;
    `idempotency_key` required (comm is at-least-once), failures are
    structural (`insufficient_credits` / `user_not_found`), never
    exceptions.
- Public API: `CHECK_ENTITLEMENT`, `DEBIT`, `check_entitlement` exported
  lazily from the package root (the `billing.debit` provider is not
  re-exported — the package-level `debit` stays `services.debit`).

## [0.4.15] - 2026-07-17

Fix-up #2: 0.4.14's regen still baked the old version into
`docs/capabilities.json` (`make contract` ran before the version bump
landed). Re-ran with 0.4.15 already in `pyproject.toml`; verified match,
suite green.

## [0.4.14] - 2026-07-17

Fix-up: 0.4.13's CI/publish failed on contract drift — `docs/capabilities.json`
embeds the package version and wasn't regenerated for the 0.4.13 bump.
Regenerated via `make contract`; no other diff.

## [0.4.13] - 2026-07-17

Fleet follow-up to stapel-core 0.12.0 (legacy shim sweep). No source
changes needed. Full suite green against core 0.12.0.

### Changed
- `stapel-core` dependency ceiling `<0.12` → `<0.13`.

## [0.4.12] - 2026-07-17

### Changed
- `stapel-core` ceiling raised `>=0.10,<0.11` → `>=0.10,<0.12` (core 0.11
  fleet re-pin: default bus, nav, config-checks, error params/language —
  additive for modules).
- `docs/schema.json` regenerated against core 0.11.2 — error object gained
  `error_language` field and a reworded `error` description; no drift
  otherwise.

## [0.4.10] - 2026-07-16

### Fixed — dependency pin

- `stapel-core` requirement was still `>=0.8,<0.9` — three releases behind
  every other stapel-* module (`>=0.10,<0.11`, matching stapel-auth /
  stapel-profiles) and behind the 0.10.1 production fix
  (`users_user.avatar` URLField widening). Bumped to `>=0.10,<0.11`. Full
  suite (136 tests) passes unchanged against core 0.10.1 — no code
  changes were needed.

## [0.4.9] — 2026-07-10

### Fixed
- Re-release of 0.4.8: its publish gate failed on CI missing stapel-tools
  (contract-emission dependency); no code changes beyond the CI fix.

## [0.4.11] — 2026-07-16

### Changed
- **v1 canon sweep §60** (api-versioning.md §2, §6): `urls.py` renamed to
  `urls_v1.py` (paths inside unchanged); the new root `urls.py` mounts it
  under `v1/` and re-exports `GATE_REGISTRY`. Hosts including
  `stapel_billing.urls` under `billing/api/` now serve `/billing/api/v1/...`;
  bare paths no longer exist (sweep lands before the §3 API00x gates are
  enabled).
- Contract artifacts regenerated (`make contract`): `/v1/` in schema paths.
- `_capabilities.py` canonical_prefix → `/billing/api/v1`.
- Lint hygiene to a clean `stapel-verify`: explicit `# noqa` on pre-existing
  findings (R004/R006/R007, CFG001).

## [0.4.8] - 2026-07-09

### Fixed — `subscription.changed.current_period_end` now carries the real period (no more structural `null`)

The `subscription.changed` comm fact advertised `current_period_end`, but no
webhook handler ever populated the model's `current_period_start` /
`current_period_end` from the Stripe subscription object, so the field was
**always emitted as `null`** — a wire value consumers (renewal reminders,
access-expiry gates, proration) could not act on.

- `handle_subscription_updated` and `handle_subscription_deleted` now populate
  the period boundaries from the provider subscription object's unix
  timestamps via the new `_apply_stripe_period` helper, so the emitted
  `current_period_end` is the real renewal/expiry moment.
- A missing period leaves the prior value intact rather than nulling it; the
  field stays `null` **only** at the `checkout.session.completed` milestone
  (whose session payload has no billing period yet — the immediately following
  `customer.subscription.created` event fills it in). `null` now means
  strictly "not yet known", never a fabricated zero timestamp — documented in
  the emit schema (`schemas/emits/subscription.changed.json`) and the
  `_announce_subscription` docstring.

Contract form is unchanged (`current_period_end` was already optional and
nullable) — this is a value-semantics fix, hence a patch bump.

### Added — per-module contract emission: `schema` + `flows` triad (contract-pipeline.md Wave 1)

stapel-billing now emits its **own** API contract per-module, completing the
triad `docs/{schema,flows,errors}.json` (`errors.json` already existed,
unchanged). The frontend codegen can now read billing's committed artifacts
instead of the monolith aggregate at floating `main` — contract-pipeline.md
verdict **A** (contract = a reviewable, version-pinned commit). Copied from the
stapel-auth reference implementation (contract-pipeline.md §7 Wave 1 recipe).

- **Harness** (reuses `stapel_tools.codegen`):
  - `_codegen_settings.py` — single source of truth for the `settings.configure`
    block, shared with `conftest.py` (extracted, no test-behavior change); a
    `contract=True` mode swaps in the production `REST_FRAMEWORK`.
  - `codegen_urls.py` — mounts `stapel_billing.urls` at the canonical
    `billing/api/` prefix (billing has no sibling co-mounted under this prefix
    in the monolith, unlike auth+gdpr).
  - `_codegen.py` — the `python -m stapel_billing._codegen --out docs`
    entrypoint; pins `SCHEMA_PATH_PREFIX="/"` like auth, and additionally
    directly registers the drf-spectacular JWT-cookie auth extension
    (`stapel_core.django.openapi.swagger._register_jwt_auth_extension()`) —
    in the monolith this is a process-global side effect of *another*
    module's `urls.py` calling `get_swagger_urls()`, so a single-module
    harness with no such sibling must trigger it directly or every operation
    is missing its `security`/`JWTCookieAuth` block relative to the monolith
    slice. See MODULE.md for the full explanation.
- **`docs/schema.json`** (new) — drf-spectacular OpenAPI for billing only,
  canonical prefix; **`docs/flows.json`** (new) — `[]` (this module has no
  `@flow_step` annotations yet).
- **Byte-identity** with the monolith aggregate's billing slice (9 paths under
  `/billing/api/` + their 14-component closure) is **exact**: zero diff vs the
  monolith, verified under matching Python 3.12 (drf-spectacular's
  dataclass-derived component `description` text renders `Optional[X]` vs
  `X | None` differently across Python versions — an environment artifact, not
  a harness/mount difference; both sides must run the same interpreter minor
  version to compare byte-for-byte).
- **Gate:** `make contract` / `make contract-check`; `tests/test_contract.py`
  (drift + determinism + canonical-prefix + monolith-slice identity, the last
  skipped when the monolith isn't checked out) is the CI-enforced gate.

## 0.4.7 — 2026-07-06

### Changed — admin-suite AS-5: `@access` category rollout

`StripeWebhookEvent` decorated `@access.ops` (idempotency/delivery log for inbound
Stripe webhooks — read-only journal, mutated only through the webhook claim
protocol, never through the admin) and its `ModelAdmin` swapped to
`stapel_core.django.admin.base.StapelModelAdmin`. `Wallet`, `Transaction`, and
`Subscription` stay undecorated (business tables, implicit `@access.standard`).
No model in this repo carries a secret/token/credential field — Stripe
credentials are process settings, not persisted — so `@access.secret` was not
applicable. Attribute-only change: no migrations.

## 0.4.6 — 2026-07-06

### Added — ru error catalog + bilingual error reference (i18n-shipping волна 2)

Reference-pattern application of the `stapel_core.i18n` catalog contour to the
`errors` domain (i18n-shipping.md §5), copied 1:1 from the stapel-auth pilot.

- `translations/errors.ru.json` — flat `{code: text}` ru catalog covering all
  53 keys, with `translations/.state.json` provenance sidecar. 50 keys seeded
  from the curated `stapel-translate` builtin fixtures (`origin:
  seed:stapel-builtin`, no tokens spent), 3 machine-translated (`origin:
  llm`, unreviewed). `translations/.errors.ru.llm-cache.json` is the
  committed, content-hash translation cache.
- `docs/errors.en.md` · `docs/errors.ru.md` — generated human-readable
  references; README + MODULE.md link both languages.
- `tests/test_error_i18n.py` — `check_translation_catalogs` gate + env-gated
  regen (`STAPEL_REGEN_ERROR_I18N=1`).

## 0.4.5 — 2026-07-06

### Added
- **Declarative error registry + `docs/errors.json` codegen artifact.** All
  twelve service error keys now declare a machine-readable `remediation` hint
  via `register_service_errors(..., remediation=...)` (backend = canon). Where
  the status+name heuristic lies for a billing key, the declaration overrides
  it: the `*_not_found` 404s and `insufficient_credits` (402) declare `fix_input`
  because retrying the same lookup/spend just loops (the heuristic would resolve
  a 404 `not_found` to `retry` and fall a 402 through to `retry`); the webhook
  and config keys — `invalid_stripe_signature`, `invalid_webhook_payload`,
  `redirect_url_not_configured` — declare `contact_support` (machine-to-machine
  or operator misconfiguration, no user field for `fix_input` to point at);
  `forbidden_billing` (403) declares `contact_support` (an authorization
  boundary, where the heuristic's `retry` would loop); `duplicate_webhook_event`
  (409) declares `retry` (a benign idempotency replay the service no-ops, not the
  heuristic's `fix_input`).
- `docs/errors.json` — the language-agnostic error-key registry (53 entries:
  core `COMMON_ERRORS` + cross-cutting keys + the twelve service keys), emitted
  by `generate_error_keys` and consumed by the frontend (`stapel-react` billing
  pair) as the errors-bundle source.
- `tests/test_error_keys.py` — byte-stable drift gate (regenerate-and-diff, same
  discipline as schema.json/flow docs) plus artifact-shape and
  declared-remediation assertions. Regenerate with
  `STAPEL_REGEN_ERROR_KEYS=1 pytest tests/test_error_keys.py`.

### Changed
- Test settings (`conftest.py`) install `stapel_core.django.apps.CommonDjangoConfig`
  so the `generate_error_keys` management command is discoverable for the drift
  gate. No `@flow_step` flows exist in this module (0 flows is valid).


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
