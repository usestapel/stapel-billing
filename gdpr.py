"""GDPR provider for the credit ledger — erasure removes the person, the bill stays.

The deletion-lifecycle spec's §6 verdict for every owner that carries a
ledger (agent, video, billing): *erasure removes what the person wrote and
pseudonymizes the ids that name them; the economics stay*. A wallet's
history is the product's financial record — what was purchased, what was
spent, what expired — and deleting it would silently restate closed
reporting periods. So nothing here is deleted:

* the ids that **name the subject** are pseudonymized — a keyed HMAC under
  the deployment's ``SECRET_KEY``, the same funnel as
  ``stapel_video.presence.pseudonymize_user`` and ``stapel_agent.gdpr``,
  down to the ``erased:`` prefix and the 32-hex truncation;
* the free-text a caller supplied is scrubbed: ``description`` on every
  transaction, hold and debt, and ``metadata`` cut down to the accounting
  keys this package writes itself (:data:`LEDGER_METADATA_KEYS`);
* the processor's names for the person go too — the Stripe customer and
  subscription ids on :class:`~stapel_billing.models.Subscription`, and the
  raw webhook payloads that carry their email, address and card details;
* amounts, credit counts, balances, lot expiries and timestamps are not
  read and not written.

One operation, :func:`erase_subject`, reached identically by the
in-process provider below and by the ``gdpr.erasure.requested`` subscriber
in :mod:`stapel_billing.actions` — so a monolith and a fleet erase the same
rows the same way.

Before 0.9.0 the provider's ``delete()`` tried
``Wallet.objects.filter(user_id=...).update(user_id=None)`` against a
NOT NULL column inside a bare ``except Exception: pass``. It raised on every
wallet it was given and reported success; that is the defect this module's
receipts exist to expose, and the migration that makes the column nullable
is what makes the anonymisation actually possible.
"""
from __future__ import annotations

from stapel_core.gdpr import GDPRProvider


def _serialize_dates(rows: list[dict]) -> list[dict]:
    return [
        {k: v.isoformat() if hasattr(v, 'isoformat') else v for k, v in row.items()}
        for row in rows
    ]


class BillingGDPRProvider(GDPRProvider):
    section = 'billing'

    def export(self, user_id: int) -> dict:
        from .models import (
            CreditDebt,
            CreditHold,
            CreditLot,
            Subscription,
            Transaction,
            Wallet,
        )

        try:
            wallet = Wallet.objects.get(user_id=user_id)
            wallet_data = {
                'balance':   str(wallet.balance),
                'currency':  wallet.currency,
                'created_at': wallet.created_at.isoformat(),
            }
            transactions = list(Transaction.objects.filter(wallet=wallet).values(
                'type', 'amount_cents', 'credits_delta', 'balance_after', 'description', 'created_at',
            ))
            # Where the balance came from and when it dies is part of the
            # answer to "what do you hold about me" — a single number is not.
            lots = list(CreditLot.objects.filter(wallet=wallet).values(
                'source', 'credits_initial', 'credits_remaining', 'expires_at', 'created_at',
            ))
            holds = list(CreditHold.objects.filter(wallet=wallet).values(
                'credits', 'type', 'description', 'status', 'expires_at',
                'resolved_at', 'created_at',
            ))
            # What the subject OWES is as much part of "what do you hold
            # about me" as what they have — and an export that showed only
            # the balance would describe a wallet that does not exist.
            debts = list(CreditDebt.objects.filter(wallet=wallet).values(
                'credits_initial', 'credits_outstanding', 'reason', 'type',
                'description', 'settled_at', 'created_at',
            ))
        except Wallet.DoesNotExist:
            wallet_data   = {}
            transactions  = []
            lots          = []
            holds         = []
            debts         = []

        try:
            sub = Subscription.objects.get(user_id=user_id)
            sub_data = {
                'plan':                 sub.plan,
                'status':               sub.status,
                'current_period_start': sub.current_period_start.isoformat() if sub.current_period_start else None,
                'current_period_end':   sub.current_period_end.isoformat() if sub.current_period_end else None,
                'cancelled_at':         sub.cancelled_at.isoformat() if sub.cancelled_at else None,
            }
        except Subscription.DoesNotExist:
            sub_data = {}

        return {
            'wallet':       wallet_data,
            'credit_lots':  _serialize_dates(lots),
            'credit_holds': _serialize_dates(holds),
            'credit_debts': _serialize_dates(debts),
            'transactions': _serialize_dates(transactions),
            'subscription': sub_data,
        }

    def delete(self, user_id: int) -> None:
        """Erase the subject — the same operation the comm path runs.

        Pseudonymize the ids that name them, scrub the free text, keep the
        amounts. See :func:`erase_subject`, which is where the operation
        lives; this method exists so a monolith reaches it through the
        registry the way a fleet reaches it through the bus.
        """
        erase_subject('account', user_id)

    #: ``anonymize`` IS ``delete`` here, not a weaker cousin: after the run
    #: the rows hold amounts and a pseudonym, which is what an
    #: anonymisation is meant to produce. Two names for one operation, and
    #: the alias is named so nobody looks for a second implementation.
    anonymize = delete


