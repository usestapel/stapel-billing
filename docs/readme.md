## Quick start

```python
# settings.py
INSTALLED_APPS = [
    ...
    'stapel_billing',
]
```

## Bus events

### Emits
| `payment.completed` | [schema](schemas/emits/payment.completed.json) | A payment transaction completed successfully. |
| `subscription.changed` | [schema](schemas/emits/subscription.changed.json) | User subscription plan or status changed. |

### Consumes
| `user.deleted` | [schema](schemas/consumes/user.deleted.json) |
| `user.deletion_initiated` | [schema](schemas/consumes/user.deletion_initiated.json) |
