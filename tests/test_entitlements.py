"""Tests for the comm surface of stapel-billing (entitlements + debit).

Covers the full ``billing.check_entitlement`` branch matrix (bool / int /
unknown key / no-subscription / non-granting statuses / catalogue
overrides), the quantity semantics, the ``billing.debit`` wrapper
(idempotency, insufficient balance, structural failures) and payload
validation against the committed ``schemas/functions/*.json`` contracts
through ``comm.call``.
"""

import uuid

import pytest
from django.utils import timezone

from stapel_core.comm import call
from stapel_core.comm.exceptions import SchemaValidationError

from stapel_billing import services
from stapel_billing.catalog import DEFAULT_PLANS, PlanCatalogEntry
from stapel_billing.models import (
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    Wallet,
)


def _check(user_id, key, **extra):
    return call(
        "billing.check_entitlement",
        {"user_id": str(user_id), "key": key, **extra},
    )


# ─── Catalogue defaults ─────────────────────────────────────


class TestCatalogEntitlements:
    def test_entitlements_default_is_empty_dict(self):
        entry = PlanCatalogEntry(
            slug="x", name="X", price_cents=0, monthly_credits_included=0,
            storage_limit_bytes=0, description="",
        )
        assert entry.entitlements == {}

    def test_default_plan_ladder(self):
        by_slug = {p.slug: p.entitlements for p in DEFAULT_PLANS}
        assert by_slug["free"] == {
            "workspaces.org": False,
            "workspaces.members.max": 5,
            "workspaces.provision_user": False,
        }
        assert by_slug["pro"] == {
            "workspaces.org": True,
            "workspaces.members.max": 25,
            "workspaces.provision_user": True,
        }
        assert by_slug["team"] == {
            "workspaces.org": True,
            "workspaces.members.max": 100,
            "workspaces.provision_user": True,
        }
        # Enterprise omits members.max on purpose: absent key = unrestricted.
        assert by_slug["enterprise"] == {
            "workspaces.org": True,
            "workspaces.provision_user": True,
        }


# ─── billing.check_entitlement ──────────────────────────────


