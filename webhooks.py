"""Stripe webhook event → handler registry.

``StripeWebhookView`` used to route with an ``if/elif`` chain over
``event["type"]``. A chain is not a seam: a host project that wanted to
react to ``charge.dispute.created``, or to replace what
``checkout.session.completed`` does with its own reconciliation, had
exactly two options — fork the view, or subclass it and re-type the whole
chain around one changed branch. Both make the host carry a copy of
routing logic this library keeps changing.

The routing is a **registry merged over the built-ins**, the same canon as
``STAPEL_NOTIFICATIONS["TYPES"]`` and ``STAPEL_WORKSPACES["ROLES"]``:
last-wins per event type, so a host adds a type without upstream and
overrides a built-in without forking::

    STAPEL_BILLING = {
        "STRIPE_WEBHOOK_HANDLERS": {
            # add an event the library does not carry
            "customer.updated": "myproject.billing.on_customer_updated",
            # override a built-in with your own reconciliation
            "checkout.session.completed": "myproject.billing.on_checkout",
            # or switch a built-in OFF: None means "ignore this type"
            "customer.subscription.deleted": None,
        },
    }

A value is a callable taking the Stripe event dict, a dotted path to one,
or ``None`` to disable. Resolution happens at dispatch time, not at
import: a bad dotted path is a boot-time ``stapel_billing.E106`` (checks.py)
rather than an import-order surprise, and a test that flips the setting
does not have to re-import the module.

What the registry deliberately does NOT own: signature verification,
the idempotency claim, the ``select_for_update`` lock and the
``processed_at`` mark. Those are the view's, they wrap the handler, and a
host overriding one event type must not be able to opt out of them —
that is how a paid checkout gets credited twice.
"""
from __future__ import annotations

from typing import Callable, Optional

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from .conf import billing_settings

#: Event types this library handles out of the box. Several types
#: deliberately share one handler — Stripe sends both ``invoice.paid`` and
#: ``invoice.payment_succeeded`` for the same money.
#:
#: The built-ins are dotted paths rather than imported function objects, and
#: that is load-bearing twice over: the registry resolves at DISPATCH time,
#: so ``services`` is not imported to define this table (no import cycle with
#: the view), and a test or a host that replaces ``services.handle_*`` is
#: actually replacing what runs, instead of leaving this table holding the
#: old function object it captured at import.
BUILTIN_STRIPE_HANDLERS: dict[str, str] = {
    "checkout.session.completed": "stapel_billing.services.handle_checkout_completed",
    "invoice.paid": "stapel_billing.services.handle_invoice_paid",
    "invoice.payment_succeeded": "stapel_billing.services.handle_invoice_paid",
    "customer.subscription.created": "stapel_billing.services.handle_subscription_updated",
    "customer.subscription.updated": "stapel_billing.services.handle_subscription_updated",
    "customer.subscription.deleted": "stapel_billing.services.handle_subscription_deleted",
    # Money going back the other way. Until 0.11.0 nothing here handled a
    # refund at all: the card was refunded, the dispute was lost, the
    # invoice was credited — and the credits those payments bought stayed
    # spendable. Free credits by webhook silence.
    "charge.refunded": "stapel_billing.services.handle_charge_refunded",
    "charge.dispute.created": "stapel_billing.services.handle_dispute_created",
    "credit_note.created": "stapel_billing.services.handle_credit_note_created",
}


def _resolve(value):
    """A registry value → a callable, or None for "ignore this type"."""
    if value is None:
        return None
    if callable(value):
        return value
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            return import_string(value)
        except ImportError as exc:
            raise ImproperlyConfigured(
                f"STAPEL_BILLING['STRIPE_WEBHOOK_HANDLERS'] points at "
                f"{value!r}, which cannot be imported. A webhook whose "
                f"handler does not resolve is a payment event silently "
                f"dropped, so this refuses instead of logging."
            ) from exc
    raise ImproperlyConfigured(
        f"STAPEL_BILLING['STRIPE_WEBHOOK_HANDLERS'] value {value!r} is "
        f"neither a callable, a dotted path, nor None."
    )


def stripe_handlers() -> dict[str, Optional[Callable[[dict], None]]]:
    """Effective registry: settings merged OVER the built-ins.

    Last-wins per event type. Values are resolved callables — or ``None``
    for a type the host explicitly switched off, which is why the mapping
    keeps the key rather than dropping it: "registered as ignored" and
    "never registered" read the same to a caller otherwise, and only the
    second deserves the unhandled-event log line.
    """
    overrides = billing_settings.STRIPE_WEBHOOK_HANDLERS or {}
    raw: dict[str, object] = {**BUILTIN_STRIPE_HANDLERS}
    for event_type, value in overrides.items():
        raw[str(event_type)] = value
    return {event_type: _resolve(value) for event_type, value in raw.items()}


def get_stripe_handler(event_type: str) -> Optional[Callable[[dict], None]]:
    """The handler for one Stripe event type, or None when there is none.

    ``None`` covers both "no entry" and "an entry the host disabled": in
    either case the view has nothing to run. Use ``stripe_handlers()``
    when the difference matters.
    """
    return stripe_handlers().get(event_type)


def registered_stripe_events() -> list[str]:
    """Event types with a handler that will actually run — for checks/docs."""
    return sorted(k for k, v in stripe_handlers().items() if v is not None)


__all__ = [
    "BUILTIN_STRIPE_HANDLERS",
    "get_stripe_handler",
    "registered_stripe_events",
    "stripe_handlers",
]
