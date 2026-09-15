"""Internal accounts: metered, but not charged.

THE PROBLEM THIS CLOSES. A deployment's own people have to be able to use
the product the way a customer does — upload the file, watch the pipeline
run, read the result — and the only way to do that used to be to pay, or to
build a privilege that steps around the meter entirely. Both are wrong. Pay
and the staff account is indistinguishable from a customer in every revenue
report it lands in; step around the meter and the run leaves no trace at
all, so "how much does our own testing cost us" and "did the charge path
even fire" become unanswerable.

THE POLICY. An internal account is **metered but not charged**: every
operation that would have spent credits still writes its ledger row, with
its type, its description and its metadata intact, and with
``credits_delta = 0``. The row carries ``metadata["internal_meter_only"] =
True`` and ``metadata["waived_credits"] = <what it would have cost>``.

Why that and not "no debit at all":

* **The charge path stays under test.** A staff run exercises the same
  ``debit`` / ``hold`` / ``capture`` code a customer's run does. A branch
  that returns before the ledger is a branch where nothing is proved.
* **Usage stays visible.** "What did internal testing consume this month"
  is ``sum(waived_credits)`` over the marked rows. With no row it is not a
  harder query, it is an impossible one.
* **Revenue reporting is unaffected.** Every report that sums
  ``credits_delta`` — balances, burn, the lots — sees a zero and is
  unchanged; reports that COUNT rows see the run, which is what they are
  for. Nothing has to learn a new row shape to stay correct, and anything
  that wants to exclude internal usage filters on the marker.
* **No debt is invented.** Without this, a staff wallet at zero took the
  ``allow_partial`` branch and accrued a :class:`~stapel_billing.models.
  CreditDebt` for work nobody intends to collect on — and then swallowed
  the next grant settling a debt that should never have existed.

THE SWITCH IS OFF BY DEFAULT. ``STAPEL_BILLING["INTERNAL_ACCOUNT_POLICY"]``
is ``"charge"`` unless a deployment says otherwise, because "staff" is not
a synonym for "ours" everywhere: in a host whose operators are also
customers, turning this on silently would stop billing real usage. A
deployment opts in with ``"meter_only"``.

WHO IS INTERNAL is the host's question, not the library's.
``INTERNAL_ACCOUNT_RESOLVER`` is a dotted path to ``callable(user) -> bool``
and defaults to :func:`staff_is_internal` (``is_staff or is_superuser``).
Like ``PAYMENT_PROVIDER`` it does not resolve from the environment: a
same-named env var in a shared pod must not be able to decide who gets
served for free.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Charge internal accounts like everybody else. The default.
POLICY_CHARGE = "charge"

#: Write the ledger row at zero cost — see the module docstring.
POLICY_METER_ONLY = "meter_only"

POLICIES = (POLICY_CHARGE, POLICY_METER_ONLY)

#: Marker written onto every row a meter-only policy zeroed. Public because
#: it is what a report filters on.
MARKER = "internal_meter_only"

#: Companion key: what the operation WOULD have cost. The number that makes
#: "what does our own testing consume" answerable.
WAIVED = "waived_credits"


def staff_is_internal(user) -> bool:
    """Default resolver: Django staff and superusers are internal.

    Deliberately the same test the rest of a Django deployment already uses
    for "one of ours", so a host that has not thought about the question
    gets the answer it already believes.
    """
    return bool(
        getattr(user, "is_authenticated", True)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def policy() -> str:
    """The configured policy, normalised. Anything unrecognised is ``charge``.

    An unrecognised spelling means SOMEBODY meant something, and the safe
    reading of "I could not understand you" on a billing switch is "keep
    charging" — a typo must never turn the meter off. It is logged rather
    than raised because a charge path is not the place to discover a
    settings typo by crashing.
    """
    from .conf import billing_settings

    raw = billing_settings.INTERNAL_ACCOUNT_POLICY
    value = str(raw or "").strip().lower()
    if value in POLICIES:
        return value
    if value:
        logger.error(
            "STAPEL_BILLING['INTERNAL_ACCOUNT_POLICY']=%r is not one of %s — "
            "internal accounts will be CHARGED.",
            raw,
            ", ".join(POLICIES),
        )
    return POLICY_CHARGE


def is_internal(user) -> bool:
    """Is *user* one of the deployment's own, per the configured resolver.

    A resolver that raises answers False: the failure mode of "we could not
    tell" has to be the one that charges, not the one that gives the
    product away.
    """
    from .conf import billing_settings

    if user is None:
        return False
    resolver = billing_settings.INTERNAL_ACCOUNT_RESOLVER
    try:
        return bool(resolver(user))
    except Exception:  # pragma: no cover — a host resolver misbehaving
        logger.exception(
            "INTERNAL_ACCOUNT_RESOLVER raised for user %s — treating the "
            "account as external (it will be charged).",
            getattr(user, "id", "?"),
        )
        return False


def meter_only(user) -> bool:
    """Should this user's spend be recorded at zero cost.

    The one question the service layer asks. Both halves must be true: the
    deployment has opted in, and this account is internal.
    """
    return policy() == POLICY_METER_ONLY and is_internal(user)


def mark(metadata: dict | None, *, credits: int) -> dict:
    """Stamp *metadata* as a waived, metered operation costing *credits*."""
    stamped = dict(metadata or {})
    stamped[MARKER] = True
    stamped[WAIVED] = int(credits)
    return stamped


def was_waived(metadata: dict | None) -> bool:
    """Did this row come out of the meter-only branch."""
    return bool((metadata or {}).get(MARKER))


__all__ = [
    "MARKER",
    "POLICIES",
    "POLICY_CHARGE",
    "POLICY_METER_ONLY",
    "WAIVED",
    "is_internal",
    "mark",
    "meter_only",
    "policy",
    "staff_is_internal",
    "was_waived",
]
