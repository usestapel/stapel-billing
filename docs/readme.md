## Quick start

```python
# settings.py
INSTALLED_APPS = [
    ...
    'stapel_billing',
]

from stapel_billing.tasks import get_billing_beat_schedule

CELERY_BEAT_SCHEDULE = {
    **get_billing_beat_schedule(),   # credit expiry, hold sweep, reconcile
}
```

Register the beat schedule (or run the three callables in `stapel_billing.tasks`
from your own cron): credits granted with an expiry only expire because
something runs. System check `stapel_billing.W105` says so if nothing does.

## Wallets are lots

A wallet is a set of `CreditLot` rows — each one knows where its credits came
from (`purchase`, `subscription`, `grant`, `adjustment`, `hold_release`) and
when they die (`expires_at`, `NULL` = never). Spend walks the lots
**expiring-soonest first**, so a subscription bundle is used before the
non-expiring credits a customer paid cash for. `Wallet.balance` is a
maintained cache of that total, recomputed from the lots inside the same row
lock as every mutation.

```python
from stapel_billing import credit, debit, hold, capture, release
from stapel_billing.models import LotSource, TransactionType

credit(user=user, credits=3000, type=TransactionType.SUBSCRIPTION_BONUS,
       source=LotSource.SUBSCRIPTION, expires_at=sub.current_period_end)

held = hold(user=user, credits=15, type=TransactionType.AI_CHARGE,
            idempotency_key=f"mic:{recording_id}")
try:
    used = do_the_work()
except Exception:
    release(hold_id=held.id)      # credits go back with their original expiry
else:
    capture(hold_id=held.id, actual_credits=used)
```

## Bus events

### Emits
| `payment.completed` | [schema](schemas/emits/payment.completed.json) | A payment transaction completed successfully. |
| `subscription.changed` | [schema](schemas/emits/subscription.changed.json) | User subscription plan or status changed. |

### Consumes
| `user.deleted` | [schema](schemas/consumes/user.deleted.json) |
| `user.deletion_initiated` | [schema](schemas/consumes/user.deletion_initiated.json) |
