"""Provider ids at the length the provider actually sends them.

Every fixture in this suite used to say ``sub_1``, ``cus_1``, ``evt_pkg_1``
— ids no Stripe deployment has ever produced. A real one is a prefix plus
roughly 24 opaque characters (``sub_1PqRsTuVwXyZ0123456789ab``), and Stripe
documents its ids as opaque strings of up to 255.

That difference is not cosmetic. A column narrow enough to reject a real id
— or a real status word — passes a whole test suite built on six-character
fakes, and fails on the first live webhook. This module exists so that
cannot happen again: fixtures ask for an id here, and what they get is
full length.

``sid`` is deterministic (a digest of prefix + seed), so a fixture and the
assertion that reads it back agree without passing values around, and a
failing run prints the same id twice.
"""

import hashlib

#: Stripe's ids are base62-ish. The exact alphabet does not matter; the
#: length and the prefix do.
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

#: Characters after the ``<prefix>_``. Stripe's live ids run 24; the
#: documented ceiling is 255 total, which is what the columns are.
BODY_LENGTH = 24

#: The longest status word Stripe sends for a subscription — 18 characters,
#: two more than the ``varchar(16)`` that used to hold it. The reason this
#: file exists.
LONGEST_PROVIDER_STATUS = "incomplete_expired"


def sid(prefix: str, seed: str = "") -> str:
    """A realistic provider id: ``<prefix>_1`` + 23 opaque characters.

    Deterministic in ``(prefix, seed)`` so fixtures and assertions can name
    the same id independently.
    """
    digest = hashlib.sha256(f"{prefix}:{seed}".encode()).digest()
    body = "".join(_ALPHABET[byte % len(_ALPHABET)] for byte in digest)
    return f"{prefix}_1{body[: BODY_LENGTH - 1]}"


def assert_realistic(value: str, prefix: str) -> None:
    """Guard a fixture: this id is the shape and length a real one has."""
    assert value.startswith(f"{prefix}_"), value
    assert len(value) - len(prefix) - 1 >= BODY_LENGTH, (
        f"{value!r} is {len(value)} characters — a real {prefix}_ id is "
        f"{len(prefix) + 1 + BODY_LENGTH}. Short fake ids are how a column "
        f"too narrow for a real one reached production."
    )
