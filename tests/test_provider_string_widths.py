"""No column this provider fills may be narrower than the provider.

The incident: ``PendingSubscriptionPeriod.status`` was ``varchar(16)`` —
copied from ``Subscription.status``'s width before 0.13.0 widened that one
to 32 to hold ``incomplete_expired`` (18 chars). The copy was missed. The
column takes the provider's RAW status word, so on Postgres the stash
INSERT raised ``StringDataRightTruncation``, the webhook answered 500,
Stripe retried the same event for ~3 days and every attempt died the same
way. A paying customer's subscription could never activate.

Nothing in the suite could see it: SQLite ignores varchar length, and every
fixture used six-character fake ids like ``sub_1``.

So the rule is enforced here, on the models themselves, rather than
remembered:

  * a field whose NAME says it holds a provider-issued identifier
    (``stripe_*_id``, ``*external_id``, ``provider_*_id``, ``*event_id``)
    or an idempotency key must be at least 255 characters — the length
    Stripe documents its ids to; and
  * it must be a :class:`~stapel_billing.models.ProviderStringField`, so
    the width is decided in one place and a new column cannot quietly pick
    its own; and
  * every ``ProviderStringField`` is at least 255 — including the ones no
    naming convention would catch, like the raw ``status`` word above.
"""

import re

import pytest
from django.apps import apps
from django.db import models

from stapel_billing.models import PROVIDER_STRING_MAX_LENGTH, ProviderStringField

#: Names that say "the provider issued this value, we only echo it".
#: Derived from the columns this module actually has: stripe_subscription_id,
#: stripe_customer_id, stripe_event_id, ProviderGrant.external_id.
PROVIDER_ID_NAME = re.compile(
    r"^(stripe_[a-z0-9_]*_id|[a-z0-9_]*external_id|provider_[a-z0-9_]*_id"
    r"|[a-z0-9_]*event_id)$"
)

#: Caller-supplied retry keys. Not provider-issued, but opaque to us in the
#: same way — a host picks the string and we store it whole.
OPAQUE_KEY_NAME = re.compile(r"^(idempotency_key|[a-z0-9_]*_idempotency_key)$")


def _billing_char_fields():
    for model in apps.get_app_config("billing").get_models():
        for field in model._meta.get_fields():
            if isinstance(field, models.CharField):
                yield model, field


def _is_provider_owned(field) -> bool:
    return bool(
        PROVIDER_ID_NAME.match(field.name)
        or OPAQUE_KEY_NAME.match(field.name)
        or isinstance(field, ProviderStringField)
    )


def _params(predicate):
    return [
        pytest.param(model, field, id=f"{model.__name__}.{field.name}")
        for model, field in _billing_char_fields()
        if predicate(field)
    ]


PROVIDER_OWNED = _params(_is_provider_owned)
PROVIDER_ISSUED_IDS = _params(lambda field: bool(PROVIDER_ID_NAME.match(field.name)))


def test_the_sweep_actually_found_the_columns():
    """A rule that matches nothing passes every time (gates that prove nothing)."""
    names = {field.name for _model, field in _billing_char_fields()}
    assert {"stripe_subscription_id", "stripe_event_id", "external_id"} <= names
    assert len(PROVIDER_OWNED) >= 8
    assert len(PROVIDER_ISSUED_IDS) >= 5


@pytest.mark.parametrize("model,field", PROVIDER_OWNED)
def test_provider_owned_columns_are_at_least_255(model, field):
    assert field.max_length >= PROVIDER_STRING_MAX_LENGTH, (
        f"{model.__name__}.{field.name} is varchar({field.max_length}). It "
        f"holds a value the payment provider chooses, and a column narrower "
        f"than the provider is a 500 on the webhook that carries it — see "
        f"stapel_billing.models.ProviderStringField."
    )


@pytest.mark.parametrize("model,field", PROVIDER_ISSUED_IDS)
def test_provider_issued_ids_declare_themselves_as_one(model, field):
    """The width lives in one class, not in each field's judgement call."""
    assert isinstance(field, ProviderStringField), (
        f"{model.__name__}.{field.name} looks like a provider-issued id but "
        f"is a plain {type(field).__name__}. Use ProviderStringField: it is "
        f"the only place this library decides how wide the provider's "
        f"strings are."
    )