#: The name this module answers to in ``STAPEL_GDPR["DATA_OWNERS"]`` — the
#: same string as :attr:`BillingGDPRProvider.section`, because an owner with
#: two names is an owner whose receipts land on nobody's part.
OWNER = BillingGDPRProvider.section

#: Subject types this module can actually erase, and therefore the only
#: ones it claims and answers ``gdpr.owner.alive`` with.
#:
#: ``account`` only, and that is not a shortcut: every row in this package
#: hangs off a :class:`~stapel_billing.models.Wallet` or a
#: :class:`~stapel_billing.models.Subscription`, and both are keyed by
#: exactly one id — the user's. There is no ``workspace_id`` column, no
#: entity ref, and no ``billing.*`` payload that carries one, so a
#: workspace or meeting erasure has no key to match on here. Claiming the
#: type would produce a receipt for work nobody could have done.
SUBJECT_TYPES = ('account',)

#: Marks a pseudonymized id, so a reader can tell one from a real host id
#: and a second erasure can leave it alone. Same spelling as
#: ``stapel_video.presence.pseudonymize_user``.
PSEUDONYM_PREFIX = 'erased:'

#: The ``metadata`` keys this package writes for accounting — the
#: dimensions a charge is explained by. Erasure keeps these and drops
#: everything else, because everything else is a caller's annotation: the
#: ``metadata`` argument of :func:`~stapel_billing.services.debit`,
#: :func:`~stapel_billing.services.hold` and
#: :func:`~stapel_billing.services.capture` is free-form, and a host that
#: files a meeting title or an email address there has filed content.
#:
#: An allowlist and not a denylist on purpose: a key nobody anticipated
#: must fall on the erasing side.
#:
#: The Stripe ids this package writes (``stripe_session_id``,
#: ``stripe_subscription_id``, ``stripe_invoice_id``) are deliberately
#: **not** on the list. They are the processor's names for the same person,
#: and leaving them behind would keep the row joinable to a live Stripe
#: customer after we swore it was not.
LEDGER_METADATA_KEYS = frozenset({
    'expires_at',
    'hold_id',
    'lot_id',
    'lots',
    'package',
    'plan',
    'source',
    # 0.11.0 accounting dimensions, all integers or enum values this
    # package writes itself. They are what makes a partial charge, a debt
    # settlement, a clawback or a non-provider bundle explainable after the
    # person is gone — which is the half of the ledger erasure keeps.
    'clawback_of',
    'clawback_reason',
    'debt_id',
    'debt_reason',
    'debt_settlement',
    'forgiven',
    'grant',
    'outstanding',
    'partial',
    'period_key',
    'reclaimed',
    'requested',
    'requested_credits',
    'shortfall',
})

#: What a scrubbed webhook payload becomes. The row itself stays: its
#: ``stripe_event_id`` is the unique key that stops a redelivered webhook
#: from crediting a wallet twice, and dropping it would trade a privacy
#: promise for a double-grant.
ERASED_PAYLOAD = {'erased': True}


