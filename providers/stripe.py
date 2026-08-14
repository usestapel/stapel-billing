"""Stripe implementation of the PaymentProvider abstraction.

Keys are read lazily through ``billing_settings`` at call time
(STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET) — never frozen at import.

Without a key this provider **refuses**: every entry point raises
:class:`~stapel_billing.providers.base.ProviderNotConfiguredError`. The dev
placeholder URLs it used to return instead are a fabricated success — a 200
with a ``cs_dev_*`` session id, a "cancelled" subscription nobody cancelled —
and they are now reachable only when the deployment opts in explicitly with
``STAPEL_BILLING["ALLOW_UNCONFIGURED_PAYMENT_PROVIDER"]``, which is loud in
both the log and ``manage.py check`` (W104).
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from ..catalog import CREDIT_PACKAGES_BY_SLUG, PLANS_BY_SLUG
from .base import PaymentProvider, ProviderNotConfiguredError

logger = logging.getLogger(__name__)


class StripeProvider(PaymentProvider):
    name = "stripe"

    # ── lazy configuration ─────────────────────────────────

    @property
    def api_key(self) -> str:
        from ..conf import billing_settings

        return billing_settings.STRIPE_SECRET_KEY or ""

    @property
    def webhook_secret(self) -> str:
        from ..conf import billing_settings

        return billing_settings.STRIPE_WEBHOOK_SECRET or ""

    def _stripe_module(self):
        try:
            import stripe  # type: ignore
        except ImportError:
            return None
        if self.api_key:
            stripe.api_key = self.api_key
        return stripe

    def _configured_stripe(self):
        """Return the stripe module iff both the SDK and a key are present."""
        stripe = self._stripe_module()
        if not stripe or not self.api_key:
            return None
        return stripe

    def is_configured(self) -> bool:
        return self._configured_stripe() is not None

    def _refuse_unless_dev_placeholders_allowed(self, operation: str) -> None:
        """Raise unless the deployment asked for fake results explicitly.

        The placeholder exists so a developer can walk the flow without a
        Stripe account. In every other deployment "Stripe is not set up" must
        surface as a failure — a fabricated checkout URL, portal link or
        silent cancel is the service lying about money on the operator's
        behalf.
        """
        from ..conf import allow_unconfigured_payment_provider

        if not allow_unconfigured_payment_provider():
            raise ProviderNotConfiguredError(
                f"Stripe is not configured (STAPEL_BILLING['STRIPE_SECRET_KEY'] "
                f"is empty or the stripe SDK is missing), so {operation} cannot "
                "be served. Set the key, or opt into dev placeholders with "
                "STAPEL_BILLING['ALLOW_UNCONFIGURED_PAYMENT_PROVIDER']."
            )
        logger.warning(
            "Stripe is not configured — serving a dev placeholder for %s "
            "because ALLOW_UNCONFIGURED_PAYMENT_PROVIDER is on. No payment "
            "system was reached.",
            operation,
        )

    # ── checkout ───────────────────────────────────────────

    def create_checkout_session(
        self,
        *,
        user,
        package: Optional[str],
        plan: Optional[str],
        success_url: str,
        cancel_url: str,
    ) -> Tuple[str, str]:
        stripe = self._configured_stripe()
        if package:
            pkg = CREDIT_PACKAGES_BY_SLUG[package]
            if not stripe:
                self._refuse_unless_dev_placeholders_allowed("a package checkout")
                return self._placeholder_checkout("package", pkg.slug)
            session = stripe.checkout.Session.create(
                mode="payment",
                payment_method_types=["card"],
                success_url=success_url,
                cancel_url=cancel_url,
                client_reference_id=str(user.id),
                metadata={"user_id": str(user.id), "package": pkg.slug},
                line_items=[
                    {
                        "price_data": {
                            "currency": pkg.currency.lower(),
                            "product_data": {"name": f"Credit pack: {pkg.name}"},
                            "unit_amount": pkg.price_cents,
                        },
                        "quantity": 1,
                    }
                ],
            )
            return session["url"], session["id"]

        plan_entry = PLANS_BY_SLUG[plan]
        if not stripe:
            self._refuse_unless_dev_placeholders_allowed("a plan checkout")
            return self._placeholder_checkout("plan", plan_entry.slug)
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=str(user.id),
            metadata={"user_id": str(user.id), "plan": plan_entry.slug},
            line_items=[
                {
                    "price_data": {
                        "currency": plan_entry.currency.lower(),
                        "recurring": {"interval": "month"},
                        "product_data": {"name": plan_entry.name},
                        "unit_amount": plan_entry.price_cents,
                    },
                    "quantity": 1,
                }
            ],
        )
        return session["url"], session["id"]

    @staticmethod
    def _placeholder_checkout(kind: str, slug: str) -> Tuple[str, str]:
        """Dev-only fake, reachable solely through the opt-in hatch above."""
        return (
            f"https://example.invalid/stripe-not-configured/{kind}/{slug}",
            f"cs_dev_{slug}",
        )

    # ── portal ─────────────────────────────────────────────

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        stripe = self._configured_stripe()
        if not stripe:
            self._refuse_unless_dev_placeholders_allowed("the customer portal")
            return "https://example.invalid/stripe-portal-not-configured"
        if not customer_id:
            # Configured, but this user has no provider-side customer yet:
            # there is no portal to open. Unchanged behaviour — the deployment
            # is fine, the user simply has never paid.
            return "https://example.invalid/stripe-portal-not-configured"
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
        return portal["url"]

    # ── subscription ───────────────────────────────────────

    def cancel_subscription(self, subscription_id: str) -> None:
        stripe = self._configured_stripe()
        if not stripe:
            # Returning here is what let the caller mark the subscription
            # cancelled locally and tell the user they are done paying while
            # Stripe kept charging — the one failure mode a cancel must not
            # have.
            self._refuse_unless_dev_placeholders_allowed("a subscription cancel")
            return
        if not subscription_id:
            # Nothing was ever created provider-side; local state is the
            # whole truth.
            return
        stripe.Subscription.modify(subscription_id, cancel_at_period_end=True)

    # ── webhooks ───────────────────────────────────────────

    def verify_webhook(self, payload: bytes, signature: str) -> dict:
        stripe = self._stripe_module()
        if not stripe or not self.webhook_secret:
            raise ValueError("Stripe is not configured")
        return stripe.Webhook.construct_event(payload, signature, self.webhook_secret)
