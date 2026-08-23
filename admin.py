from django.contrib import admin

from stapel_core.django.admin.base import StapelModelAdmin

from .models import (
    CreditHold,
    CreditLot,
    StripeWebhookEvent,
    Subscription,
    Transaction,
    Wallet,
)


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ["user", "balance", "currency", "auto_recharge_enabled", "updated_at"]
    search_fields = ["user__email", "user__id"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ["wallet", "type", "credits_delta", "balance_after", "created_at"]
    list_filter = ["type"]
    search_fields = ["wallet__user__email", "description"]
    readonly_fields = [
        "id",
        "wallet",
        "type",
        "amount_cents",
        "credits_delta",
        "balance_after",
        "metadata",
        "created_at",
    ]


@admin.register(CreditLot)
class CreditLotAdmin(admin.ModelAdmin):
    list_display = ["wallet", "source", "credits_remaining", "credits_initial", "expires_at", "created_at"]
    list_filter = ["source"]
    search_fields = ["wallet__user__email", "wallet__user__id"]
    # A lot is moved by services.py under the wallet's row lock; editing one
    # here would change a balance without the ledger row that explains it.
    readonly_fields = [
        "id",
        "wallet",
        "source",
        "credits_initial",
        "credits_remaining",
        "expires_at",
        "granting_transaction",
        "created_at",
    ]


@admin.register(CreditHold)
class CreditHoldAdmin(admin.ModelAdmin):
    list_display = ["wallet", "credits", "type", "status", "expires_at", "created_at"]
    list_filter = ["status", "type"]
    search_fields = ["wallet__user__email", "idempotency_key", "description"]
    readonly_fields = [
        "id",
        "wallet",
        "credits",
        "type",
        "description",
        "metadata",
        "idempotency_key",
        "status",
        "expires_at",
        "resolved_at",
        "created_at",
    ]


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ["user", "plan", "status", "current_period_end", "cancelled_at"]
    list_filter = ["plan", "status"]
    search_fields = ["user__email", "stripe_subscription_id", "stripe_customer_id"]


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(StapelModelAdmin):
    list_display = ["stripe_event_id", "event_type", "processed_at", "received_at"]
    list_filter = ["event_type"]
    search_fields = ["stripe_event_id"]
