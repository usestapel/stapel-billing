"""Debts, parked billing periods, and an index the charge path was missing.

Three additions, no removals and no data moved — expand-only in the strict
sense: every column is new, every table is new, and an 0.10.0 writer that
knows nothing about them keeps working unchanged.

* ``billing_credit_debt`` — credits a wallet owes. A balance counts credits
  that exist, and credits that were already spent do not come back into
  existence because a charge failed, so a shortfall cannot be expressed by
  driving the balance negative without making every lot, every expiry and
  every ``balance_after`` in the ledger untrue. The shortfall gets its own
  row instead. Two writers create them: a partial debit (the consumer
  served work the wallet could not cover) and a clawback (a refund landed
  after the granted credits were spent).

* ``billing_pending_subscription_period`` — a billing period that arrived
  before the subscription it belongs to. Stripe does not promise event
  order, and the handler used to drop such a payload on the floor; it is
  the ONLY payload carrying the period, so the first bundle stayed undated
  and never expired.

* ``billing_transaction.idempotency_key`` + ``billing_txn_wallet_idem_idx``
  — the retry key, mirrored out of the JSON blob into an indexed column.
  Every debit and credit runs a duplicate lookup before it moves a credit,
  and until now the only spelling of that key was
  ``metadata->>'idempotency_key'``: an unindexed scan over the wallet's
  entire, ever-growing transaction history, on the charge path.

  A double-write, NOT a replacement. Rows written before 0.11.0 carry the
  key only in ``metadata``, so the lookup still falls back to the JSON
  query (``STAPEL_BILLING["LEGACY_IDEMPOTENCY_JSON_LOOKUP"]``, default on).
  A host that backfills the column can turn the fallback off; turning it
  off before backfilling makes every old retry key a fresh charge.

Reverse drops the two tables and the column. It cannot restore an
outstanding debt into a model that has nowhere to put one, so a downgrade
with open debts forgives them — which is the safe direction, and the only
one available.
"""

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0004_erasure_pseudonyms'),
    ]

    operations = [
        migrations.CreateModel(
            name='CreditDebt',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('credits_initial', models.IntegerField(help_text='Credits this debt was opened for.')),
                ('credits_outstanding', models.IntegerField(help_text='Credits still owed. 0 once settled_at is set.')),
                ('reason', models.CharField(choices=[('partial_debit', 'Partial Debit'), ('clawback', 'Clawback')], help_text='Why the wallet owes this (see DebtReason).', max_length=16)),
                ('type', models.CharField(choices=[('credit_purchase', 'Credit Purchase'), ('transcription_charge', 'Transcription Charge'), ('ai_charge', 'AI Charge'), ('subscription_bonus', 'Subscription Bonus'), ('refund', 'Refund'), ('adjustment', 'Manual Adjustment'), ('expiration', 'Credit Expiration')], help_text='Transaction type the settlement is billed under — the type the uncovered charge would have carried.', max_length=32)),
                ('description', models.CharField(blank=True, max_length=255, null=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('idempotency_key', models.CharField(blank=True, default='', help_text="The debiting caller's retry key, when it supplied one.", max_length=255)),
                ('settled_at', models.DateTimeField(blank=True, help_text='When the last outstanding credit was collected. NULL = open.', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'billing_credit_debt',
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.CreateModel(
            name='PendingSubscriptionPeriod',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('stripe_subscription_id', models.CharField(max_length=255, unique=True)),
                ('stripe_customer_id', models.CharField(blank=True, default='', max_length=255)),
                ('status', models.CharField(blank=True, default='', help_text='Provider-side status string, applied when the row lands.', max_length=16)),
                ('current_period_start', models.DateTimeField(blank=True, null=True)),
                ('current_period_end', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'billing_pending_subscription_period',
            },
        ),
        migrations.AddField(
            model_name='transaction',
            name='idempotency_key',
            field=models.CharField(blank=True, default='', help_text="The caller's retry key, mirrored out of metadata into an indexed column. Empty when the caller supplied none. The duplicate lookup in services.credit/debit reads this column; metadata['idempotency_key'] stays the compatible spelling for rows written before 0.11.0.", max_length=255),
        ),
        migrations.AddIndex(
            model_name='transaction',
            index=models.Index(fields=['wallet', 'idempotency_key'], name='billing_txn_wallet_idem_idx'),
        ),
        migrations.AddField(
            model_name='creditdebt',
            name='transaction',
            field=models.ForeignKey(blank=True, help_text='The ledger row that recorded the uncovered part.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='debts', to='billing.transaction'),
        ),
        migrations.AddField(
            model_name='creditdebt',
            name='wallet',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='debts', to='billing.wallet'),
        ),
        migrations.AddIndex(
            model_name='pendingsubscriptionperiod',
            index=models.Index(fields=['stripe_customer_id'], name='billing_pen_stripe__274417_idx'),
        ),
        migrations.AddIndex(
            model_name='creditdebt',
            index=models.Index(fields=['wallet', 'settled_at', 'created_at'], name='billing_cre_wallet__ffd708_idx'),
        ),
        migrations.AddIndex(
            model_name='creditdebt',
            index=models.Index(fields=['wallet', 'idempotency_key'], name='billing_cre_wallet__3b489d_idx'),
        ),
    ]
