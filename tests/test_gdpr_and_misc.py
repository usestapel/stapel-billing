"""GDPR provider, action handlers, admin registration, catalog lazy views."""

import types

import pytest

from stapel_billing import catalog, services
from stapel_billing.gdpr import BillingGDPRProvider
from stapel_billing.models import (
    LotSource,
    Subscription,
    Transaction,
    TransactionType,
    Wallet,
)


@pytest.mark.django_db
class TestGDPRExport:
    def test_export_with_full_data(self, user):
        services.credit(
            user=user,
            credits=100,
            type=TransactionType.CREDIT_PURCHASE,
            source=LotSource.PURCHASE,
            amount_cents=200,
            description="pack",
        )
        Subscription.objects.create(user=user, plan="pro", status="active")

        data = BillingGDPRProvider().export(user.id)

        assert data["wallet"]["balance"] == "100"
        assert data["wallet"]["currency"] == "USD"
        assert len(data["transactions"]) == 1
        txn = data["transactions"][0]
        assert txn["type"] == "credit_purchase"
        assert txn["credits_delta"] == 100
        assert isinstance(txn["created_at"], str)  # dates serialized
        assert data["subscription"]["plan"] == "pro"
        assert data["subscription"]["status"] == "active"
        assert data["subscription"]["cancelled_at"] is None

    def test_export_without_data(self, user):
        data = BillingGDPRProvider().export(user.id)
        assert data == {
            "wallet": {},
            "credit_lots": [],
            "credit_holds": [],
            "transactions": [],
            "subscription": {},
        }


@pytest.mark.django_db
class TestGDPRDelete:
    def test_delete_clears_stripe_ids(self, user):
        sub = Subscription.objects.create(
            user=user,
            plan="pro",
            stripe_subscription_id="sub_pii",
            stripe_customer_id="cus_pii",
        )
        BillingGDPRProvider().delete(user.id)
        sub.refresh_from_db()
        assert sub.stripe_subscription_id == ""
        assert sub.stripe_customer_id == ""

    def test_delete_without_rows_is_noop(self, user):
        BillingGDPRProvider().delete(user.id)  # must not raise

    def test_anonymize_is_noop(self, user):
        assert BillingGDPRProvider().anonymize(user.id) is None


@pytest.mark.django_db
class TestUserDeletedAction:
    def _event(self, payload):
        return types.SimpleNamespace(payload=payload, event_id="evt-act-1")

    def test_handle_user_deleted_erases_billing_pii(self, user):
        from stapel_billing.actions import handle_user_deleted

        sub = Subscription.objects.create(
            user=user, plan="pro", stripe_customer_id="cus_1"
        )
        handle_user_deleted(self._event({"user_id": str(user.id)}))
        sub.refresh_from_db()
        assert sub.stripe_customer_id == ""

    def test_handle_user_deleted_without_user_id_logs_and_returns(self, caplog):
        from stapel_billing.actions import handle_user_deleted

        with caplog.at_level("ERROR", logger="stapel_billing.actions"):
            handle_user_deleted(self._event({}))
        assert any("without user_id" in r.message for r in caplog.records)


def test_admin_registers_all_models():
    from django.contrib import admin

    from stapel_billing import admin as billing_admin  # noqa: F401
    from stapel_billing.models import StripeWebhookEvent

    for model in (Wallet, Transaction, Subscription, StripeWebhookEvent):
        assert model in admin.site._registry


@pytest.mark.django_db
def test_model_str_representations(user):
    wallet = Wallet.objects.create(user=user, balance=5)
    txn = Transaction.objects.create(
        wallet=wallet,
        type=TransactionType.CREDIT_PURCHASE,
        credits_delta=5,
        balance_after=5,
    )
    sub = Subscription.objects.create(user=user, plan="pro", status="active")
    assert str(user.id) in str(wallet) and "5" in str(wallet)
    assert "+5" in str(txn)
    assert "pro" in str(sub)


class TestCatalogLazyViews:
    def test_coerce_rejects_garbage(self):
        with pytest.raises(TypeError):
            catalog._coerce(42, catalog.CreditPackage)

    def test_lazy_list_protocol(self):
        assert len(catalog.CREDIT_PACKAGES) == 3
        assert catalog.CREDIT_PACKAGES[0].slug == "starter"
        assert "starter" in repr(catalog.CREDIT_PACKAGES)

    def test_lazy_mapping_protocol(self):
        assert len(catalog.PLANS_BY_SLUG) == 4
        assert set(iter(catalog.PLANS_BY_SLUG)) == {"free", "pro", "team", "enterprise"}
        assert "enterprise" in repr(catalog.PLANS_BY_SLUG)


def test_error_keys_view_exposes_billing_errors():
    from stapel_billing.errors import BILLING_ERRORS, BillingErrorKeysView

    assert BillingErrorKeysView().get_service_errors() == BILLING_ERRORS


@pytest.mark.django_db
class TestServiceGuards:
    def test_credit_rejects_non_positive(self, user):
        with pytest.raises(ValueError):
            services.credit(
                user=user,
                credits=0,
                type=TransactionType.ADJUSTMENT,
                source=LotSource.ADJUSTMENT,
            )

    def test_debit_rejects_non_positive(self, user):
        with pytest.raises(ValueError):
            services.debit(user=user, credits=-1, type=TransactionType.AI_CHARGE)
