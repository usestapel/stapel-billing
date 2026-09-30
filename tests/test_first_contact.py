"""A user billing has not met yet is met, not "not found".

Measured on a client fleet (2026-09-28..30): guests upload before their
browser has ever called billing, so the recordings pre-flight asked
billing.can_afford about accounts billing had no row for. Every answer was
``user_not_found``; the caller treated it as "affordable" and paged — 20
guests, 40 faults, 10 pages, and a real outage page lost in the noise.
"""
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from stapel_core.comm import call

pytestmark = pytest.mark.django_db

GUEST = {"is_anonymous": True, "auth_type": "anonymous"}


@pytest.fixture
def consumer_mode(settings):
    with override_settings(JWT_CREATE_USERS_FROM_TOKEN=True):
        yield


def test_a_guest_with_an_identity_is_materialised_and_answered(consumer_mode):
    uid = str(uuid.uuid4())

    answer = call("billing.can_afford", {"user_id": uid, "credits": 5, "identity": GUEST})

    assert answer["ok"] is True
    user = get_user_model().objects.get(pk=uid)
    assert user.is_anonymous is True
    assert user.email is None


def test_a_bare_id_never_creates_a_row(consumer_mode):
    uid = str(uuid.uuid4())

    answer = call("billing.can_afford", {"user_id": uid, "credits": 5})

    assert answer == {
        "ok": False, "affordable": False, "balance": None, "shortfall": None,
        "reason": "user_unknown",
    }
    assert not get_user_model().objects.filter(pk=uid).exists()


def test_a_deleted_account_is_inactive_not_unknown(consumer_mode, monkeypatch):
    uid = str(uuid.uuid4())
    monkeypatch.setattr(
        "stapel_core.django.jwt.tombstone.is_user_tombstoned", lambda u: str(u) == uid
    )

    answer = call("billing.can_afford", {"user_id": uid, "credits": 5, "identity": GUEST})

    assert answer["reason"] == "user_inactive"
    assert not get_user_model().objects.filter(pk=uid).exists()


def test_hold_and_debit_meet_the_user_too(consumer_mode):
    uid = str(uuid.uuid4())

    hold = call("billing.hold", {
        "user_id": uid, "credits": 1, "idempotency_key": "k1", "identity": GUEST,
    })
    debit = call("billing.debit", {
        "user_id": str(uuid.uuid4()), "credits": 1, "idempotency_key": "k2",
        "identity": GUEST,
    })

    # Met, and answered on the wallet: an empty one says so in its own words.
    assert hold["reason"] != "user_unknown"
    assert debit["reason"] != "user_unknown"


def test_with_token_mirroring_off_an_identity_creates_nothing(settings):
    with override_settings(JWT_CREATE_USERS_FROM_TOKEN=False):
        uid = str(uuid.uuid4())
        answer = call("billing.can_afford", {"user_id": uid, "credits": 5, "identity": GUEST})

    assert answer["reason"] == "user_unknown"
    assert not get_user_model().objects.filter(pk=uid).exists()


def test_an_identity_carries_no_privileges(consumer_mode):
    from stapel_core.comm.exceptions import SchemaValidationError

    with pytest.raises(SchemaValidationError):
        call("billing.can_afford", {
            "user_id": str(uuid.uuid4()), "credits": 5,
            "identity": {**GUEST, "is_staff": True},
        })
