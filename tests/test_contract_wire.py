"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns, and
the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim. stapel-alerts 0.2.0
shipped ``GET /issues`` declared as ``Issue[]`` while the wire carried
``{count, offset, limit, results}``: the drift gate was green and the
frontend pair rendered ``undefined``.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response body, and validates the body
it gets against the schema it was promised.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers three of four rows is the family
  of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of substitution,
  naming the operation;
* an operation that genuinely cannot run in-process is listed by name in
  ``UNDRIVABLE`` with a one-line reason, asserted exactly current;
* a collection that comes back empty fails in the populated pass — an empty
  array validates against any item schema, so an empty answer is a check that
  looked at nothing;
* every operation is driven a SECOND time in its emptiest legal state
  (``EMPTY_STATE``): a wallet with lots, holds, debts and a deadline and a
  wallet that has never been funded; a paid subscription and an account with
  none. Every null finding in the first wave of this gate was on the empty
  state.

Runs on every interpreter: it reads the committed schema and never emits.

THE MOUNT — and here it is the finding, not the preamble. ``codegen_urls.py``
mounts ``billing/api/`` + ``stapel_billing.urls``, and that module contributes
the mandatory ``v1/`` sub-prefix, so the committed document describes
``/billing/api/v1/…``. ``stapel_billing/tests/urls.py`` mounts
``stapel_billing.urls_v1`` DIRECTLY under ``billing/api/``, skipping the
version segment — so the whole existing suite drives ``/billing/api/wallet``,
``/billing/api/checkout``, ``/billing/api/webhooks/stripe``, and **not one
path in the committed contract resolves under it**. Nothing in this
repository had ever driven the document it ships. That is the sixth library
in this wave with that shape, and
``test_every_declared_path_resolves_under_this_urlconf`` is what catches it:
this module declares the EMISSION mount and asserts every declared path
resolves under it. The library's own urlconf is left exactly as it is.

WHAT IT FOUND once the requests went where the document points: 10 of 10
operations driven, 10 of them a second time in their emptiest state, 0 red.
Every declared body held — including all six ``nullable`` claims of
``SubscriptionResponse`` on an account that never paid, the nullable
``auto_recharge_package`` and ``expiring_soon`` of an unfunded wallet, the
nullable ``amount_cents`` and ``description`` of a credit-ledger row, and the
nullable ``next_cursor`` of a transaction page — and
``test_the_gate_is_not_blind`` proves that is a finding rather than a gate
that never looked: it re-validates every driven body against
``{"type": "string"}`` and requires all of them to fail.

