# Changelog

All notable changes to stapel-billing are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.7.0] — 2026-08-14

### Security — BILL-01 / BILL-02 (audit 2026-08-11)

Grants and entitlement answers no longer fail open.

`billing.check_entitlement` denies a key outside the deployment's vocabulary
(`reason="unknown_key"`) instead of answering "no ceiling configured, go ahead";
the vocabulary is the shipped plan ladder plus `STAPEL_BILLING["ENTITLEMENT_KEYS"]`,
and system check `stapel_billing.E101` refuses the boot when a configured plan
mentions a key nobody declared, so a typo in a paywall is found at deploy.
`ALLOW_UNKNOWN_ENTITLEMENT_KEYS` restores the permissive behaviour for hosts that
need time to declare their keys.

Request-supplied `success_url` / `cancel_url` / `return_url` are checked against an
origin allowlist (`redirects.py`, new `REDIRECT_ALLOWED_ORIGINS`, refusal key
`error.400.redirect_url_not_allowed`) — the payment provider renders that link, so an
unchecked one made checkout an authenticated open redirect. The configured fallbacks
and `FRONTEND_URL` are allowlisted implicitly, so a single-frontend deployment needs
no new configuration; `ALLOW_UNVALIDATED_REDIRECT_URLS` (reported by check
`stapel_billing.W102`) is the escape hatch.

`checkout.session.completed` is reconciled against the catalog entry its metadata
names — mode, settled payment status, currency, amount net of coupon, and buyer
(`client_reference_id` vs `metadata.user_id`) — before any credit or plan is granted
(`STRICT_CHECKOUT_RECONCILIATION`). `invoice.paid` refuses an invoice whose status is
not `paid`.

New `ProviderGrant` model (`billing_provider_grant`, unique on
provider/scope/external_id) claims the *business object* a webhook describes;
`StripeWebhookEvent` only ever claimed the *event*. Stripe describes one paid invoice
in several distinct events, so the previous read-then-credit check over ledger
metadata let two concurrent deliveries both pay out. Migration `0002` also makes
`Subscription.stripe_customer_id` / `stripe_subscription_id` unique when set (webhook
routing was ambiguous between duplicates) and backfills claims from existing ledger
rows so a redelivery of an already-granted invoice/session stays a no-op.

### Security — BILL-03: an unknown plan slug no longer means "unlimited"

