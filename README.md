# stapel-billing

[![CI](https://img.shields.io/github/actions/workflow/status/usestapel/stapel-billing/ci.yml?branch=main&logo=github&label=CI)](https://github.com/usestapel/stapel-billing/actions/workflows/ci.yml?query=branch%3Amain)
[![coverage](https://img.shields.io/codecov/c/github/usestapel/stapel-billing?branch=main&logo=codecov&label=coverage)](https://app.codecov.io/gh/usestapel/stapel-billing)
[![pypi](https://img.shields.io/pypi/v/stapel-billing?logo=pypi&logoColor=white&label=pypi)](https://pypi.org/project/stapel-billing/)
[![downloads](https://static.pepy.tech/badge/stapel-billing/month)](https://pepy.tech/project/stapel-billing)
[![python](https://img.shields.io/pypi/pyversions/stapel-billing?logo=python&logoColor=white)](https://pypi.org/project/stapel-billing/)
[![license](https://img.shields.io/github/license/usestapel/stapel-billing)](https://github.com/usestapel/stapel-billing/blob/main/LICENSE)
[![llms.txt](https://img.shields.io/badge/llms.txt-blue)](https://github.com/usestapel/stapel-billing/blob/main/docs/llms.txt)

> Billing — Stripe subscriptions, credit wallets, transaction history

Part of the [Stapel framework](https://github.com/usestapel) — composable Django apps for building production-grade platforms.

**Error reference:** [Errors (EN)](docs/errors.en.md) · [Ошибки (RU)](docs/errors.ru.md) · [Errores (ES)](docs/errors.es.md)

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
