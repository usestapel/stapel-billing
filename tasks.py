"""Scheduled workers: credit expiry, hold crash-safety, balance reconciliation.

Register in Django settings::

    CELERY_BEAT_SCHEDULE = {
        **get_billing_beat_schedule(),
        ...
    }

Every task here is a plain callable first and a Celery task second, so a
deployment without Celery can drive the same three jobs from its own cron.
An unscheduled expiry is the silent half of the two-pool design: the
subscription credits that were sold as "expire at period end" simply never
do, and nothing anywhere says so — the system check
``stapel_billing.W105`` refuses to let that be discovered by an accountant.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Task names as beat sees them. The system check compares against these, so
#: a rename that misses the check would be caught by the check itself.
EXPIRE_LOTS_TASK_NAME = "stapel_billing.tasks.expire_credit_lots"
EXPIRE_HOLDS_TASK_NAME = "stapel_billing.tasks.expire_holds"
RECONCILE_TASK_NAME = "stapel_billing.tasks.reconcile_wallets"
GRANT_PLAN_BUNDLES_TASK_NAME = "stapel_billing.tasks.grant_plan_bundles"


def expire_credit_lots() -> int:
    """Zero every credit lot past its expiry, writing one EXPIRATION row each.

    Returns the number of credits expired. Wallets are swept one locked
    wallet at a time: the expiry is a balance mutation like any other, and
    doing it outside the lock every other operation takes would race a
    concurrent debit into a wrong ``balance_after``.
    """
    from django.db import transaction as db_transaction
    from django.utils import timezone

    from .models import CreditLot, Wallet
    from .services import _expire_due_lots

    now = timezone.now()
    wallet_ids = list(
        CreditLot.objects.filter(credits_remaining__gt=0, expires_at__lte=now)
        .values_list("wallet_id", flat=True)
        .distinct()
    )
    expired = 0
    for wallet_id in wallet_ids:
        with db_transaction.atomic():
            wallet = Wallet.objects.select_for_update().filter(pk=wallet_id).first()
            if wallet is None:
                continue
            expired += _expire_due_lots(wallet, now=timezone.now())
    logger.info(
        "expire_credit_lots: %s credit(s) expired across %s wallet(s)",
        expired,
        len(wallet_ids),
    )
    return expired


def expire_holds() -> int:
    """Release reservations nobody came back for. Returns how many.

    Crash safety, not policy: a hold is created before the work and resolved
    after it, so a worker that dies in between leaves credits reserved
    against an answer that will never arrive. The released hold is recorded
    as ``expired``, not ``released``, so "the caller gave them back" and
    "nobody ever came back" stay different facts.
    """
    from django.utils import timezone

    from .models import CreditHold, HoldStatus
    from .services import release

    now = timezone.now()
    stale = list(
        CreditHold.objects.filter(
            status=HoldStatus.HELD, expires_at__lte=now
        ).values_list("id", flat=True)
    )
    for hold_id in stale:
        release(hold_id=hold_id, expired=True)
    if stale:
        logger.warning(
            "expire_holds: released %s abandoned hold(s) — a pipeline is "
            "dying between hold and capture/release",
            len(stale),
        )
    return len(stale)


def reconcile_wallets() -> list[dict]:
    """Report wallets whose cached balance disagrees with their credit lots.

    Deliberately a report, not a repair: the cache is maintained under the
    wallet's row lock by every operation in ``services``, so a disagreement
    means something wrote a balance from outside the module or the expiry
    worker is not running. Rewriting the number to match would erase the
    only evidence of either.
    """
    from .services import reconcile_wallet_balances

    drifted = reconcile_wallet_balances()
    if drifted:
        logger.error(
            "reconcile_wallets: %s wallet(s) whose balance cache disagrees "
            "with their lots",
            len(drifted),
        )
    return drifted


def grant_plan_bundles(period_key: str | None = None) -> int:
    """Grant the current period's plan bundle to every entitled wallet.

    The half of the two-pool design that had no worker: credits were only
    ever granted by a Stripe webhook, so a plan whose bundle is not sold
    through Stripe — the free tier above all — never granted anything, and
    its ``monthly_credits_included`` was a number in a catalogue that no
    code path read.

    Safe to run as often as a host likes, and safe to run by hand: the
    grant is idempotent per (wallet, plan, period) through the same unique
    claim row the provider grants use, so a second run in the same period
    grants nothing.

    Who is entitled is a seam
    (``STAPEL_BILLING["PLAN_BUNDLE_ENTITLEMENTS"]``) because plan
    membership is host knowledge; the default resolver reads the local
    Subscription row. Returns the number of bundles actually granted.
    """
    from .services import current_period_key, grant_plan_bundle, plan_bundle_entitlements

    period = period_key or current_period_key()
    granted = 0
    seen = 0
    for row in plan_bundle_entitlements():
        seen += 1
        try:
            txn = grant_plan_bundle(
                user_id=row.get("user_id"),
                plan_slug=row["plan"],
                period_key=row.get("period_key") or period,
                credits=row.get("credits"),
                expires_at=row.get("expires_at"),
            )
        except Exception:
            # One wallet's failure is not the month's failure. The sweep is
            # re-runnable and idempotent, so the honest response to a bad
            # row is to log it and keep granting.
            logger.exception(
                "grant_plan_bundles: could not grant %s to %s",
                row.get("plan"),
                row.get("user_id"),
            )
            continue
        if txn is not None:
            granted += 1
    logger.info(
        "grant_plan_bundles: granted %s bundle(s) of %s entitled wallet(s) "
        "for period %s",
        granted,
        seen,
        period,
    )
    return granted


def get_billing_beat_schedule() -> dict:
    """Add to ``CELERY_BEAT_SCHEDULE`` in your Django settings.

    Hourly expiry rather than daily: the credits die at a moment the
    customer was told (their period end), and a daily sweep would let them
    stay spendable for up to a day past it. The reconciliation is daily —
    it is an audit, not a mechanism.
    """
    try:
        from celery.schedules import crontab
    except ImportError as exc:  # pragma: no cover - celery is present in most envs
        raise ImportError(
            "get_billing_beat_schedule() builds celery entries and needs "
            "celery installed. Without celery, schedule the plain callables "
            "instead: stapel_billing.tasks.expire_credit_lots, "
            "stapel_billing.tasks.expire_holds and "
            "stapel_billing.tasks.reconcile_wallets are all invocable from "
            "cron."
        ) from exc

    return {
        "billing-expire-credit-lots": {
            "task": EXPIRE_LOTS_TASK_NAME,
            "schedule": crontab(minute=5),          # every hour at :05
        },
        "billing-expire-holds": {
            "task": EXPIRE_HOLDS_TASK_NAME,
            "schedule": crontab(minute=25),         # every hour at :25
        },
        "billing-reconcile-wallets": {
            "task": RECONCILE_TASK_NAME,
            "schedule": crontab(hour=4, minute=40),  # daily at 04:40 UTC
        },
        # Daily rather than monthly: the grant is idempotent per period, so
        # running it every day costs one claim query per entitled wallet and
        # buys the thing a monthly cron cannot — a wallet created on the
        # 14th gets the period's bundle on the 14th, not next month.
        "billing-grant-plan-bundles": {
            "task": GRANT_PLAN_BUNDLES_TASK_NAME,
            "schedule": crontab(hour=5, minute=10),  # daily at 05:10 UTC
        },
    }


try:  # pragma: no cover — exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    expire_credit_lots = shared_task(name=EXPIRE_LOTS_TASK_NAME)(expire_credit_lots)
    expire_holds = shared_task(name=EXPIRE_HOLDS_TASK_NAME)(expire_holds)
    reconcile_wallets = shared_task(name=RECONCILE_TASK_NAME)(reconcile_wallets)
    grant_plan_bundles = shared_task(name=GRANT_PLAN_BUNDLES_TASK_NAME)(
        grant_plan_bundles
    )


__all__ = [
    "EXPIRE_HOLDS_TASK_NAME",
    "EXPIRE_LOTS_TASK_NAME",
    "GRANT_PLAN_BUNDLES_TASK_NAME",
    "RECONCILE_TASK_NAME",
    "expire_credit_lots",
    "expire_holds",
    "get_billing_beat_schedule",
    "grant_plan_bundles",
    "reconcile_wallets",
]
