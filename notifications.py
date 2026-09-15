"""Outbound notifications of stapel-billing — telling the payer what happened.

Until 0.14.0 this module did not exist, and neither did any other path from
"money moved" to "the person who paid it was told". The emit at
``services._announce_payment`` has existed since 0.5.0 and is dispatched
faithfully; what was missing was anybody listening for the purpose of
writing to the human. A forensic pass over a live fleet on 2026-09-16 found
six real charges on one account, every one of them dispatched through the
outbox, and not one notification of any kind — the only subscriber in the
whole deployment re-queued parked recordings. Stripe's own receipts were
switched off on the account as well, so the payers learned they had been
charged from their card statement.

WHY THE SUBSCRIBER LIVES HERE AND THE TEMPLATE LIVES UPSTREAM
-------------------------------------------------------------
Three seams were available and only one of them is this library's:

1. In the HOST (an ``@on_action`` in the app next to the billing service).
   That is where the missing code would most obviously have gone, and it is
   the wrong place: every fleet that installs stapel-billing has the same
   hole, and a fix in one app leaves the next one silent. A second fleet on
   this same library was checked on the same day and has the identical gap.
   A defect that is identical in every host is a defect in the library.

2. Entirely in stapel-notifications (let IT subscribe to ``payment.completed``).
   Rejected: it inverts the dependency. stapel-notifications knows channels,
   templates, languages and preferences, and deliberately knows nothing about
   any domain's event vocabulary — it has no idea what a plan, a credit
   package or a billing period is, and would have to grow a billing catalogue
   reader to render "what was bought".

3. Split, which is what the fleet already does everywhere else and what this
   module implements: the DOMAIN owns the fact and the variables, the
   NOTIFICATIONS module owns the copy, the channel and the language. It is
   stapel-auth's shape (``otp/services.py``), stapel-moderation's shape
   (``notifications.py``, which this file is deliberately modelled on), and
   it is the shape ``manage.py check_notifications`` lints for: every
   ``request_notification("literal")`` call site here is checked against the
   registry upstream, so a type this module requests and nobody registered is
   a lint failure rather than a letter that silently goes nowhere.

The three types (``billing.payment_succeeded``, ``billing.payment_failed``,
``billing.subscription_ending``) are registered in stapel-notifications
0.20.0 under the ``billing`` group — mandatory, no unsubscribe. A receipt for
money the platform took is a record the payer is owed; it is not platform
news they can switch off.

IDEMPOTENCY
-----------
Action delivery is at-least-once, by design and in practice: the outbox
retries, the broker redelivers, and a replayed row must not produce a second
receipt. Every send here is therefore claimed first under
:func:`services.claim_provider_object` — the unique-constraint claim table
the credit grants already use — keyed on the identity of the THING, not of
the delivery: the transaction id for a payment, the invoice id for a failure,
and ``(subscription, period_end)`` for a pending cancellation, so a
subscription that is updated eleven times during one period still produces
one letter about that period.

Claims are taken under ``provider="notify"`` rather than ``"stripe"``, so a
notification claim can never collide with the GRANT claim on the same Stripe
object id — the two are different questions about the same identifier and
must not share a row.

The claim is taken BEFORE the publish and released if the publish fails, and
the handler then raises so the broker redelivers. Claim-and-never-send is the
one failure mode this module exists to prevent, so it is not allowed to be
the quiet path.

THE VARIABLE-NAME TRAP
----------------------
stapel-notifications merges the resolved copy into the template context
first and the caller's variables second, dropping any caller variable whose
name collides with a copy slot (``subject``, ``heading``, ``body``, ``cta``,
``note``, ``period``, ``reason``, ``warning``). Every name below dodges it on
purpose — ``item_name`` not ``body``, ``decline_reason`` not ``reason``,
``period_start``/``period_end`` not ``period``. That is the reason for the
spelling, not house style.

DATES ARE ISO, DELIBERATELY
---------------------------
This module does not know what language the letter will be written in — that
is resolved downstream from the recipient's profile, which is exactly where
it belongs. So it passes ``YYYY-MM-DD`` rather than a rendered "28 September
2026" that would arrive in English inside a Russian email. Formatting a date
in the reader's language is a real improvement and it belongs in
stapel-notifications, next to the language it resolves.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

#: The types this module requests. Registered upstream in
#: stapel-notifications 0.20.0; ``manage.py check_notifications`` validates
#: these literals against that registry.
TYPE_PAYMENT_SUCCEEDED = "billing.payment_succeeded"
TYPE_PAYMENT_FAILED = "billing.payment_failed"
TYPE_SUBSCRIPTION_ENDING = "billing.subscription_ending"

REQUESTED_TYPES = (
    TYPE_PAYMENT_SUCCEEDED,
    TYPE_PAYMENT_FAILED,
    TYPE_SUBSCRIPTION_ENDING,
)

#: Symbols for the currencies this library's own catalogue can be priced in,
#: plus the majors a host is likely to configure. Anything else is rendered
#: as "12.34 SEK" — a correct, readable amount in a currency we have no
#: symbol for, rather than a wrong symbol or a bare number.
_CURRENCY_SYMBOLS = {
    "usd": "$",
    "eur": "€",
    "gbp": "£",
    "rub": "₽",
    "jpy": "¥",
}

#: Currencies with no minor unit. Dividing these by 100 invents two decimal
#: places that do not exist and understates the price by a factor of a
#: hundred — ¥1500 billed would read "¥15.00".
_ZERO_DECIMAL_CURRENCIES = frozenset({
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
    "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
})


def format_amount(amount_cents, currency: str) -> str:
    """``(2100, "usd")`` → ``"$21.00"``; ``(1500, "jpy")`` → ``"¥1500"``.

    Returns ``""`` for an amount that is not a number, which the callers
    treat as "no letter": an amount is the one thing a receipt cannot be
    written without, and a receipt for "None" is worse than the silence
    this module exists to end — it is silence plus a support ticket.
    """
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool):
        try:
            amount_cents = int(amount_cents)
        except (TypeError, ValueError):
            logger.error(
                "billing notification: amount_cents=%r is not a number — "
                "refusing to render an amount", amount_cents,
            )
            return ""
    code = (currency or "").strip().lower()
    symbol = _CURRENCY_SYMBOLS.get(code)
    if code in _ZERO_DECIMAL_CURRENCIES:
        figure = f"{amount_cents:,}"
    else:
        figure = f"{amount_cents / 100:,.2f}"
    if symbol:
        return f"{symbol}{figure}"
    return f"{figure} {code.upper()}" if code else figure


def item_label(*, package: str | None = None, plan: str | None = None) -> str:
    """The human name of what was bought, from the configured catalogue.

    Falls back to the slug when the catalogue no longer carries the entry —
    a plan a host renamed or retired is still a plan somebody paid for, and
    naming it by slug is worse copy but true. Falls back to a generic label
    only when the fact names neither, which is the top-up case.
    """
    from .catalog import CREDIT_PACKAGES_BY_SLUG, PLANS_BY_SLUG

    if plan:
        entry = PLANS_BY_SLUG.get(plan)
        return getattr(entry, "name", None) or str(plan)
    if package:
        entry = CREDIT_PACKAGES_BY_SLUG.get(package)
        return getattr(entry, "name", None) or str(package)
    return "your account credit"


def iso_date(value) -> str:
    """A date-only ``YYYY-MM-DD`` string, or ``""`` when there is no date.

    Accepts what the facts actually carry: an ISO string from a comm
    payload, or a ``datetime``/``date`` from a model field. Empty for
    anything unparseable, because every template block that shows a date is
    guarded on the date being non-empty — an unparseable timestamp drops the
    period line and keeps the letter, instead of losing the letter.
    """
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        text = str(value)
        # fromisoformat on 3.11+ accepts the trailing Z; be explicit anyway.
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError):
        logger.warning("billing notification: %r is not an ISO timestamp", value)
        return ""


#: Returned by :func:`staleness_refusal` when the fact carries no usable
#: timestamp at all. Kept as a named constant so the handlers and the tests
#: agree on the case without matching on prose.
NO_TIMESTAMP = "carries no usable timestamp"


def staleness_refusal(timestamp, *, now=None) -> str | None:
    """``None`` = send it. A string = the reason not to, ready to log.

    THE PROBLEM THIS EXISTS FOR. The send-once claim makes a redelivery
    silent, but only for a payment that was ALREADY notified under 0.14.0 or
    later. Every payment taken before this module existed has no claim row,
    because the code that writes them is the code that was missing — so a
    replayed outbox row from before the fix looks, to the claim table, exactly
    like a payment that just happened. Six real charges on the fleet this was
    found on are in precisely that state.

    A receipt that arrives three weeks after the charge is worse than the
    silence it replaces: the payer has already reconciled the statement, and
    the letter reads as a second charge or as a system that has lost track of
    time. So freshness is a property of the FACT, checked before the claim —
    the claim table goes on meaning "a letter was sent" and never "a letter
    was considered".

    ``STAPEL_BILLING["NOTIFY_MAX_AGE_SECONDS"]`` is the window; ``0`` (or
    ``None``) switches the gate off, which is the deliberate escape hatch for
    a host that has decided to backfill and wants these letters to fire for
    old facts. Seven days by default: an outbox that is a week behind is an
    incident a human should be deciding about, not a queue that should quietly
    start mailing.

    A fact with NO timestamp is refused rather than sent. ``created_at`` is
    required by this library's own emit schema, so its absence means a
    malformed or hand-made payload — and "I cannot tell how old this is" must
    not resolve to "mail it", which is the exact failure the gate is here to
    prevent. The refusal is logged with its remedy by the caller.
    """
    from django.utils import timezone

    from .conf import billing_settings

    max_age = billing_settings.NOTIFY_MAX_AGE_SECONDS
    if not max_age:
        return None

    moment = _parse_moment(timestamp)
    if moment is None:
        return NO_TIMESTAMP

    now = now or timezone.now()
    age = (now - moment).total_seconds()
    if age > max_age:
        return f"is {int(age // 86400)} day(s) old (limit {int(max_age // 86400)})"
    return None


def _parse_moment(value):
    """An aware datetime from what a comm payload or a model field carries."""
    from django.utils import timezone

    if value in (None, ""):
        return None
    moment = value
    if not isinstance(moment, datetime):
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if timezone.is_naive(moment):
        # A naive timestamp is ambiguous, but treating it as UTC is what every
        # other reader of these payloads does and is right for the only
        # producer that exists. Never silently "now": that would read every
        # malformed fact as perfectly fresh.
        moment = timezone.make_aware(moment, timezone.utc)
    return moment


def billing_page_url() -> str:
    """Where a person manages their card and their plan, or ``""``.

    Empty rather than invented — the same rule the redirect settings follow
    and the same rule stapel-moderation's ``appeal_url`` follows. A "update
    your payment method" button that 404s converts a recoverable declined
    card into a support ticket, so the templates render no button at all
    when the host has not said where its billing page is.
    """
    from .conf import billing_settings

    return (billing_settings.BILLING_PAGE_URL or "").strip()


def _send(notification_type: str, *, user_id, variables: dict) -> bool:
    """Publish one notification request. False means it did not go out.

    Never raises for a missing recipient or a bad type — those are decisions
    this module already made — but does NOT swallow a transport failure:
    the caller turns that into a retry, because a receipt that was dropped
    because the bus blinked is the defect this whole module is about.
    """
    from stapel_core.notifications import request_notification

    return bool(request_notification(
        notification_type,
        user_id=str(user_id),
        variables=variables,
        source_service="stapel-billing",
    ))


def payment_succeeded_variables(payload: dict) -> dict | None:
    """Template variables for a ``payment.completed`` fact, or None to skip.

    None means "this fact cannot be made into a truthful receipt" — today
    that is only an unusable amount. It is deliberately NOT "no invoice
    link": a receipt with an amount, an item and no link is a complete,
    useful letter, and refusing to send it because Stripe's payload happened
    not to carry a hosted invoice URL would reproduce the silence in a new
    place.
    """
    amount = format_amount(payload.get("amount_cents"), payload.get("currency"))
    if not amount:
        return None
    variables = {
        "amount": amount,
        "item_name": item_label(
            package=payload.get("package"), plan=payload.get("plan")
        ),
    }
    # Both halves or neither: the copy reads "from {period_start} to
    # {period_end}" and the template shows it only when both are present,
    # so a half-known period silently drops the line rather than printing
    # a sentence with a literal "{period_end}" in it.
    start = iso_date(payload.get("period_start"))
    end = iso_date(payload.get("period_end"))
    if start and end:
        variables["period_start"] = start
        variables["period_end"] = end
    invoice_url = (payload.get("invoice_url") or "").strip()
    if invoice_url:
        variables["invoice_url"] = invoice_url
    return variables


def payment_failed_variables(payload: dict) -> dict | None:
    """Template variables for a ``payment.failed`` fact, or None to skip."""
    amount = format_amount(payload.get("amount_cents"), payload.get("currency"))
    if not amount:
        return None
    variables = {
        "amount": amount,
        "item_name": item_label(
            package=payload.get("package"), plan=payload.get("plan")
        ),
    }
    # Stripe's machine codes ("insufficient_funds") are not copy. The human
    # sentence it sends alongside them is, so that is what is forwarded; a
    # code we have words for is turned into words here, and a code we do
    # not is dropped. The key is set only when there is something to say —
    # an empty string still renders the "Reason given by your bank:" line
    # with nothing after the colon.
    reason = _humanise_decline(payload.get("decline_reason") or "")
    if reason:
        variables["decline_reason"] = reason
    retry_url = billing_page_url()
    if retry_url:
        variables["retry_url"] = retry_url
    return variables


#: The decline codes worth a sentence of their own. Anything outside this
#: table is forwarded as the provider's own human message when there is one,
#: and otherwise omitted — a raw code in a customer email ("your payment
#: failed: do_not_honor") tells the reader nothing and looks like a leak.
_DECLINE_SENTENCES = {
    "insufficient_funds": "the card did not have enough available funds",
    "card_declined": "the card was declined by the bank",
    "expired_card": "the card has expired",
    "incorrect_cvc": "the security code did not match",
    "processing_error": "the bank could not process the payment",
    "authentication_required": "the bank asked for confirmation that was not given",
}


def _humanise_decline(reason: str) -> str:
    key = reason.strip().lower().replace(" ", "_")
    sentence = _DECLINE_SENTENCES.get(key)
    if sentence:
        return sentence
    # Not a code we know. If it reads like a code (single token, underscores,
    # no spaces) it is not copy and is dropped; a provider sentence is kept.
    if "_" in reason and " " not in reason.strip():
        return ""
    return reason.strip()


def subscription_ending_variables(payload: dict) -> dict | None:
    """Template variables for a subscription that will not renew, or None.

    None when the fact does not actually describe a pending cancellation —
    the caller checks that too, but the rule that a letter announcing an end
    date must HAVE an end date lives with the copy it fills in.
    """
    end = iso_date(payload.get("current_period_end"))
    if not end:
        return None
    variables = {
        "item_name": item_label(plan=payload.get("plan")),
        "period_end": end,
    }
    resubscribe_url = billing_page_url()
    if resubscribe_url:
        variables["resubscribe_url"] = resubscribe_url
    return variables
