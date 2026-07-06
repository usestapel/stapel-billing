from django.contrib import admin

from stapel_core.django.admin.base import StapelModelAdmin

from .models import StripeWebhookEvent, Subscription, Transaction, Wallet


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
