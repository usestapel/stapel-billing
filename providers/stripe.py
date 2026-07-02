"""Stripe implementation of the PaymentProvider abstraction.

Keys are read lazily through ``billing_settings`` at call time
(STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET) — never frozen at import.
Falls back to dev placeholder URLs when Stripe is not configured so the
rest of the flow stays testable end-to-end.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from ..catalog import CREDIT_PACKAGES_BY_SLUG, PLANS_BY_SLUG
from .base import PaymentProvider

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
        """Used in dev/staging when Stripe is unconfigured."""
        return (
            f"https://example.invalid/stripe-not-configured/{kind}/{slug}",
            f"cs_dev_{slug}",
        )

    # ── portal ─────────────────────────────────────────────

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        stripe = self._configured_stripe()
        if not stripe or not customer_id:
            return "https://example.invalid/stripe-portal-not-configured"
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
        return portal["url"]

    # ── subscription ───────────────────────────────────────

    def cancel_subscription(self, subscription_id: str) -> None:
        stripe = self._configured_stripe()
        if not stripe or not subscription_id:
            # Not configured — nothing to cancel remotely.
            return
        stripe.Subscription.modify(subscription_id, cancel_at_period_end=True)

    # ── webhooks ───────────────────────────────────────────

    def verify_webhook(self, payload: bytes, signature: str) -> dict:
        stripe = self._stripe_module()
        if not stripe or not self.webhook_secret:
            raise ValueError("Stripe is not configured")
        return stripe.Webhook.construct_event(payload, signature, self.webhook_secret)
