"""Database-enforced grant identity for provider objects.

Cleanup path for the two Subscription constraints. They are partial
(NULL and "" are exempt), so only rows that really share a provider id
collide. Find them before applying::

    SELECT stripe_customer_id, COUNT(*) FROM billing_subscription
     WHERE stripe_customer_id IS NOT NULL AND stripe_customer_id <> ''
     GROUP BY stripe_customer_id HAVING COUNT(*) > 1;

    SELECT stripe_subscription_id, COUNT(*) FROM billing_subscription
     WHERE stripe_subscription_id IS NOT NULL AND stripe_subscription_id <> ''
     GROUP BY stripe_subscription_id HAVING COUNT(*) > 1;

A duplicate means two local subscriptions claim one provider object and
webhook routing is already ambiguous between them; resolve it by clearing
the id on every row but the one the provider actually bills (set it to
'' — the lifecycle handlers then ignore that row) and re-running the
migration. The migration fails loudly on unresolved duplicates rather
than picking a winner.

``backfill_provider_grants`` seeds the new claim table from the ledger so
that a redelivery of an invoice/session granted BEFORE this migration is
still recognised as already granted.
"""

import uuid
from django.conf import settings
from django.db import migrations, models


def backfill_provider_grants(apps, schema_editor):
    Transaction = apps.get_model("billing", "Transaction")
    ProviderGrant = apps.get_model("billing", "ProviderGrant")
    seen = set()
    rows = []
    for scope, field in (
        ("invoice", "stripe_invoice_id"),
        ("checkout_session", "stripe_session_id"),
    ):
        for metadata in Transaction.objects.values_list("metadata", flat=True):
            external_id = (metadata or {}).get(field)
            if not external_id or (scope, external_id) in seen:
                continue
            seen.add((scope, external_id))
            rows.append(
                ProviderGrant(provider="stripe", scope=scope, external_id=str(external_id))
            )
    ProviderGrant.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ProviderGrant',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('provider', models.CharField(default='stripe', max_length=32)),
                ('scope', models.CharField(max_length=32)),
                ('external_id', models.CharField(max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'billing_provider_grant',
            },
        ),
        migrations.AddConstraint(
            model_name='subscription',
            constraint=models.UniqueConstraint(condition=models.Q(models.Q(('stripe_customer_id', None), _negated=True), models.Q(('stripe_customer_id', ''), _negated=True)), fields=('stripe_customer_id',), name='billing_subscription_unique_stripe_customer'),
        ),
        migrations.AddConstraint(
            model_name='subscription',
            constraint=models.UniqueConstraint(condition=models.Q(models.Q(('stripe_subscription_id', None), _negated=True), models.Q(('stripe_subscription_id', ''), _negated=True)), fields=('stripe_subscription_id',), name='billing_subscription_unique_stripe_subscription'),
        ),
        migrations.AddConstraint(
            model_name='providergrant',
            constraint=models.UniqueConstraint(fields=('provider', 'scope', 'external_id'), name='billing_provider_grant_unique_object'),
        ),
        migrations.RunPython(
            backfill_provider_grants, migrations.RunPython.noop, elidable=True
        ),
    ]
