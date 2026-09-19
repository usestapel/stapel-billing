"""The plan columns take their choices from the catalogue, not the enum (0.21.0).

No DDL: `choices` is Django-level metadata, so both AlterFields are no-ops
against the database and the column stays varchar(16). What changes is what
a form will accept — the deployment's configured plans
(`STAPEL_BILLING["PLANS"]`, read per access through
`catalog.plan_choices`) instead of the four plans this library ships.

A host sells its own ladder and its subscriptions have always carried its
own slugs (the ORM never validates `choices` on save), so the enum here
described a set the data was never in: the admin could not edit the row it
was displaying, and the same enum used as a membership test in
`services.extend_subscription` refused to comp any real customer.
"""

import stapel_billing.catalog
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0009_provider_time_and_comp_periods'),
    ]

    operations = [
        migrations.AlterField(
            model_name='compperiod',
            name='plan',
            field=models.CharField(choices=stapel_billing.catalog.plan_choices, help_text="The plan the comp window entitles to. Stored rather than read from the subscription: the comp is usually granted BECAUSE the subscription stopped saying 'pro', and a window that resolved its plan at read time would hand back the free one.", max_length=16),
        ),
        migrations.AlterField(
            model_name='subscription',
            name='plan',
            field=models.CharField(choices=stapel_billing.catalog.plan_choices, default='free', max_length=16),
        ),
    ]
