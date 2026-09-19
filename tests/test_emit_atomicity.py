"""The suite's own emit-outside-atomic guard, and the paths it protects.

A gate nobody tests is a gate that quietly stops testing. The first test
here asserts the guard in ``conftest.py`` is installed and live; the rest
assert that the module's two non-webhook entry points into an ``emit()``
— the reconciliation sweep and the simulated purchase — announce from
inside a transaction, which is the whole of the outbox guarantee.
"""

import pytest
from django.db import transaction
from stapel_core.comm.exceptions import EmitOutsideAtomicError

from stapel_billing import services

from .stripe_ids import sid


class TestTheGuardItself:
    """If these fail, every other emit assertion in the suite is vacuous."""

    @pytest.mark.django_db
    def test_emit_outside_an_owned_atomic_block_is_an_error(self):
        # pytest-django has us inside transaction.atomic() right now, which
        # is exactly why the production `in_atomic_block` check cannot see
        # this. The depth guard can.
        with pytest.raises(EmitOutsideAtomicError):
            services.emit("payment.completed", {})

    @pytest.mark.django_db
    def test_emit_inside_an_owned_atomic_block_is_allowed(self):
        with transaction.atomic():
            # Reaches the real emit, so the payload is schema-validated too;
            # a bare {} would be refused for the wrong reason.
            services.emit(
                "subscription.changed",
                {
                    "user_id": "00000000-0000-0000-0000-000000000001",
                    "plan": "free",
                    "status": "active",
                    "current_period_end": None,
                    "cancel_at_period_end": False,
                },
                key="00000000-0000-0000-0000-000000000001",
            )


@pytest.mark.django_db
class TestReconcileAnnouncesInsideItsTransaction:
    """The defect this file was opened for: a repair that announced itself
    from autocommit, observed mid-sweep on a client host."""

    def _subscription(self, user, **kwargs):
        from stapel_billing.models import Subscription

        defaults = dict(
            user=user,
            plan="pro",
            status="active",
            stripe_subscription_id=sid("sub", "recon"),
            stripe_customer_id=sid("cus", "recon"),
        )
        defaults.update(kwargs)
        return Subscription.objects.create(**defaults)

    def test_a_repair_that_changes_status_emits_atomically(
        self, user, monkeypatch, settings
    ):
        sub = self._subscription(user, status="past_due")

        class _Provider:
            def fetch_subscription(self, subscription_id):
                return {
                    "id": subscription_id,
                    "status": "active",
                    "cancel_at_period_end": False,
                }

        monkeypatch.setattr(services, "get_provider", lambda: _Provider())

        # The guard raises out of services.emit, so a non-atomic repair
        # fails this test rather than logging and passing.
        (result,) = services.reconcile_subscriptions()

        assert result.applied is True
        assert "status" in result.changed
        sub.refresh_from_db()
        assert sub.status == "active"
