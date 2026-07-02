"""Payment provider backends for stapel-billing.

Select via ``STAPEL_BILLING = {"PAYMENT_PROVIDER": "dotted.path.ToProvider"}``.
"""

from .base import PaymentProvider

__all__ = ["PaymentProvider"]
