"""Payment provider abstraction.

Subclass :class:`PaymentProvider` and point
``STAPEL_BILLING["PAYMENT_PROVIDER"]`` at the dotted path to swap the
payment backend without forking::

    class MyProvider(PaymentProvider):
        name = "acme"
        ...

    STAPEL_BILLING = {"PAYMENT_PROVIDER": "myproject.billing.MyProvider"}

Providers must read their credentials lazily (at call time), never at
import time — tests and multi-tenant hosts change settings after import.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Tuple


class PaymentProvider(ABC):
    """Base class for payment backends (Stripe by default)."""

    #: Short human-readable provider name ("stripe", "paddle", ...).
    name: str = ""

    @abstractmethod
    def create_checkout_session(
        self,
        *,
        user,
        package: Optional[str],
        plan: Optional[str],
        success_url: str,
        cancel_url: str,
    ) -> Tuple[str, str]:
        """Start a checkout for a one-off *package* or recurring *plan*.

        Returns a ``(checkout_url, session_id)`` tuple.
        """

    @abstractmethod
    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        """Return a URL to the provider's self-service billing portal."""

    @abstractmethod
    def cancel_subscription(self, subscription_id: str) -> None:
        """Cancel the provider-side subscription (at period end).

        Must be a no-op when the provider is not configured; must raise on
        a failed remote call so callers do not mark the subscription
        cancelled locally while the provider keeps charging.
        """

    @abstractmethod
    def verify_webhook(self, payload: bytes, signature: str) -> dict:
        """Parse + verify an incoming webhook. Returns the event dict.

        Raises ``ValueError`` on an invalid signature or unconfigured
        provider.
        """