def pseudonymize(value) -> str:
    """Replace an id with a stable pseudonym — the fleet's one scheme.

    A keyed digest (HMAC-SHA256 under the deployment's ``SECRET_KEY``),
    mirroring ``stapel_video.presence.pseudonymize_user`` down to the
    prefix and the 32-hex truncation: stable, so one subject's rows stay
    one subject and per-subject arithmetic still works, and not reversible
    without the key. Never a plain hash — a bare digest of a user id is a
    rainbow table away from being the id again.

    Idempotent: a value that is already a pseudonym is returned as-is, so a
    redelivered erasure cannot mint a second pseudonym for one subject and
    split its history in two.

    A ``SECRET_KEY`` rotation splits pseudonyms — an erasure after the
    rotation produces a different value for the same id. Documented and
    accepted fleet-wide: the alternative is a second key nobody rotates.
    """
    import hashlib
    import hmac

    from django.conf import settings

    value = str(value)
    if value.startswith(PSEUDONYM_PREFIX):
        return value
    digest = hmac.new(
        str(settings.SECRET_KEY).encode(), value.encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f'{PSEUDONYM_PREFIX}{digest}'


def _scrub_free_text(model, wallet_ids) -> int:
    """Blank ``description`` and cut ``metadata`` on one model's rows.

    Returns the number of rows **touched** — the receipt's count, and the
    reason a redelivery reports ``0`` instead of claiming the work twice.
    """
    rows = model.objects.filter(wallet_id__in=wallet_ids)
    touched = 0
    pending = []
    for row in rows.only('id', 'description', 'metadata').iterator():
        meta = row.metadata if isinstance(row.metadata, dict) else {}
        kept = {k: v for k, v in meta.items() if k in LEDGER_METADATA_KEYS}
        if not row.description and kept == row.metadata:
            continue  # already scrubbed (or never carried anything)
        row.description = ''
        row.metadata = kept
        pending.append(row)
        touched += 1
        if len(pending) >= 500:
            model.objects.bulk_update(pending, ['description', 'metadata'])
            pending = []
    if pending:
        model.objects.bulk_update(pending, ['description', 'metadata'])
    return touched


def _scrub_webhook_payloads(customer_ids, subscription_ids) -> int:
    """Erase the raw Stripe payloads that describe this customer.

    A ``customer.subscription.*`` or ``invoice.*`` payload carries the
    person's email, billing address and card details — by volume the
    largest store of their PII in this package, and one no earlier
    erasure touched. The rows are found by the ids the payloads are keyed
    on (``data.object.customer``, and ``data.object.id`` for the
    subscription objects themselves), not by a scan: a JSON key lookup is
    exact on every backend Django supports, and a text scan over every
    webhook ever received is neither.
    """
    from .models import StripeWebhookEvent

    if not customer_ids and not subscription_ids:
        return 0
    touched = 0
    seen: set = set()
    lookups = [{'payload__data__object__customer': cid} for cid in customer_ids]
    lookups += [{'payload__data__object__id': sid} for sid in subscription_ids]
    for lookup in lookups:
        for pk in StripeWebhookEvent.objects.filter(**lookup).exclude(
            payload=ERASED_PAYLOAD
        ).values_list('pk', flat=True):
            seen.add(pk)
    if seen:
        touched = StripeWebhookEvent.objects.filter(pk__in=seen).update(
            payload=ERASED_PAYLOAD, error=''
        )
    return touched


def erase_subject(subject_type: str, subject_key, *, workspace_id=None) -> dict | None:
    """Erase one subject from the credit ledger: the person out, the bill kept.

    Returns the receipt's ``counts`` — rows **touched** per model — or
    ``None`` when *subject_type* is not one this module claims (the caller
    then owes no receipt: stapel-gdpr creates a part only for owners that
    claim the type).

    ``credit_lots`` and ``hold_allocations`` are absent from the counts on
    purpose and not by omission: they carry no free text and no id of their
    own — a lot is a source, a count and an expiry hanging off a wallet —
    so the wallet's pseudonymization is their erasure, and reporting a
    number for them would inflate a receipt with work that was not done.

    Idempotent: the wallet's ``user`` is NULL after the first run, so a
    redelivery matches nothing and receipts zeroes. ``workspace_id`` is
    accepted and ignored — an account request may carry it as a partition
    hint for owners that need one, and narrowing by it here would leave the
    subject's rows in every other tenant.
    """
    from .models import CreditDebt, CreditHold, Subscription, Transaction, Wallet

    key = str(subject_key) if subject_key is not None else ''
    if not key or subject_type not in SUBJECT_TYPES:
        return None

    counts = {
        'wallets': 0,
        'subscriptions': 0,
        'transactions': 0,
        'credit_holds': 0,
        'credit_debts': 0,
        'webhook_events': 0,
    }

    customer_ids: list[str] = []
    subscription_ids: list[str] = []
    for sub in Subscription.objects.filter(user_id=key):
        if sub.stripe_customer_id:
            customer_ids.append(sub.stripe_customer_id)
        if sub.stripe_subscription_id:
            subscription_ids.append(sub.stripe_subscription_id)
        sub.user = None
        sub.user_pseudonym = pseudonymize(key)
        sub.stripe_subscription_id = ''
        sub.stripe_customer_id = ''
        sub.save(update_fields=[
            'user', 'user_pseudonym', 'stripe_subscription_id', 'stripe_customer_id',
        ])
        counts['subscriptions'] += 1

    # Pin the wallets by primary key before detaching them: everything
    # below is reached through the wallet FK, and a queryset that filters
    # on ``user_id`` would stop matching its own rows halfway through.
    wallet_ids = list(Wallet.objects.filter(user_id=key).values_list('pk', flat=True))
    if wallet_ids:
        counts['transactions'] = _scrub_free_text(Transaction, wallet_ids)
        counts['credit_holds'] = _scrub_free_text(CreditHold, wallet_ids)
        # Same free-text problem as a hold: a debt carries the debiting
        # caller's description and its metadata verbatim.
        counts['credit_debts'] = _scrub_free_text(CreditDebt, wallet_ids)
        counts['wallets'] = Wallet.objects.filter(pk__in=wallet_ids).update(
            user=None, user_pseudonym=pseudonymize(key)
        )

    counts['webhook_events'] = _scrub_webhook_payloads(customer_ids, subscription_ids)
    return counts


__all__ = [
    'ERASED_PAYLOAD',
    'LEDGER_METADATA_KEYS',
    'OWNER',
    'PSEUDONYM_PREFIX',
    'SUBJECT_TYPES',
    'BillingGDPRProvider',
    'erase_subject',
    'pseudonymize',
]
