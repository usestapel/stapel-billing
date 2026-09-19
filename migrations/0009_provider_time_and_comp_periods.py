"""Provider-time ordering, comp periods, and the column that could not hold a status (0.20.0).

Three things, all expand-only — every operation adds a column, a table or
widens one, so an old process keeps serving against the new schema while
the rollout runs.

1. `Subscription.last_provider_event_at` (+ `PendingSubscriptionPeriod.
   provider_event_at`, + `StripeWebhookEvent.ignored_stale`). Stripe does
   not promise delivery order, and on a client host it did not deliver
   one: `customer.subscription.created` (status `incomplete`) arrived
   AFTER `customer.subscription.updated` (status `active`), the older
   word was written on top of the newer, the plan stopped entitling and a
   paying customer was shown the paywall. Lifecycle state is applied in
   provider time from now on, and this column is that clock. NULL means
   "nothing applied yet — accept the next event", which is where every
   pre-0.20.0 row starts; the backfill below moves as many as it can off
   NULL using the webhook log this module already keeps.

2. `PendingSubscriptionPeriod.status` widens 16 -> 255. It stores the
   provider's RAW status word, and `incomplete_expired` is 18 characters:
   on Postgres the stash INSERT raised StringDataRightTruncation, the
   webhook answered 500 and Stripe retried the same event for ~3 days.
   `StripeWebhookEvent.event_type` widens 64 -> 255 for the same reason
   and not because it has ever truncated. On Postgres a varchar widening
   is binary-coercible, so neither table is rewritten; it takes a brief
   ACCESS EXCLUSIVE lock, and `event_type` being the leading column of
   the `(event_type, -received_at)` index means that index is rebuilt —
   the only part of this worth timing on a large webhook log. The
   `status` column carries no index or constraint at all, and the unique
   constraints on the two id columns are untouched (those were already
   255).

3. `CompPeriod` — subscription time an operator gives away, as its own
   row. The thing it replaces is editing `current_period_end`, which
   mirrors the provider and is overwritten by the next webhook.

Reversible: the reverse drops the additions and narrows the two columns
back, which will refuse to run if any row already holds a value longer
than the old width (as it should).
"""

import django.db.models.deletion
import uuid
from django.db import migrations, models


#: Event types whose payload describes the subscription lifecycle. The
#: backfill reads only these: a clock set too EARLY merely accepts an
#: event it could have ignored, while one set too late would drop a real
#: update on the floor.
_LIFECYCLE_EVENT_PREFIX = "customer.subscription."


def _backfill_provider_clock(apps, schema_editor):
    """Seed `last_provider_event_at` from the processed webhook log.

    A deployment that has been running carries the evidence already: the
    newest lifecycle event it processed per provider subscription id is,
    by definition, the provider time its row reflects. Rows with no such
    event stay NULL, which means "accept the next event" — the same
    behaviour as before this migration.
    """
    from datetime import datetime, timezone as dt_timezone

    Subscription = apps.get_model('billing', 'Subscription')
    StripeWebhookEvent = apps.get_model('billing', 'StripeWebhookEvent')

    newest: dict[str, datetime] = {}
    events = StripeWebhookEvent.objects.filter(
        event_type__startswith=_LIFECYCLE_EVENT_PREFIX, processed_at__isnull=False
    ).only('payload')
    for event in events.iterator(chunk_size=500):
        payload = event.payload or {}
        created = payload.get('created')
        obj = ((payload.get('data') or {}).get('object')) or {}
        sub_id = obj.get('id')
        if not sub_id or not isinstance(created, (int, float)):
            continue
        when = datetime.fromtimestamp(int(created), tz=dt_timezone.utc)
        if sub_id not in newest or when > newest[sub_id]:
            newest[sub_id] = when

    if not newest:
        return
    rows = []
    for sub in Subscription.objects.filter(
        stripe_subscription_id__in=list(newest)
    ).only('id', 'stripe_subscription_id', 'last_provider_event_at'):
        sub.last_provider_event_at = newest[sub.stripe_subscription_id]
        rows.append(sub)
    if rows:
        Subscription.objects.bulk_update(rows, ['last_provider_event_at'], batch_size=200)


def _forget_provider_clock(apps, schema_editor):
    """Reverse: the column is dropped straight after this, so clear it."""
    Subscription = apps.get_model('billing', 'Subscription')
    Subscription.objects.update(last_provider_event_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0008_alter_wallet_options'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='subscription',
            options={'permissions': [('extend_subscription', 'Can give comp subscription time')]},
        ),
        migrations.AddField(
            model_name='pendingsubscriptionperiod',
            name='provider_event_at',
            field=models.DateTimeField(blank=True, help_text='Provider-clock time of the event this stash was written from. A stash is overwritten only by a NEWER event, and applied only if it is newer than what the subscription already carries — out-of-order delivery must not park a stale period either.', null=True),
        ),
        migrations.AddField(
            model_name='stripewebhookevent',
            name='ignored_stale',
            field=models.BooleanField(default=False, help_text="The event was accepted and acknowledged, but its lifecycle payload was OLDER than what had already been applied, so it was not applied. Processed and ignored is a third outcome: without it, 'processed_at is set' would claim the row reflects this event when it deliberately does not."),
        ),
        migrations.AddField(
            model_name='subscription',
            name='last_provider_event_at',
            field=models.DateTimeField(blank=True, help_text="Provider-clock time of the newest lifecycle event already applied to this row (the event's `created`, or the moment a reconcile re-read the provider). Stripe does not promise delivery order: without this, a `customer.subscription.created` carrying `incomplete` that arrives AFTER an `.updated` carrying `active` overwrites the live plan with the older word, and the paying subscriber is shown the paywall. NULL means 'nothing applied yet — accept the next event'.", null=True),
        ),
        migrations.AlterField(
            model_name='pendingsubscriptionperiod',
            name='status',
            field=models.CharField(blank=True, default='', help_text='Provider-side status string, applied when the row lands. Stored raw and full-width: the provider picks this word.', max_length=255),
        ),
        migrations.AlterField(
            model_name='stripewebhookevent',
            name='event_type',
            field=models.CharField(max_length=255),
        ),
        migrations.CreateModel(
            name='CompPeriod',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('plan', models.CharField(choices=[('free', 'Free'), ('pro', 'Pro'), ('team', 'Team'), ('enterprise', 'Enterprise')], help_text="The plan the comp window entitles to. Stored rather than read from the subscription: the comp is usually granted BECAUSE the subscription stopped saying 'pro', and a window that resolved its plan at read time would hand back the free one.", max_length=16)),
                ('starts_at', models.DateTimeField(help_text='When the comp window opens.')),
                ('ends_at', models.DateTimeField(help_text='When the comp window closes.')),
                ('reason', models.CharField(help_text='Why this was given. Required — an unexplained comp is the row an audit stops on.', max_length=255)),
                ('granted_by', models.CharField(blank=True, default='', help_text='The operator who granted it (admin username, or the shell user for a command run).', max_length=255)),
                ('revoked_at', models.DateTimeField(blank=True, help_text='Set to withdraw the window without deleting the record of it.', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('subscription', models.ForeignKey(help_text='The local subscription this comp time belongs to.', on_delete=django.db.models.deletion.CASCADE, related_name='comp_periods', to='billing.subscription')),
            ],
            options={
                'db_table': 'billing_comp_period',
                'indexes': [models.Index(fields=['subscription', '-ends_at'], name='billing_com_subscri_372688_idx')],
            },
        ),
        migrations.RunPython(_backfill_provider_clock, _forget_provider_clock),
    ]