**Upgrade note — a deployment can be affected by this without changing anything.**
`billing.check_entitlement` used to answer `allowed=True, limit=None` for every key
when the user's plan slug was absent from `STAPEL_BILLING["PLANS"]`: no entitlement
map resolved, and "no ceiling configured" read as "go ahead". Two ordinary
configuration states triggered it — a renamed or retired plan still carried by live
`Subscription` rows (those subscribers went unlimited while their subscription stayed
ACTIVE), and a host ladder without a `"free"` entry, which is what *every* user
without a subscription row resolves to (`Subscription.plan`'s model default).

That answer is now `allowed=False, limit=None, reason="unknown_plan"`, symmetric with
BILL-01's `unknown_key`. New system check `stapel_billing.E102` refuses the boot when
the default plan slug is not in `PLANS`, so the ladder problem is found at deploy
rather than by everyone being denied at runtime.

* **If your `PLANS` does not contain the slug `"free"`, add it** (or migrate the
  model default in your own migration) — otherwise the boot check fails.
* **If live subscriptions carry slugs you have retired**, restore the slug in `PLANS`
  or migrate those rows.
* `STAPEL_BILLING["ALLOW_UNKNOWN_PLAN_SLUGS"] = True` restores the old permissive
  answer (and silences `E102`) while you do it.

### Security — BILL-04: switches from the environment are coerced, `PAYMENT_PROVIDER` is not read from it

**Upgrade note — read this if you set any billing switch as an environment variable.**
`AppSettings` has no per-key type, so an environment variable arrives as raw text and
every switch was consumed as a bare truth value. `bool("false")` is `True`: setting
`ALLOW_UNKNOWN_ENTITLEMENT_KEYS=false` or `ALLOW_UNVALIDATED_REDIRECT_URLS=false`
*enabled* the hatch it was meant to disable. The mirror image hit the gate that
defaults to on — an empty `STRICT_CHECKOUT_RECONCILIATION=` disabled checkout
reconciliation entirely, because `bool("")` is `False`.

* `ALLOW_UNVALIDATED_REDIRECT_URLS`, `ALLOW_UNKNOWN_ENTITLEMENT_KEYS` and
  `ALLOW_UNKNOWN_PLAN_SLUGS` are now on only for `1` / `true` / `yes` / `on`
  (case- and space-insensitive). **If you relied on any other non-empty string to
  keep a hatch open, it is now closed** — spell it `true`.
* `STRICT_CHECKOUT_RECONCILIATION` is off only for an explicit `0` / `false` / `no` /
  `off`; anything else, including an empty value, keeps the reconciliation on.
* `PAYMENT_PROVIDER` is now `no_env`: it selects the class that handles money, and a
  same-named environment variable in a shared pod or compose file must not swap it.
  **If you configured the provider through a bare `PAYMENT_PROVIDER` environment
  variable, move it into `settings.STAPEL_BILLING["PAYMENT_PROVIDER"]`** (or a flat
  Django setting of the same name) — the environment is ignored for this key.

### Security — BILL-05: the entitlement hatch reports itself

`ALLOW_UNKNOWN_ENTITLEMENT_KEYS` silenced the `E101` typo hunt and then said nothing
at all, so a deployment that opened the paywall hatch "for a week" got no signal from
`manage.py check`. It now emits `stapel_billing.W101` on every boot, symmetric with
the `W102` the redirect hatch has always emitted. No behaviour change beyond the
warning.

### Security — BILL-06: an unconfigured payment provider refuses instead of fabricating

**Upgrade note — this changes what a deployment without Stripe credentials does.**
With an empty `STRIPE_SECRET_KEY` (or no `stripe` SDK) the provider answered every call
with a dev placeholder and told nobody: `POST /billing/api/checkout` returned **200**
with a fabricated `cs_dev_*` session id, `GET /billing/api/portal` returned a link to
nowhere, and `POST /billing/api/subscription/cancel` returned quietly — so the view
stamped `cancelled_at` and reported a cancellation that no payment system had heard of.

* An unconfigured provider now raises `ProviderNotConfiguredError`; the checkout and
  portal endpoints answer **502**, and the cancel endpoint answers 502 *without*
  marking the subscription cancelled locally.
* New system check `stapel_billing.E104` fails the boot when the effective
  `PAYMENT_PROVIDER` reports it has no credentials; `stapel_billing.E103` covers a
  `PAYMENT_PROVIDER` that does not resolve to a `PaymentProvider` subclass.
* **A dev/staging environment that relied on the placeholders must set
  `STAPEL_BILLING["ALLOW_UNCONFIGURED_PAYMENT_PROVIDER"] = True`** — the hatch keeps
  the old behaviour, logs a warning on every use and is reported by check
  `stapel_billing.W104`.
* `PaymentProvider` gained `is_configured()` (default `True`, so third-party providers
  are unaffected); `StripeProvider` overrides it. `cancel_subscription` is now
  documented as *must raise* when unconfigured, instead of *must be a no-op*.

### Security — BILL-07: money endpoints deny anonymous (guest) sessions

**Upgrade note for deployments running with `AUTH_ANONYMOUS`.** A guest session is
`is_authenticated`, so the bare `IsAuthenticated` on every billing view admitted it.
`GET /billing/api/wallet` minted a `Wallet` row per throwaway session and
`POST /billing/api/checkout` opened a real Stripe Checkout whose
`client_reference_id` names an identity that can be discarded and re-minted with one
unauthenticated request — a payment nobody can be credited for.

The wallet, transactions, checkout, portal, subscription and subscription-cancel
views now refuse an anonymous session with **403** (`GuestDeniedMixin`, declared as
`stapel_anonymous_access = ANONYMOUS_DENIED`). Ordinary users are unaffected.
`GET /billing/api/products` (the public price list) and the Stripe webhook stay open
and now say so explicitly (`ANONYMOUS_ALLOWED`). **If a guest-facing flow in your
frontend called any money endpoint, it must convert the session to a real account
first** — there is no opt-out setting: a guest wallet has no owner to bill.

## [0.6.2] — 2026-08-10

### Fixed — this module translates only the keys it owns

`translations/errors.{ru,es}.json` each carried 41 verbatim copies of the
cross-cutting keys stapel-core owns. None was an intentional reword: before
stapel-core 0.22.0 the coverage gate took its canon from the whole in-process
registry, so going green *required* copying them. Core ships those catalogs
itself now and the loader merges them, so the copies were a second, drifting
shadow of texts this module does not answer for — and `test_catalog_gate_green`
went red on them, which is what blocked every tag in this repository.

ru and es go 53 → 12 keys: the ones this module actually owns. Two
machine-translation entries in the test module (network/verification) were
core-owned and went with them; coverage is now scoped through `owned_keys` /
`owner_of_dir`.

The reference does not move: `docs/errors.{en,ru,es}.md` regenerated after the
deletion are **byte-identical** to the ones regenerated before it, because
stapel-core 0.23.1 resolves a key this module does not own from its owner's
catalog (`module_catalog`). Without that fix the prune would have downgraded 41
Russian and Spanish rows to `_(en)_` English fallbacks with nothing saying so;
`test_error_reference_matches_a_fresh_regeneration` now keeps the committed
reference and a fresh regeneration byte-equal.

The `stapel-core` pin moves to `>=0.23.1`: with an older core these pruned
catalogs resolve to English at runtime.

## [0.6.1] — 2026-08-09

### Added — Spanish ships as a language of the library, not as a host override

`translations/errors.es.json` (53 keys) + `docs/errors.es.md`, generated by the
same contour that produces Russian — no hand-written JSON, no product-side
override file. 50 values are lifted verbatim from the curated
`stapel-translate` builtin corpus (`origin: seed:stapel-builtin`), which is what
"clients don't spend tokens" means in practice: the corpus was paid for once.
The remaining 3 are machine translations of this module's own keys, recorded
`origin: llm` — **unreviewed**, and counted as such by the gate now that
stapel-core 0.20.1 stopped treating a curated corpus as human sign-off. Nobody
has read these; `translate_catalogs --approve` is the state transition that
changes that, and it has not been run.

Register and terminology follow the corpus rather than being invented per
module: informal *tú* address, *espacio de trabajo*, *llave de acceso*, *nombre
para mostrar*.

The harness in `tests/test_error_i18n.py` is now language-generic — a language
is a tag in `LANGUAGES` plus whatever the corpus does not carry, and the
catalog, the provenance sidecar, the reference page and the gate all follow.
Adding the next language is not a second copy of this work.

### Fixed — the translation catalogs were built into the wheel and then left out of it

`translations/errors.ru.json` has been in this repository since the i18n wave,
and it has never reached anyone who installed the package. `[tool.setuptools.package-data]`
listed `schemas/`, `migrations/`, `docs/` — and not `translations/`, so setuptools
dropped the directory on the way into the wheel. Verified by installing the built
wheel into an empty virtualenv: no `translations/` under the installed package,
and `load_app_catalogs()` therefore found nothing to merge. Every host running
this module in Russian was silently reading the English canon.

`translations/*.json` is now declared, and the check is the install, not the
manifest: the wheel is built, installed into a clean virtualenv, and the
catalogs are listed on disk.

## [0.5.2] — 2026-08-02

### Packaging / contract

- `docs/llms.txt` — the fifth contract artifact — is now emitted, drift-gated
  by `make contract`/`contract-check`, and badged in the README. `docs/capabilities.json`
  has no `surface` entries yet, so the generated llms.txt's Usage surface
  section is empty (pre-existing gap, not introduced here).
- `docs/llms.txt` is now listed in `package-data` so it ships in the wheel.
- `package-data` also now carries `docs/capabilities.json`, `docs/flows.json`,
  `docs/errors.json` and `CONFIG.MD`.
- Badge canon + Python 3.14 classifier added.

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