#: The two columns that mirror ONE vocabulary: the subscription lifecycle
#: status. `Subscription.status` holds the mapped local word,
#: `PendingSubscriptionPeriod.status` holds the provider's raw one. Two
#: columns for one vocabulary is how this broke — the vocabulary grew
#: `incomplete_expired`, one column was widened for it (0.13.0) and the
#: other was not.
STATUS_MIRRORS = [
    ("Subscription", "status"),
    ("PendingSubscriptionPeriod", "status"),
]


def _status_vocabulary() -> set[str]:
    """Every word either column can be asked to hold — derived, not typed.

    Both halves: the provider's raw strings (the keys of the translation
    table) and the local names they map to. Hard-coding 18, or 32, is the
    same mistake one layer further out.
    """
    from stapel_billing.models import SubscriptionStatus
    from stapel_billing.services import _STRIPE_STATUS_MAP

    return {str(raw) for raw in _STRIPE_STATUS_MAP} | {
        str(value) for value in SubscriptionStatus.values
    }


@pytest.mark.parametrize("model_name,field_name", STATUS_MIRRORS)
def test_status_columns_hold_the_whole_vocabulary(model_name, field_name):
    """Derived from the enum: a longer member must widen both columns."""
    vocabulary = _status_vocabulary()
    longest = max(vocabulary, key=len)
    field = apps.get_model("billing", model_name)._meta.get_field(field_name)
    assert field.max_length >= len(longest), (
        f"{model_name}.{field_name} is varchar({field.max_length}) but the "
        f"status vocabulary contains {longest!r} ({len(longest)} chars). "
        f"Both columns mirror the same vocabulary; widening one and not the "
        f"other is exactly the defect this test exists for."
    )


def test_the_column_that_broke_activation_is_covered():
    """A direct pin on the incident, not only on the rule.

    `PendingSubscriptionPeriod.status` stores the provider's raw word; the
    longest Stripe sends is `incomplete_expired`.
    """
    from stapel_billing.models import PendingSubscriptionPeriod

    field = PendingSubscriptionPeriod._meta.get_field("status")
    assert isinstance(field, ProviderStringField)
    assert field.max_length >= len("incomplete_expired")
    assert field.max_length == PROVIDER_STRING_MAX_LENGTH
    assert "incomplete_expired" in _status_vocabulary()


def test_provider_string_fields_deconstruct_as_plain_charfields():
    """Migrations stay portable: no host history imports this class."""
    field = ProviderStringField(null=True, blank=True)
    _name, path, _args, kwargs = field.deconstruct()
    assert path == "django.db.models.CharField"
    assert kwargs["max_length"] == PROVIDER_STRING_MAX_LENGTH


def test_the_models_and_the_migrations_agree():
    """A width fixed in models.py and not in a migration fixes nothing.

    The suite builds its tables straight from the models
    (``MIGRATION_MODULES = {"billing": None}``), so the migration a
    deployment actually runs is otherwise compared to nothing until it
    reaches a production database.

    A subprocess for the same reason ``test_contract`` uses one: this
    process is configured with migrations disabled and its test database
    was built without them, so the check has to run in an interpreter that
    was never told to skip them.
    """
    import subprocess
    import sys
    from pathlib import Path

    script = (
        "from stapel_billing._codegen_settings import settings_kwargs\n"
        "from django.conf import settings\n"
        "kwargs = settings_kwargs()\n"
        "kwargs.pop('MIGRATION_MODULES', None)\n"
        "settings.configure(**kwargs)\n"
        "import django; django.setup()\n"
        "from django.core.management import call_command\n"
        "call_command('makemigrations', 'billing', '--check', '--dry-run')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent.parent.parent,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "the models and migrations/ have drifted — run makemigrations and "
        f"commit the result:\n{proc.stdout}\n{proc.stderr}"
    )
