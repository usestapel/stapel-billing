# stapel-billing

[![CI](https://github.com/usestapel/stapel-billing/actions/workflows/ci.yml/badge.svg)](https://github.com/usestapel/stapel-billing/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/usestapel/stapel-billing/graph/badge.svg)](https://codecov.io/gh/usestapel/stapel-billing)

> Billing — Stripe subscriptions, credit wallets, transaction history

Part of the [Stapel framework](https://github.com/usestapel) — composable Django apps for building production-grade platforms.

**Error reference:** [Errors (EN)](docs/errors.en.md) · [Ошибки (RU)](docs/errors.ru.md)

## Installation

```bash
pip install stapel-billing
```

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

## License

MIT — see [LICENSE](LICENSE)
