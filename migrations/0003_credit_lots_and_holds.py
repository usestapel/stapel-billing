"""Credit lots + reservations: a wallet stops being one integer.

``backfill_wallet_lots`` gives every wallet that holds credits exactly ONE
lot, ``source=adjustment, expires_at=NULL``, sized to the balance it already
had.

One lot, and an adjustment one, because the history cannot be replayed into
lots honestly. The ledger records deltas against a single scalar: it says a
wallet was credited 3000 in February and debited 40 in March, but not which
credits the 40 came out of — the question did not exist yet. Reconstructing
lots from it would mean inventing a consumption order that nobody ever
applied, and stamping expiries on credits that were sold as non-expiring
(every subscription bonus in the ledger predates the promise that plan
credits expire). ``adjustment`` is the source that says exactly that: these
credits are the opening balance of the new model, not a purchase and not a
grant we can date.

The consequence is deliberate and worth stating: **pre-migration credits
never expire**, and they are spent last (a NULL expiry sorts last in the
consumption walk), so a customer's existing balance survives untouched
while everything granted from now on carries its real deadline.

Deletion-driven: nothing is dropped, because nothing is replaced — the
``Wallet.balance`` column stays, demoted from truth to maintained cache
(``services._sync_balance`` recomputes it from the lots inside the same row
lock as every mutation).

Reverse deletes the lots and the holds. It cannot restore a hold's
reservation into the old model — the old model had no reservations — so a
downgrade must be run when no hold is open; ``Wallet.balance`` is left as
it stands, which is the spendable number both models agree on.
"""

import django.db.models.deletion
import uuid
from django.db import migrations, models


#: Source value for the opening-balance lot. Spelled literally rather than
#: imported from models: a migration must keep meaning what it meant when it
#: ran, and an enum the code renames later would silently rewrite history.
_OPENING_BALANCE_SOURCE = 'adjustment'


def backfill_wallet_lots(apps, schema_editor):
    """One opening-balance lot per wallet with credits. See module docstring."""
    Wallet = apps.get_model('billing', 'Wallet')
    CreditLot = apps.get_model('billing', 'CreditLot')
    lots = [
        CreditLot(
            id=uuid.uuid4(),
            wallet_id=wallet_id,
            source=_OPENING_BALANCE_SOURCE,
            credits_initial=balance,
            credits_remaining=balance,
            expires_at=None,
            granting_transaction=None,
        )
        for wallet_id, balance in Wallet.objects.filter(balance__gt=0).values_list(
            'id', 'balance'
        )
    ]
    CreditLot.objects.bulk_create(lots, batch_size=1000)


def drop_wallet_lots(apps, schema_editor):
    """Reverse: the lot layer goes away, the cached balance stays."""
    apps.get_model('billing', 'HoldAllocation').objects.all().delete()
    apps.get_model('billing', 'CreditHold').objects.all().delete()
    apps.get_model('billing', 'CreditLot').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0002_providergrant_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='transaction',
            name='type',
            field=models.CharField(choices=[('credit_purchase', 'Credit Purchase'), ('transcription_charge', 'Transcription Charge'), ('ai_charge', 'AI Charge'), ('subscription_bonus', 'Subscription Bonus'), ('refund', 'Refund'), ('adjustment', 'Manual Adjustment'), ('expiration', 'Credit Expiration')], max_length=32),
        ),
        migrations.AlterField(
            model_name='wallet',
            name='balance',
            field=models.IntegerField(default=0, help_text='Integer credits — never fractional. Maintained cache of SUM(credit_lot.credits_remaining) over live lots; write it only through stapel_billing.services.'),
        ),
        migrations.CreateModel(
            name='CreditHold',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('credits', models.IntegerField(help_text='Credits currently reserved by this hold.')),
                ('type', models.CharField(choices=[('credit_purchase', 'Credit Purchase'), ('transcription_charge', 'Transcription Charge'), ('ai_charge', 'AI Charge'), ('subscription_bonus', 'Subscription Bonus'), ('refund', 'Refund'), ('adjustment', 'Manual Adjustment'), ('expiration', 'Credit Expiration')], help_text='Transaction type the capture will be billed under.', max_length=32)),
                ('description', models.CharField(blank=True, max_length=255, null=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('idempotency_key', models.CharField(help_text='Caller-supplied key; unique per wallet, so a retried hold reserves once.', max_length=255)),
                ('status', models.CharField(choices=[('held', 'Held'), ('captured', 'Captured'), ('released', 'Released'), ('expired', 'Expired')], default='held', max_length=16)),
                ('expires_at', models.DateTimeField(blank=True, help_text='When expire_holds releases this hold. NULL = never swept.', null=True)),
                ('resolved_at', models.DateTimeField(blank=True, help_text='When the hold stopped being held (captured, released or expired).', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='holds', to='billing.wallet')),
            ],
            options={
                'db_table': 'billing_credit_hold',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='CreditLot',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('source', models.CharField(choices=[('purchase', 'Purchase'), ('subscription', 'Subscription'), ('grant', 'Grant'), ('adjustment', 'Adjustment'), ('hold_release', 'Hold Release')], max_length=16)),
                ('credits_initial', models.IntegerField(help_text='Credits this lot was created with.')),
                ('credits_remaining', models.IntegerField(help_text='Credits still unspent and unreserved.')),
                ('expires_at', models.DateTimeField(blank=True, help_text='When these credits die. NULL = never (the paid-cash case).', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('granting_transaction', models.ForeignKey(blank=True, help_text='The ledger row that brought this lot into existence.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='granted_lots', to='billing.transaction')),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='lots', to='billing.wallet')),
            ],
            options={
                'db_table': 'billing_credit_lot',
                'ordering': ['created_at'],
            },
        ),
        migrations.AddField(
            model_name='transaction',
            name='lot',
            field=models.ForeignKey(blank=True, help_text="The single lot this row moved credits in or out of. NULL when the operation spanned several lots — the per-lot split is then in metadata['lots'].", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='transactions', to='billing.creditlot'),
        ),
        migrations.CreateModel(
            name='HoldAllocation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('credits', models.IntegerField(help_text='Credits this hold took from this lot.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('hold', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='allocations', to='billing.credithold')),
                ('lot', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='allocations', to='billing.creditlot')),
            ],
            options={
                'db_table': 'billing_hold_allocation',
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.AddIndex(
            model_name='credithold',
            index=models.Index(fields=['wallet', '-created_at'], name='billing_cre_wallet__9a6580_idx'),
        ),
        migrations.AddIndex(
            model_name='credithold',
            index=models.Index(fields=['status', 'expires_at'], name='billing_cre_status_9fca22_idx'),
        ),
        migrations.AddConstraint(
            model_name='credithold',
            constraint=models.UniqueConstraint(fields=('wallet', 'idempotency_key'), name='billing_credit_hold_unique_idempotency_key'),
        ),
        migrations.AddIndex(
            model_name='creditlot',
            index=models.Index(fields=['wallet', 'expires_at'], name='billing_cre_wallet__d9e77e_idx'),
        ),
        migrations.AddIndex(
            model_name='creditlot',
            index=models.Index(fields=['expires_at'], name='billing_cre_expires_2266e8_idx'),
        ),
        migrations.AddIndex(
            model_name='holdallocation',
            index=models.Index(fields=['hold'], name='billing_hol_hold_id_4b4341_idx'),
        ),
        migrations.RunPython(backfill_wallet_lots, drop_wallet_lots),
    ]
