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


class ProviderNotConfiguredError(RuntimeError):
    """The payment backend has no credentials, so it can serve nothing.

    Raised instead of returning a fabricated session/URL: a 200 carrying a
    made-up ``session_id`` tells the caller a payment is under way when no
    payment system was ever reached. Views translate it into a 502 —
    "the upstream cannot serve this" — never into a success.
    """


class PaymentProvider(ABC):
    """Base class for payment backends (Stripe by default)."""

    #: Short human-readable provider name ("stripe", "paddle", ...).
    name: str = ""

    def is_configured(self) -> bool:
        """Whether this provider can actually reach its payment system.

        Default ``True``: a third-party provider knows its own credentials
        and nothing here can introspect them, so the honest default is to
        believe it. Providers that can be *half* installed — the Stripe one
        ships with an empty key — override this, and
        ``checks.check_payment_provider_configured`` turns the answer into a
        deploy-time refusal instead of a runtime surprise.
        """
        return True

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

        Must raise on a failed remote call — and on an unconfigured
        provider (:class:`ProviderNotConfiguredError`) — so callers do not
        mark the subscription cancelled locally while the provider keeps
        charging. Returning quietly is what makes a user believe they are
        no longer being billed.
        """

    @abstractmethod
    def verify_webhook(self, payload: bytes, signature: str) -> dict:
        """Parse + verify an incoming webhook. Returns the event dict.

        Raises ``ValueError`` on an invalid signature or unconfigured
        provider.
        """
