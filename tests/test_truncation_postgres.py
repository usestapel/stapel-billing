"""The truncation, against a real server (the CI `postgres` job).

SQLite does not enforce ``varchar`` length: it stored an 18-character
provider status in a ``varchar(16)`` column happily, which is exactly why
the whole suite was green while a client host answered 500 to every
subscription webhook and a paying customer's subscription never activated.

These reproduce it on Postgres, where the INSERT really is refused
(``StringDataRightTruncation``). They skip without a server; the CI job
points ``STAPEL_TEST_DATABASE_URL`` at one, and the tests assert the
variable was honoured so the job cannot go green by quietly running on
SQLite.

Run locally against a server with::

    STAPEL_TEST_DATABASE_URL=postgres://postgres:stapel@127.0.0.1:5432/billing_test \\
        pytest tests/test_truncation_postgres.py
"""

import json
import os

import pytest
from django.db import connection

from stapel_billing.conf import billing_settings
from stapel_billing.models import (
    PendingSubscriptionPeriod,
    StripeWebhookEvent,
    Subscription,
)

from .stripe_ids import LONGEST_PROVIDER_STATUS, sid
from .test_webhooks import PROVIDER_PATH, WEBHOOK_URL

pytestmark = pytest.mark.skipif(
    not os.environ.get("STAPEL_TEST_DATABASE_URL"),
    reason="needs a real Postgres: set STAPEL_TEST_DATABASE_URL",
)


@pytest.fixture(autouse=True)
def _real_server_and_json_provider(settings):
    # The job asked for Postgres; a run that silently fell back to SQLite
    # proves nothing about column widths, so it fails instead.
    assert connection.vendor == "postgresql", (
        "STAPEL_TEST_DATABASE_URL is set but the suite is on "
        f"{connection.vendor} — this job cannot pass on SQLite."
    )
    settings.STAPEL_BILLING = {"PAYMENT_PROVIDER": PROVIDER_PATH}
    yield
    billing_settings.reload()


@pytest.mark.django_db
def test_the_stash_insert_accepts_the_longest_provider_status():
    """`varchar(16)` + `incomplete_expired` = the production 500."""
    PendingSubscriptionPeriod.objects.create(
        stripe_subscription_id=sid("sub", "pg"),
        stripe_customer_id=sid("cus", "pg"),
        status=LONGEST_PROVIDER_STATUS,
    )
    row = PendingSubscriptionPeriod.objects.get()
    assert row.status == LONGEST_PROVIDER_STATUS
    assert row.stripe_subscription_id == sid("sub", "pg")


@pytest.mark.django_db
def test_the_webhook_parks_a_real_payload_instead_of_answering_500(client):
    event = {
        "id": sid("evt", "pg"),
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": sid("sub", "pg2"),
                "customer": sid("cus", "pg2"),
                "status": LONGEST_PROVIDER_STATUS,
                "current_period_end": 4102444800,
            }
        },
    }
    resp = client.post(
        WEBHOOK_URL,
        data=json.dumps(event),
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="good",
    )
    assert resp.status_code == 200, resp.content
    assert PendingSubscriptionPeriod.objects.get().status == LONGEST_PROVIDER_STATUS
    log = StripeWebhookEvent.objects.get(stripe_event_id=sid("evt", "pg"))
    assert log.processed_at is not None
    assert log.error == ""


@pytest.mark.django_db
def test_every_provider_column_is_255_in_the_database_itself():
    """The model says 255; this asks the database what it actually built."""
    checked = {
        (PendingSubscriptionPeriod, "status"),
        (PendingSubscriptionPeriod, "stripe_subscription_id"),
        (PendingSubscriptionPeriod, "stripe_customer_id"),
        (Subscription, "stripe_subscription_id"),
        (Subscription, "stripe_customer_id"),
        (StripeWebhookEvent, "stripe_event_id"),
        (StripeWebhookEvent, "event_type"),
    }
    with connection.cursor() as cursor:
        for model, name in sorted(checked, key=lambda pair: (pair[0].__name__, pair[1])):
            cursor.execute(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = %s",
                [model._meta.db_table, name],
            )
            row = cursor.fetchone()
            assert row is not None, f"{model._meta.db_table}.{name} is missing"
            assert row[0] >= 255, f"{model._meta.db_table}.{name} is varchar({row[0]})"
