"""Split "may look at wallets" from "may move credits" (0.17.0).

No schema change: `AlterModelOptions` only re-states the model's Meta, and
the `grant_credits` Permission row is created by Django's own post-migrate
signal. Safe to apply while serving, and reversible without data loss — the
row simply stops being created.

Applying this migration NARROWS an existing deployment: the admin's
Grant-credits action was previously offered to anyone who could see the
wallet changelist, and afterwards it is offered only to somebody holding
`billing.grant_credits`. Grant it in the same fixture as the rest of the
operator's permissions before deploying, or the affordance disappears for
everyone except superusers.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0007_subscription_cancel_at_period_end'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='wallet',
            options={'permissions': [('grant_credits', 'Can grant credits by hand')]},
        ),
    ]