@pytest.mark.django_db
class TestCheckEntitlement:
    def test_no_subscription_falls_back_to_free_plan_bool_false(self, user):
        result = _check(user.pk, "workspaces.org")
        assert result == {"allowed": False, "limit": None, "reason": "not_in_plan"}

    def test_no_subscription_int_limit_within(self, user):
        result = _check(user.pk, "workspaces.members.max", quantity=5)
        assert result == {"allowed": True, "limit": 5, "reason": None}

    def test_no_subscription_int_limit_exceeded(self, user):
        result = _check(user.pk, "workspaces.members.max", quantity=6)
        assert result == {"allowed": False, "limit": 5, "reason": "limit_exceeded"}

    def test_quantity_defaults_to_one(self, user):
        result = _check(user.pk, "workspaces.members.max")
        assert result == {"allowed": True, "limit": 5, "reason": None}

    def test_key_outside_the_vocabulary_is_denied(self, user):
        # A feature name nobody declared is a typo or a stale caller, and a
        # typo must not read as "no limit configured, go ahead" (BILL-01).
        result = _check(user.pk, "totally.unknown.key")
        assert result == {"allowed": False, "limit": None, "reason": "unknown_key"}

    def test_host_declared_key_is_recognised(self, user, settings):
        settings.STAPEL_BILLING = {"ENTITLEMENT_KEYS": ["recordings.minutes.max"]}
        # Declared but absent from the free plan → unrestricted, not denied.
        result = _check(user.pk, "recordings.minutes.max", quantity=10_000)
        assert result == {"allowed": True, "limit": None, "reason": None}

    def test_escape_hatch_restores_the_permissive_default(self, user, settings):
        settings.STAPEL_BILLING = {"ALLOW_UNKNOWN_ENTITLEMENT_KEYS": True}
        result = _check(user.pk, "totally.unknown.key")
        assert result == {"allowed": True, "limit": None, "reason": None}

    def test_active_subscription_bool_true(self, user):
        Subscription.objects.create(
            user=user, plan="pro", status=SubscriptionStatus.ACTIVE
        )
        result = _check(user.pk, "workspaces.org")
        assert result == {"allowed": True, "limit": None, "reason": None}
        assert _check(user.pk, "workspaces.provision_user")["allowed"] is True

    def test_team_plan_limit_boundary(self, user):
        Subscription.objects.create(
            user=user, plan="team", status=SubscriptionStatus.ACTIVE
        )
        assert _check(user.pk, "workspaces.members.max", quantity=100) == {
            "allowed": True, "limit": 100, "reason": None,
        }
        assert _check(user.pk, "workspaces.members.max", quantity=101) == {
            "allowed": False, "limit": 100, "reason": "limit_exceeded",
        }

    def test_trialing_and_past_due_keep_the_plan(self, user):
        sub = Subscription.objects.create(
            user=user, plan="pro", status=SubscriptionStatus.TRIALING
        )
        assert _check(user.pk, "workspaces.org")["allowed"] is True
        sub.status = SubscriptionStatus.PAST_DUE
        sub.save(update_fields=["status"])
        assert _check(user.pk, "workspaces.org")["allowed"] is True

    def test_cancelled_subscription_falls_back_to_free(self, user):
        Subscription.objects.create(
            user=user,
            plan="team",
            status=SubscriptionStatus.CANCELLED,
            cancelled_at=timezone.now(),
        )
        result = _check(user.pk, "workspaces.org")
        assert result == {"allowed": False, "limit": None, "reason": "not_in_plan"}
        # And the free member ceiling applies, not team's.
        assert _check(user.pk, "workspaces.members.max", quantity=6)["allowed"] is False

    def test_incomplete_subscription_falls_back_to_free(self, user):
        Subscription.objects.create(
            user=user, plan="pro", status=SubscriptionStatus.INCOMPLETE
        )
        assert _check(user.pk, "workspaces.org")["allowed"] is False

    def test_enterprise_absent_limit_key_is_unrestricted(self, user):
        Subscription.objects.create(
            user=user, plan="enterprise", status=SubscriptionStatus.ACTIVE
        )
        result = _check(user.pk, "workspaces.members.max", quantity=10_000)
        assert result == {"allowed": True, "limit": None, "reason": None}

    def test_plan_slug_missing_from_catalog_denies(self, user):
        # Host reconfigured PLANS and a subscriber is left on a ghost slug.
        # No entitlements resolve — which used to read as "no ceiling
        # configured, go ahead" and made the subscriber unlimited (BILL-03).
        Subscription.objects.create(
            user=user, plan="legacy", status=SubscriptionStatus.ACTIVE
        )
        assert _check(user.pk, "workspaces.org") == {
            "allowed": False,
            "limit": None,
            "reason": "unknown_plan",
        }

    def test_plan_slug_missing_from_catalog_allows_under_the_hatch(
        self, user, settings
    ):
        settings.STAPEL_BILLING = {"ALLOW_UNKNOWN_PLAN_SLUGS": True}
        Subscription.objects.create(
            user=user, plan="legacy", status=SubscriptionStatus.ACTIVE
        )
        assert _check(user.pk, "workspaces.org")["allowed"] is True

    def test_host_override_via_settings(self, user, settings):
        settings.STAPEL_BILLING = {
            "PLANS": [
                {
                    "slug": "free",
                    "name": "Free",
                    "price_cents": 0,
                    "monthly_credits_included": 0,
                    "storage_limit_bytes": 0,
                    "description": "",
                    "entitlements": {"workspaces.org": True},
                },
            ],
        }
        result = _check(user.pk, "workspaces.org")
        assert result == {"allowed": True, "limit": None, "reason": None}
        # members.max is still part of the vocabulary (the shipped ladder
        # declares it); this host's single plan just sets no ceiling.
        assert _check(user.pk, "workspaces.members.max", quantity=99)["allowed"] is True

    def test_payload_schema_enforced_through_comm_call(self, user):
        with pytest.raises(SchemaValidationError):
            call("billing.check_entitlement", {"user_id": str(user.pk)})
        with pytest.raises(SchemaValidationError):
            _check(user.pk, "workspaces.org", quantity=0)
        with pytest.raises(SchemaValidationError):
            _check(user.pk, "workspaces.org", extra_field="nope")


# ─── billing.debit ──────────────────────────────────────────


