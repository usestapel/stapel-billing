# CONFIG.MD — stapel-billing

Config registry for **stapel-billing** (`static-scaffold-and-config.md` §2). One
row per configuration key the module reads, its **source** (`env` = the process
environment / the `STAPEL_BILLING` settings dict; `vault` = a credential that
belongs in the `stapel_core.secrets` provider seam), what it is for, whether it
is required, and its default.

Every key is read through `billing_settings` (`stapel_billing.conf.AppSettings`,
namespace `STAPEL_BILLING`). Resolution order per key: `settings.STAPEL_BILLING`
dict → a flat Django setting of the same name → environment variable → the
default below. `PAYMENT_PROVIDER` is the one CTO-facing config axis
(capability-config.md §16) and is `no_env`: it names the class that handles
money, so a same-named variable in a shared pod cannot swap it. `CREDIT_PACKAGES`
/ `PLANS` are catalog-override extension points; the `STRIPE_*` credentials
belong in the secrets seam.

Two rules the boolean keys add to that order: an environment value is text, not
truth (`bool("false")` is `True`), so the default-off `ALLOW_*` switches are on
only for `1` / `true` / `yes` / `on`, and the default-on
`STRICT_CHECKOUT_RECONCILIATION` gate is off only for `0` / `false` / `no` /
`off`. Anything else leaves the safe value in place.

## stapel-billing

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| PAYMENT_PROVIDER | env | axis: dotted path to a `PaymentProvider` — which processor handles checkout, subscriptions and webhooks. Never read from the environment. | no | stapel_billing.providers.stripe.StripeProvider |
| STRIPE_SECRET_KEY | vault | Stripe API key for the default provider, read lazily per call. Empty = the provider is unconfigured: checkout/portal/cancel refuse and boot check `E104` fails. | no | (empty) |
| STRIPE_WEBHOOK_SECRET | vault | Stripe webhook signature secret, read lazily per call. Without it no inbound webhook verifies. | no | (empty) |
| CREDIT_PACKAGES | env | extension point: the one-off credit package catalog (list of dicts or `CreditPackage` instances). Stripe builds `price_data` inline per checkout, so there are no price ids to keep in sync. | no | catalog.DEFAULT_CREDIT_PACKAGES (starter/standard/bulk) |
| PLANS | env | extension point: the subscription plan catalog (list of dicts or `PlanCatalogEntry` instances), each entry carrying the `entitlements` map `billing.check_entitlement` answers from. | no | catalog.DEFAULT_PLANS (free/pro/team/enterprise) |
| CHECKOUT_SUCCESS_URL | env | fallback checkout success redirect when the request carries none. Empty → derived from the flat `FRONTEND_URL`; with neither, the request fails with `error.400.redirect_url_not_configured`. | no | (empty) |
| CHECKOUT_CANCEL_URL | env | same resolution for the checkout cancel redirect. | no | (empty) |
| PORTAL_RETURN_URL | env | same resolution for the customer-portal return URL. | no | (empty) |
| REDIRECT_ALLOWED_ORIGINS | env | exact origins (`"https://app.example.com"`) a REQUEST-supplied redirect may point at, on top of the three fallbacks' origins and `FRONTEND_URL`. Anything else is refused — the processor renders the link, so an unchecked one is a phishing hop. | no | [] |
| ALLOW_UNVALIDATED_REDIRECT_URLS | env | escape hatch: forward request-supplied redirect URLs unchecked. That is an authenticated open redirect; check `W102` says so on every boot. | no | false |
| ENTITLEMENT_KEYS | env | feature keys this deployment recognises, on top of the shipped ladder's. A key outside the vocabulary is denied at runtime (`unknown_key`) and fails boot check `E101` when a configured plan mentions it — a typo must not hand out a paid feature. | no | [] |
| ALLOW_UNKNOWN_ENTITLEMENT_KEYS | env | escape hatch: allow an unrecognised entitlement key again (silences `E101`, reported by `W101`). | no | false |
| ALLOW_UNKNOWN_PLAN_SLUGS | env | escape hatch: treat a plan slug missing from `PLANS` as "no ceilings" again instead of denying (silences `E102`). | no | false |
| ALLOW_UNCONFIGURED_PAYMENT_PROVIDER | env | escape hatch for a developer's machine: let an unconfigured provider answer with dev placeholders instead of refusing (silences `E104`, reported by `W104`). | no | false |
| STRICT_CHECKOUT_RECONCILIATION | env | reconcile the processor's checkout object (mode, payment status, currency, amount vs. catalog price, buyer) before granting. Off takes the session's metadata on faith. | no | true |
| HOLD_DEFAULT_TTL_SECONDS | env | how long a `services.hold()` reservation stays valid when the caller sets no deadline — the bound on how long a crashed pipeline can keep a customer's credits reserved. `expire_holds` releases holds past it. 0 (or None) = never sweep, which is a decision, not a default. | no | 3600 |

## Scheduling (not a settings key, but required configuration)

`stapel_billing.tasks.get_billing_beat_schedule()` must be spread into the host's
`CELERY_BEAT_SCHEDULE` (or its three plain callables run from cron). Credits are
granted with an expiry that only becomes real because `expire_credit_lots` runs;
abandoned reservations only come back because `expire_holds` runs. System check
`stapel_billing.W105` reports a beat-driven host that schedules neither.
