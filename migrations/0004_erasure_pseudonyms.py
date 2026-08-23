"""The ledger outlives the person: a detachable owner and a stable pseudonym.

Erasure in this package anonymizes, it never deletes — the deletion-lifecycle
spec's §6 verdict for every owner that carries a ledger. Two things had to
change in the schema before that was even expressible:

* ``Wallet.user`` and ``Subscription.user`` were **NOT NULL**, so the
  0.8.x provider's ``update(user_id=None)`` raised on every wallet it was
  handed — inside a bare ``except Exception: pass``, which reported success.
  A gate that lies is worse than no gate; the column is nullable now and
  the anonymisation actually commits.
* ``on_delete`` was ``CASCADE``, which was not merely useless here but
  actively harmful: ``Transaction.wallet`` is ``PROTECT``, so deleting the
  auth user row cascaded into a wallet the ledger refuses to release and
  raised ``ProtectedError`` — the account deletion failed on the very rows
  it was meant to preserve. ``SET_NULL`` detaches instead, which is what
  "keep the bill, drop the person" means at the database level.

``user_pseudonym`` is where the keyed HMAC lands (``erased:<32 hex>``, the
``stapel_video.presence.pseudonymize_user`` funnel). It carries a ``''``
default and is indexed, so one erased subject's rows stay one subject
without naming them.

Expand-only and backfill-free: existing rows keep their owner and get an
empty pseudonym, an old writer cannot violate the new rule, and no data
moves. Nothing is dropped, because nothing is replaced.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('billing', '0003_credit_lots_and_holds'),
    ]

    operations = [
        migrations.AddField(
            model_name='wallet',
            name='user_pseudonym',
            field=models.CharField(blank=True, db_index=True, default='', help_text="The erased owner's stable pseudonym ('erased:<hmac>'), written by stapel_billing.gdpr.erase_subject. Empty for a live wallet. Keeps one subject's history one subject without naming them.", max_length=64),
        ),
        migrations.AddField(
            model_name='subscription',
            name='user_pseudonym',
            field=models.CharField(blank=True, db_index=True, default='', help_text="The erased owner's stable pseudonym ('erased:<hmac>'). Empty for a live subscription.", max_length=64),
        ),
        migrations.AlterField(
            model_name='wallet',
            name='user',
            field=models.OneToOneField(blank=True, help_text='NULL once the owner has been erased — the ledger outlives the person (see stapel_billing.gdpr). SET_NULL and not CASCADE because Transaction protects its wallet: a cascade from the user row would raise ProtectedError and block the account deletion it was supposed to serve.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='wallet', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='subscription',
            name='user',
            field=models.OneToOneField(blank=True, help_text='NULL once the owner has been erased — see stapel_billing.gdpr.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='subscription', to=settings.AUTH_USER_MODEL),
        ),
    ]
