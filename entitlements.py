"""comm Function providers of the billing module: entitlements + debit.

Other modules gate features and charge credits by name — no import of this
app, no HTTP client code (the transport is deployment configuration, see
STAPEL_COMM)::

    from stapel_core.comm import call

    result = call(
        "billing.check_entitlement",
        {"user_id": str(workspace.owner_id), "key": "workspaces.members.max",
         "quantity": accepted + pending + 1},
    )
    # -> {"allowed": bool, "limit": int | None, "reason": str | None}

    result = call(
        "billing.debit",
        {"user_id": str(user_id), "credits": 5,
         "idempotency_key": f"ws-provision:{invitation_id}"},
    )
    # -> {"ok": bool, "balance": int | None, "reason": str | None,
    #     "transaction_id"?: str}

Callers that must degrade when billing is not installed catch
``FunctionNotRegistered`` / ``FunctionRouteNotConfigured`` and treat it as
"everything allowed" (the OSS default — see stapel-workspaces).

``check_entitlement`` semantics (all branches):

* The user's effective plan is their ``Subscription`` row's plan while the
  subscription is in a credit-granting status (``active`` / ``trialing`` /
  ``past_due`` — the grace statuses Stripe keeps a subscription alive in).
* No subscription row, or a ``cancelled`` / ``incomplete`` one, falls back
  to the catalogue's default plan — the ``Subscription.plan`` model default
  (``"free"``), exactly what ``SubscriptionView`` materialises for a user
  without a subscription.
* A plan slug missing from the catalogue (host reconfigured ``PLANS``)
  contributes no entitlements — every key is then unknown.
* Unknown key (not declared in the plan's ``entitlements``) →
  ``allowed=True, limit=None``. Unknown keys never deny: the conservative
  OSS default, so host modules can introduce new entitlement keys without
  every deployment re-declaring its plans.
* ``bool`` value → feature switch: ``allowed = value`` (``quantity`` is
  ignored), ``limit=None``, ``reason="not_in_plan"`` when denied.
* ``int`` value → ceiling: ``allowed = quantity <= value``,
  ``limit=value``, ``reason="limit_exceeded"`` when denied. ``quantity``
  (default 1) is the prospective total, not a delta — billing does not
  track usage; the caller counts and passes current+1.

``billing.debit`` wraps :func:`stapel_billing.services.debit` (previously
reachable only through the internal HTTP endpoint
:class:`stapel_billing.views.InternalDebitView`) with the same idempotency
contract; ``idempotency_key`` is *required* here because comm delivery is
at-least-once. Failures are structural (``ok=False`` + ``reason``), never
exceptions — insufficient balance is an expected outcome, not an error.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from stapel_core.comm import register_function

logger = logging.getLogger(__name__)

CHECK_ENTITLEMENT = "billing.check_entitlement"
DEBIT = "billing.debit"

# Denial reasons (structural — mapped to error keys by the caller, e.g.
# workspaces' error.402.entitlement_required / error.402.member_limit_reached).
REASON_NOT_IN_PLAN = "not_in_plan"
REASON_LIMIT_EXCEEDED = "limit_exceeded"
REASON_INSUFFICIENT_CREDITS = "insufficient_credits"
REASON_USER_NOT_FOUND = "user_not_found"

# Single source of truth for the payload contracts: the committed schema
# files (registered by the schemas/ autoloader too; passing them at
# register_function() time makes validation work even without it).
_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas" / "functions"


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS_DIR / f"{name}.json").read_text())


CHECK_ENTITLEMENT_SCHEMA = _load_schema(CHECK_ENTITLEMENT)
DEBIT_SCHEMA = _load_schema(DEBIT)

def _granting_statuses() -> tuple:
    """Subscription statuses under which the subscribed plan's entitlements
    apply. Mirrors the credit-granting lifecycle: ``past_due`` is Stripe's
    dunning grace window (the subscription is still alive), while
    ``cancelled`` / ``incomplete`` fall back to the default plan."""
    from .models import SubscriptionStatus

    return (
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.TRIALING,
        SubscriptionStatus.PAST_DUE,
    )


def _effective_plan_entry(user_id: str):
    """Resolve the user's effective PlanCatalogEntry, or None.

    Absence of a subscription is treated exactly like the existing HTTP
    surface does (``SubscriptionView`` creates the row with model
    defaults): the default plan is ``Subscription.plan``'s field default.
    """
    from .catalog import PLANS_BY_SLUG
    from .models import Subscription

    sub = (
        Subscription.objects.filter(user_id=user_id)
        .only("plan", "status")
        .first()
    )
    if sub is not None and sub.status in _granting_statuses():
        slug = sub.plan
    else:
        slug = Subscription._meta.get_field("plan").get_default()
    return PLANS_BY_SLUG.get(slug)


def check_entitlement(payload: dict) -> dict:
    """Provider for ``billing.check_entitlement``.

    Payload: ``{"user_id": str, "key": str, "quantity": int = 1}``
    Returns: ``{"allowed": bool, "limit": int | None, "reason": str | None}``
    """
    quantity = payload.get("quantity", 1)
    entry = _effective_plan_entry(payload["user_id"])
    entitlements = entry.entitlements if entry is not None else {}

    if payload["key"] not in entitlements:
        # Unknown key never denies — conservative OSS default (see module
        # docstring).
        return {"allowed": True, "limit": None, "reason": None}

    value = entitlements[payload["key"]]
    if isinstance(value, bool):  # bool first: bool is a subclass of int
        return {
            "allowed": value,
            "limit": None,
            "reason": None if value else REASON_NOT_IN_PLAN,
        }
    limit = int(value)
    allowed = quantity <= limit
    return {
        "allowed": allowed,
        "limit": limit,
        "reason": None if allowed else REASON_LIMIT_EXCEEDED,
    }


def debit(payload: dict) -> dict:
    """Provider for ``billing.debit``.

    Payload: ``{"user_id": str, "credits": int, "idempotency_key": str,
    "metadata": dict = {}, "description": str = None,
    "type": str = "adjustment"}``
    Returns: ``{"ok": bool, "balance": int | None, "reason": str | None}``
    plus ``transaction_id`` on success.
    """
    from django.contrib.auth import get_user_model

    from . import services
    from .models import TransactionType

    user = get_user_model().objects.filter(id=payload["user_id"]).first()
    if user is None:
        return {"ok": False, "balance": None, "reason": REASON_USER_NOT_FOUND}

    try:
        txn = services.debit(
            user=user,
            credits=payload["credits"],
            type=payload.get("type", TransactionType.ADJUSTMENT),
            description=payload.get("description"),
            metadata=payload.get("metadata") or {},
            idempotency_key=payload["idempotency_key"],
        )
    except services.InsufficientCreditsError:
        wallet = services.get_or_create_wallet(user)
        return {
            "ok": False,
            "balance": wallet.balance,
            "reason": REASON_INSUFFICIENT_CREDITS,
        }
    return {
        "ok": True,
        "balance": txn.balance_after,
        "reason": None,
        "transaction_id": str(txn.id),
    }


def register() -> None:
    """Register this module's Function providers.

    Idempotent: re-registering the *same* handler object is a no-op, so
    AppConfig.ready() may run more than once without raising.
    """
    register_function(CHECK_ENTITLEMENT, check_entitlement, schema=CHECK_ENTITLEMENT_SCHEMA)
    register_function(DEBIT, debit, schema=DEBIT_SCHEMA)