@pytest.mark.django_db
class TestDebitFunction:
    def _fund(self, user, balance=10):
        # Through the service, not by writing Wallet.balance: since 0.8.0 the
        # balance is a cache of the credit lots, and a hand-set number is a
        # wallet with nothing in it to spend.
        from stapel_billing.models import LotSource

        services.credit(
            user=user,
            credits=balance,
            type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
        )
        return Wallet.objects.get(user=user)

    def test_debit_success(self, user):
        self._fund(user, 10)
        result = call(
            "billing.debit",
            {"user_id": str(user.pk), "credits": 4, "idempotency_key": "op-1"},
        )
        assert result["ok"] is True
        assert result["balance"] == 6
        assert result["reason"] is None
        txn = Transaction.objects.get(id=result["transaction_id"])
        assert txn.credits_delta == -4
        assert txn.type == TransactionType.ADJUSTMENT  # the default type
        assert txn.metadata["idempotency_key"] == "op-1"

    def test_debit_idempotent_retry_returns_original(self, user):
        self._fund(user, 10)
        payload = {
            "user_id": str(user.pk),
            "credits": 4,
            "idempotency_key": "ws-provision:abc",
        }
        first = call("billing.debit", payload)
        second = call("billing.debit", payload)
        assert second["ok"] is True
        assert second["transaction_id"] == first["transaction_id"]
        assert second["balance"] == first["balance"] == 6
        assert Wallet.objects.get(user=user).balance == 6
        # The funding credit row is there too; exactly one DEBIT was written.
        assert Transaction.objects.filter(
            wallet__user=user, credits_delta__lt=0
        ).count() == 1

    def test_debit_insufficient_credits_is_structural(self, user):
        self._fund(user, 3)
        result = call(
            "billing.debit",
            {"user_id": str(user.pk), "credits": 5, "idempotency_key": "op-2"},
        )
        assert result == {
            "ok": False, "balance": 3, "reason": "insufficient_credits",
        }
        assert Wallet.objects.get(user=user).balance == 3
        assert not Transaction.objects.filter(
            wallet__user=user, credits_delta__lt=0
        ).exists()

    def test_debit_unknown_user(self, db):
        result = call(
            "billing.debit",
            {
                "user_id": str(uuid.uuid4()),
                "credits": 1,
                "idempotency_key": "op-3",
            },
        )
        assert result == {"ok": False, "balance": None, "reason": "user_not_found"}

    def test_debit_optional_type_description_metadata(self, user):
        self._fund(user, 10)
        result = call(
            "billing.debit",
            {
                "user_id": str(user.pk),
                "credits": 2,
                "idempotency_key": "op-4",
                "type": "ai_charge",
                "description": "Provisioned member",
                "metadata": {"workspace_id": "ws-1"},
            },
        )
        txn = Transaction.objects.get(id=result["transaction_id"])
        assert txn.type == TransactionType.AI_CHARGE
        assert txn.description == "Provisioned member"
        assert txn.metadata["workspace_id"] == "ws-1"
        assert txn.metadata["idempotency_key"] == "op-4"

    def test_payload_schema_enforced_through_comm_call(self, user):
        # idempotency_key is required on the comm surface (at-least-once).
        with pytest.raises(SchemaValidationError):
            call("billing.debit", {"user_id": str(user.pk), "credits": 1})
        with pytest.raises(SchemaValidationError):
            call(
                "billing.debit",
                {"user_id": str(user.pk), "credits": 0, "idempotency_key": "k"},
            )
        with pytest.raises(SchemaValidationError):
            call(
                "billing.debit",
                {
                    "user_id": str(user.pk),
                    "credits": 1,
                    "idempotency_key": "k",
                    "type": "not_a_transaction_type",
                },
            )


# ─── Contract files ─────────────────────────────────────────


class TestFunctionSchemas:
    def test_registered_schemas_are_the_committed_files(self):
        """The schema passed at register_function() time must be the
        committed schemas/functions/*.json content — one source of truth."""
        import json
        from pathlib import Path

        import stapel_billing.entitlements as ent

        schemas_dir = (
            Path(ent.__file__).resolve().parent / "schemas" / "functions"
        )
        for name, schema in [
            (ent.CHECK_ENTITLEMENT, ent.CHECK_ENTITLEMENT_SCHEMA),
            (ent.DEBIT, ent.DEBIT_SCHEMA),
        ]:
            committed = json.loads((schemas_dir / f"{name}.json").read_text())
            assert schema == committed