One thing this pass deliberately does not call a lie:
``POST /webhooks/stripe`` declares its 200 as ``OpenApiTypes.OBJECT`` — a
bare ``{"type": "object"}`` that any object satisfies. That is a weak claim,
not a false one, and the endpoint is not a first-party client surface (Stripe
is the caller, and the signature is the credential). It is driven in both
states anyway, so the day somebody types it the gate is already asking.
"""
import copy
import json
import re
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import jsonschema
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import include, path as url_path
from django.utils import timezone
from rest_framework.test import APIClient
from stapel_billing.providers.base import PaymentProvider

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at, reproduced for the test client
#: (``codegen_urls.py``: ``billing/api/`` + the module's own ``v1/``). The
#: suite's own ``tests/urls.py`` mounts ``urls_v1`` directly under
#: ``billing/api/``, under which NONE of these paths resolve — see the module
#: docstring.
urlpatterns = [
    url_path("billing/api/", include("stapel_billing.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/billing/api/v1"


class WireProvider(PaymentProvider):
    """The payment provider seam, filled the way a deployment fills it.

    ``PAYMENT_PROVIDER`` is a dotted path (providers/base.py); swapping it is
    how this module's own suites drive checkout, the portal, the cancel and
    the webhook, and it is the honest boundary — Stripe is not this
    repository's code, and an unconfigured provider REFUSES rather than
    fabricating a session (BILL-06), so there is no third option that both
    runs in-process and answers a 200.

    Everything on this side of the seam — the DTO, the serializer, the
    redirect allowlist, the idempotency claim, the ledger — runs for real.
    """

    name = "wire-contract"

    def create_checkout_session(self, *, user, package, plan, success_url, cancel_url):
        return ("https://provider.example/checkout/cs_wire_1", "cs_wire_1")

    def create_portal_session(self, *, customer_id, return_url):
        return "https://provider.example/portal/wire"

    def cancel_subscription(self, subscription_id):
        return None

    def verify_webhook(self, payload, signature):
        # The header selects the outcome, exactly as this module's own
        # webhook suite does it: a real HMAC would only be testing Stripe's
        # SDK, and the view's contract starts after verification returns.
        if signature != "good":
            raise ValueError("invalid signature")
        return json.loads(payload)


PROVIDER_PATH = f"{WireProvider.__module__}.{WireProvider.__qualname__}"


@contextmanager
def billing_conf(**overrides):
    """``STAPEL_BILLING`` with the provider wired, plus per-recipe overrides.

    ``billing_settings`` is an ``AppSettings`` that caches what it reads, so
    the cache is dropped on the way in AND on the way out — otherwise the
    next test inherits this one's catalogue.
    """
    from stapel_billing.conf import billing_settings

    conf = {"PAYMENT_PROVIDER": PROVIDER_PATH}
    conf.update(overrides)
    with override_settings(STAPEL_BILLING=conf):
        billing_settings.reload()
        try:
            yield
        finally:
            billing_settings.reload()


@pytest.fixture(autouse=True)
def _deployment(tmp_path):
    """Pin ``MEDIA_ROOT``, and wire the one seam every money path needs.

    ``MEDIA_ROOT`` is unset in the harness settings, so it defaults to the
    working directory. Nothing here writes a file today — billing stores no
    media — but that is exactly how an export in stapel-auth ended up written
    into the checkout, where a stray directory then shadowed a real module.
    """
    with override_settings(MEDIA_ROOT=str(tmp_path)), billing_conf():
        yield


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _undiscriminated_union(node):
    """A ``oneOf`` whose branches OVERLAP by construction.

    Nothing this gate validates goes through one today; the conversion stays
    because a response union would be read the same way — without a
    ``discriminator`` the branches are alternatives, and an exclusive
    ``oneOf`` would reject what the document plainly describes.
    """
    branches = node.get("oneOf")
    if not isinstance(branches, list) or "discriminator" in node:
        return False
    return len(branches) > 1


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergences that matter here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type`` (or
    beside an ``allOf`` wrapping a ``$ref``, which is how ``expiring_soon`` is
    emitted); JSON Schema has no such keyword and would refuse the null —
    which is exactly the value most of these fields answer in their empty
    state. Everything else drf-spectacular emits here (``$ref``, ``allOf``,
    ``format``, ``required``, ``additionalProperties``) is JSON Schema as
    written.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if _undiscriminated_union(rebuilt):
        rebuilt["anyOf"] = rebuilt.pop("oneOf")
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0], o[2]))


OPERATIONS = _operations()


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def anonymous():
    return APIClient()


def make_user(**kwargs):
    User = get_user_model()
    defaults = dict(
        username=_unique("wire-"),
        email=f"{_unique('wire-')}@example.com",
        password="wire-contract-password-7",
    )
    defaults.update(kwargs)
    return User.objects.create_user(**defaults)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def staff_client():
    """``IsServiceRequest | IsStaffUser`` — the human half of the internal door.

    The key path needs a non-empty ``SERVICE_API_KEY`` in the harness (it is
    ``""``, so ``ServiceAPIKeyMiddleware`` never marks a request), and the
    permission is an OR: the same view code answers either way.
    """
    return client_for(make_user(is_staff=True))


def fund(user, credits=100, *, expires_at=None, source=None):
    """Real credits through the real ledger — a lot, and a transaction."""
    from stapel_billing.models import LotSource, TransactionType
    from stapel_billing import services

    return services.credit(
        user=user,
        credits=credits,
        type=TransactionType.CREDIT_PURCHASE,
        source=source or LotSource.PURCHASE,
        expires_at=expires_at,
    )


def reserve(user, credits=15):
    from stapel_billing.models import TransactionType
    from stapel_billing import services

    return services.hold(
        user=user,
        credits=credits,
        type=TransactionType.AI_CHARGE,
        idempotency_key=_unique("wire-hold-"),
    )


def owe(user, credits=20):
    """Serve work the wallet could not cover — the branch that opens a debt."""
    from stapel_billing.models import TransactionType
    from stapel_billing import services

    return services.debit(
        user=user,
        credits=credits,
        type=TransactionType.AI_CHARGE,
        description="work served without cover",
        allow_partial=True,
    )


def rich_wallet():
    """A wallet carrying everything the contract can describe at once: two
    lots (one of them expiring, so ``expiring_soon`` is non-null) and an open
    hold against them.

    A debt is deliberately NOT here, and cannot be: credits into a wallet pay
    its debts down before they become spendable (services.py ``_settle_debts``),
    so live lots and an outstanding debt are mutually exclusive by design. The
    debt state is driven separately, in ``indebted_wallet``.
    """
    user = make_user()
    fund(user, 100)
    fund(user, 30, expires_at=timezone.now() + timedelta(days=7))
    reserve(user, 15)
    return user


def indebted_wallet():
    """Work served that the wallet could not cover: a partial debit on an
    empty wallet, which opens a debt and consumes nothing.

    The only state in which ``debts`` is non-empty and ``debt_outstanding``
    is above zero — and, because the settlement rule above makes it exclusive
    with live lots, also the state with no ``lots``, no ``holds`` and a null
    ``expiring_soon``.
    """
    user = make_user()
    owe(user, 400)
    return user


def paid_subscription(user=None, **kwargs):
    """A row with a provider object behind it — what ``is_paid`` means."""
    from stapel_billing.models import Plan, Subscription, SubscriptionStatus

    user = user or make_user()
    defaults = dict(
        plan=Plan.PRO if hasattr(Plan, "PRO") else "pro",
        status=SubscriptionStatus.ACTIVE,
        stripe_subscription_id=_unique("sub_wire_"),
        stripe_customer_id=_unique("cus_wire_"),
        current_period_start=timezone.now() - timedelta(days=3),
        current_period_end=timezone.now() + timedelta(days=27),
    )
    defaults.update(kwargs)
    sub, _created = Subscription.objects.update_or_create(user=user, defaults=defaults)
    return user, sub


def webhook(client, event, signature="good"):
    return client.post(
        V1 + "/webhooks/stripe",
        data=json.dumps(event),
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=signature,
    )


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path):
        self.method = method
        self.path = path

    def __call__(self, client, params=None, data=None, query="", **extra):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        send = getattr(client, self.method.lower())
        if self.method == "GET":
            return send(url + query, **extra)
        return send(url + query, data if data is not None else {}, format="json", **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template, status code)``.
RECIPES = {}

#: The same operations again, in the emptiest state the contract still has to
#: describe. Every null finding in the first wave of this gate was there.
EMPTY_STATE = {}


def recipe(method, path, code=None, table=None):
    def register(fn):
        target = RECIPES if table is None else table
        key = (method, V1 + path, code)
        assert key not in target, f"duplicate recipe for {method} {path} {code}"
        target[key] = fn
        return fn

    return register


def empty_state(method, path, code=None):
    return recipe(method, path, code, table=EMPTY_STATE)


#: Operations that cannot be driven in-process, by name and with the reason.
#: A short, visible list is acceptable here; a silent skip is not.
#:
#: EMPTY. Every operation this module declares is reachable from a test
#: client. The one external dependency — the payment provider — is a
#: documented dotted-path SEAM a deployment fills, so the gate fills it too
#: and everything on this side of it runs for real.
UNDRIVABLE: dict = {}


# ── the wallet ───────────────────────────────────────────────────────────────


@recipe("GET", "/wallet")
def _wallet(call):
    return call(client_for(rich_wallet()))


@empty_state("GET", "/wallet")
def _wallet_empty(call):
    """An account that has never bought anything: empty ``lots``, ``holds``
    and ``debts``, a null ``expiring_soon`` (nothing has a deadline) and the
    null ``auto_recharge_package`` the declaration marks REQUIRED."""
    return call(client_for(make_user()))


@recipe("PATCH", "/wallet")
def _wallet_patch(call):
    return call(
        client_for(rich_wallet()),
        data={
            "auto_recharge_enabled": True,
            "auto_recharge_threshold": 50,
            "auto_recharge_package": "starter",
            "low_balance_alert": 25,
        },
    )


@empty_state("PATCH", "/wallet")
def _wallet_patch_indebted(call):
    """A no-op patch on a wallet that owes: no lots, no holds, a null
    ``expiring_soon`` and the null ``auto_recharge_package`` — with the one
    non-empty ``debts`` array this contract can produce beside them."""
    return call(client_for(indebted_wallet()), data={})


@recipe("GET", "/wallet/transactions")
def _transactions(call):
    user = rich_wallet()
    return call(client_for(user))


@empty_state("GET", "/wallet/transactions")
def _transactions_empty(call):
    """A ledger that has never been written to — ``transactions`` is []."""
    return call(client_for(make_user()))


# ── the catalogue ────────────────────────────────────────────────────────────


@recipe("GET", "/products")
def _products(call):
    """The public price list — drawn before anybody logs in."""
    return call(anonymous())


@empty_state("GET", "/products")
def _products_empty(call):
    """A deployment that sells nothing yet. The catalogue is configuration
    (``STAPEL_BILLING["CREDIT_PACKAGES"]`` / ``["PLANS"]``), so "empty" is a
    real state a host can be in on day one, and the pricing page has to
    render against it rather than crash."""
    with billing_conf(CREDIT_PACKAGES=[], PLANS=[]):
        return call(anonymous())


# ── checkout and the portal ──────────────────────────────────────────────────


@recipe("POST", "/checkout")
def _checkout(call):
    return call(
        client_for(make_user()),
        data={
            "package": "starter",
            "success_url": "https://front.example/ok",
            "cancel_url": "https://front.example/no",
        },
    )


@empty_state("POST", "/checkout")
def _checkout_bare(call):
    """No redirect targets supplied: the endpoint falls back to
    ``FRONTEND_URL`` through the redirect allowlist rather than answering a
    null URL, which is the claim ``CheckoutResponse`` makes."""
    return call(client_for(make_user()), data={"package": "starter"})


@recipe("POST", "/checkout/simulate")
def _simulate(call):
    """Staff buy a package with no card. The gate is WHO, so the user is staff."""
    return call(client_for(make_user(is_staff=True)), data={"package": "starter"})


@empty_state("POST", "/checkout/simulate")
def _simulate_empty(call):
    """An empty wallet is the state this exists for: a staff account that has
    never bought anything, going from zero to the package's credits through
    the real post-payment path."""
    return call(client_for(make_user(is_staff=True)), data={"package": "starter"})


@recipe("GET", "/portal")
def _portal(call):
    user, _sub = paid_subscription()
    return call(client_for(user), query="?return_url=https://front.example/billing")


@empty_state("GET", "/portal")
def _portal_no_customer(call):
    """An account with no subscription row at all, so ``customer_id`` is the
    empty string — the state a portal link is asked for in most often, and
    the one where a null ``portal_url`` would show up if it ever could."""
    return call(client_for(make_user()))


# ── the subscription ─────────────────────────────────────────────────────────


@recipe("GET", "/subscription")
def _subscription(call):
    user, _sub = paid_subscription()
    return call(client_for(user))


@empty_state("GET", "/subscription")
def _subscription_free(call):
    """The 111-account case this DTO was rewritten for: a free row created by
    this very read, answering a null ``stripe_subscription_id``, a null
    ``current_period_start``/``end`` and a null ``cancelled_at`` — four
    REQUIRED nullable claims at once — with ``is_paid`` false beside them."""
    return call(client_for(make_user()))


@recipe("POST", "/subscription/cancel")
def _cancel(call):
    user, _sub = paid_subscription()
    return call(client_for(user))


@empty_state("POST", "/subscription/cancel")
def _cancel_no_period(call):
    """A paid subscription whose period the provider never sent — the exact
    production shape this module was fixed for (21 webhooks processed, every
    row's period NULL). ``is_paid`` is true, so the cancel is legal, and the
    answer still carries two nulls."""
    user, _sub = paid_subscription(
        current_period_start=None, current_period_end=None
    )
    return call(client_for(user))


# ── service-to-service ───────────────────────────────────────────────────────


@recipe("POST", "/internal/debit")
def _debit(call):
    user = make_user()
    fund(user, 100)
    return call(
        staff_client(),
        data={
            "user_id": str(user.id),
            "credits": 10,
            "type": "ai_charge",
            "description": "one minute of transcription",
            "metadata": {"recording_id": str(uuid.uuid4())},
            "idempotency_key": _unique("wire-debit-"),
        },
    )


@empty_state("POST", "/internal/debit")
def _debit_bare(call):
    """The smallest legal debit: no description, no metadata, no idempotency
    key — and a balance that lands on zero."""
    user = make_user()
    fund(user, 10)
    return call(
        staff_client(),
        data={"user_id": str(user.id), "credits": 10, "type": "ai_charge"},
    )


# ── the provider's callback ──────────────────────────────────────────────────


@recipe("POST", "/webhooks/stripe")
def _webhook(call):
    """A real handled event, through verification, the idempotency claim, the
    row lock and the handler — ``customer.subscription.updated`` on a row
    this deployment already has."""
    _user, sub = paid_subscription()
    return webhook(
        anonymous(),
        {
            "id": _unique("evt_"),
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": sub.stripe_subscription_id,
                    "object": "subscription",
                    "customer": sub.stripe_customer_id,
                    "status": "active",
                    "items": {"data": [{"current_period_end": 4102444800}]},
                }
            },
        },
    )


@empty_state("POST", "/webhooks/stripe")
def _webhook_duplicate(call):
    """Stripe's retry of an event already processed: the ``duplicate`` branch,
    which is the shortest body this endpoint ever sends."""
    event = {"id": _unique("evt_"), "type": "some.unhandled.event"}
    first = webhook(anonymous(), event)
    assert first.status_code == 200, first.content
    return webhook(anonymous(), event)


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


#: Operations whose declared body the wire does not send.
#:
#: EMPTY, and that is the finding rather than the absence of one: once the
#: requests went where the document points — which, before this file, nothing
#: in this repository did — 10 of 10 operations answered the body they
#: promise, in both states. The mechanism stays because the next change will
#: need it: an entry must name the defect AND its owner, and ``strict=True``
#: turns a fixed one into a failure until the entry is deleted, so a finding
#: can be neither forgotten nor quietly kept.
KNOWN_MISMATCHES: dict = {}


def _recipe_for(table, method, path, code):
    """The code-specific recipe if there is one, else the operation's."""
    return table.get((method, path, code)) or table.get((method, path, None))


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Five of the first eight libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe: one mounted a different
    prefix AND one segment short, one mounted the paths bare, one mounted less
    than the emission did, one doubled a segment to reproduce a host's
    deployed prefix. THIS module is the sixth: ``tests/urls.py`` mounts
    ``stapel_billing.urls_v1`` directly, skipping the ``v1/`` segment that
    ``stapel_billing.urls`` contributes and that every path in the committed
    document carries. The whole suite drives ``/billing/api/wallet``; the
    contract describes ``/billing/api/v1/wallet``.

    That is the same family as a gate nobody asks: the recipes can all be
    written, the run can be green, and not one request went where the contract
    says it goes. A missing recipe already fails loudly; this fails when the
    MOUNT is wrong, which no per-operation check can see, because when the
    mount is wrong every operation is equally and silently unreachable.

    Asserted against the urlconf this module declares, so it fails at the one
    moment it is cheap to fix: when somebody changes a mount.
    """
    from django.urls import Resolver404, resolve

    # Resolution cares about the SHAPE of a segment. A path counts as
    # reachable if any one shape resolves: the question here is whether the
    # mount exists, not whether a particular id does.
    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_the_suite_urlconf_resolves_the_committed_contract_too():
    """The mount defect, now pinned closed.

    This test used to assert the OPPOSITE — that under the suite's own
    ROOT_URLCONF not a single path of the committed contract resolved, which
    was true when this file was written: ``tests/urls.py`` mounted
    ``urls_v1`` directly under ``billing/api/``, skipping the ``v1/`` segment
    every declared path carries, so every existing test in this repository
    drove an endpoint the document does not describe.

    It was written as an assertion rather than a comment so that the day the
    mount was fixed it would fail and force this docstring to be corrected
    instead of quietly becoming untrue. That is what happened. The suite
    urlconf now carries the contract mount alongside the one the existing
    tests address, and this asserts the property that replaced the defect.
    """
    from django.urls import Resolver404, resolve

    with override_settings(ROOT_URLCONF="stapel_billing.tests.urls"):
        unresolved = []
        for _method, path, _code, _schema in OPERATIONS:
            try:
                resolve(re.sub(r"\{[^}]+\}", "1", path))
            except Resolver404:
                unresolved.append(path)

    assert not unresolved, (
        "a contract nothing drives is a contract nothing checks: these "
        "declared paths do not resolve under the suite's own urlconf:\n  "
        + "\n  ".join(sorted(set(unresolved)))
    )

def test_every_declared_operation_is_driven_or_named_undrivable():
    """No operation is covered by silence, and no entry outlives its operation."""
    missing = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if _recipe_for(RECIPES, method, path, code) is None
        and (method, path) not in UNDRIVABLE
    ]
    assert not missing, (
        "operations with a declared JSON response body and no recipe:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in missing)
    )

    declared_codes = {(m, p, c) for m, p, c, _ in OPERATIONS}
    declared_ops = {(m, p) for m, p, _c, _ in OPERATIONS}
    stale = sorted(
        key
        for key in RECIPES
        if (key[0], key[1]) not in declared_ops
        or (key[2] is not None and key not in declared_codes)
    )
    assert not stale, (
        "recipes for operations/status codes the contract no longer declares:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in stale)
    )
    stale_exclusions = sorted(set(UNDRIVABLE) - declared_ops)
    assert not stale_exclusions, (
        f"exclusions for operations the contract no longer declares: {stale_exclusions}"
    )
    both = sorted((m, p) for m, p, _c in RECIPES if (m, p) in UNDRIVABLE)
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"

    # RECIPES ∪ UNDRIVABLE is EXACTLY the declared set, in both directions.
    covered = {(m, p) for m, p, _c in RECIPES} | set(UNDRIVABLE)
    assert covered == declared_ops, (
        "the covered set and the declared set differ:\n"
        f"  declared and not covered: {sorted(declared_ops - covered)}\n"
        f"  covered and not declared: {sorted(covered - declared_ops)}"
    )


def test_every_read_is_also_driven_in_its_emptiest_state():
    """A populated answer cannot say what a field holds when there is nothing.

    Every null finding in the first wave of this gate was on the empty state.
    A gate that only ever seeds three rows and asks never sees any of them.

    Every operation this module declares is required to have an
    ``EMPTY_STATE`` recipe — including the writes, because every response
    shape here carries at least one nullable field. The exemption list is
    empty, and that is the point.
    """
    exempt: set = set()
    operations = {(m, p) for m, p, _c, _ in OPERATIONS}
    covered = {(m, p) for m, p, _c in EMPTY_STATE}
    missing = sorted(operations - covered - exempt)
    assert not missing, (
        "operations driven only against a populated database — the state where "
        "every null claim in this gate's history was found is unchecked:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    stale = sorted(covered - operations)
    assert not stale, f"empty-state recipes for undeclared operations: {stale}"


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live operation and carry its reason."""
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    for key, reason in KNOWN_MISMATCHES.items():
        assert key in declared, (
            f"{key} is recorded as a known mismatch but the contract no longer "
            "declares it — delete the entry"
        )
        assert reason and reason.strip(), f"{key} is recorded with no reason"
        assert "OWNER:" in reason, (
            f"{key} names a defect but not who owns it — an unowned finding "
            "is a finding nobody fixes"
        )


def _drive(table, method, path, code, body_schema, *, expect_rows):
    perform = _recipe_for(table, method, path, code)
    assert perform is not None, (
        f"{method} {path} declares a response body and has no recipe — an "
        "unchecked operation is a schema nobody proves. Teach RECIPES, or "
        "name it in UNDRIVABLE with a reason."
    )

    response = perform(Call(method, path))
    assert response.status_code == code, (
        f"{method} {path}: expected the declared {code}, got "
        f"{response.status_code}: {response.content[:400]}"
    )

    body = response.json()
    errors = sorted(_validator(body_schema).iter_errors(body), key=lambda e: list(e.path))
    assert not errors, (
        f"{method} {path} answers a body the contract does not describe:\n"
        + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
        + f"\n  body: {json.dumps(body)[:600]}"
    )
    # An empty collection validates against any item schema, so a listing must
    # actually carry a row for the check to have looked at anything.
    if expect_rows:
        for key in ("transactions", "packages", "plans", "lots"):
            rows = body.get(key) if isinstance(body, dict) else None
            if isinstance(rows, list):
                assert rows, f"{method} {path}: declared `{key}` came back empty"
        if isinstance(body, list):
            assert body, f"{method} {path}: the declared collection came back empty"
    return body


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(method, path, code, body_schema, request):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(RECIPES, method, path, code, body_schema, expect_rows=True)


_EMPTY_OPERATIONS = [
    (method, path, code, schema)
    for method, path, code, schema in OPERATIONS
    if _recipe_for(EMPTY_STATE, method, path, code) is not None
]


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    _EMPTY_OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in _EMPTY_OPERATIONS],
)
def test_the_wire_matches_the_declared_response_when_there_is_nothing_there(
    method, path, code, body_schema, request
):
    """The same claim, asked in the state where the nulls live."""
    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(EMPTY_STATE, method, path, code, body_schema, expect_rows=False)


def test_the_gate_is_not_blind():
    """A canary: swap a declared schema for one the wire cannot satisfy.

    Everything above can be green for two reasons — the claims are honest, or
    the check never looks at the body. This tells them apart by validating a
    real response against ``{"type": "string"}``: every operation here answers
    an object, so every one of them must fail. If any passes, the validation
    in ``_drive`` is not reaching the received body and this whole file proves
    nothing. With ``KNOWN_MISMATCHES`` empty this covers the entire declared
    surface — ``POST /webhooks/stripe`` included, which is the one operation
    whose own declaration (a bare ``object``) is too weak to catch anything.
    """
    honest = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if (method, path) not in KNOWN_MISMATCHES and (method, path) not in UNDRIVABLE
    ]
    assert honest, "nothing left to canary"

    survivors = []
    for method, path, code in honest:
        try:
            _drive(RECIPES, method, path, code, {"type": "string"}, expect_rows=False)
        except AssertionError:
            continue
        survivors.append(f"{method} {path}")
    assert not survivors, (
        "these operations passed validation against {'type': 'string'} — the "
        "gate is not looking at the body it received:\n  " + "\n  ".join(survivors)
    )
