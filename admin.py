from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ActionForm

from stapel_core.django.admin.base import StapelModelAdmin

from .models import (
    CreditDebt,
    CreditHold,
    CreditLot,
    LotSource,
    StripeWebhookEvent,
    Subscription,
    Transaction,
    TransactionType,
    Wallet,
)


class GrantCreditsActionForm(ActionForm):
    """The two things a manual grant needs: how many, and why.

    An action form rather than an intermediate page: the fields sit in the
    action bar the operator is already using, and the reason is REQUIRED —
    a credit adjustment nobody can explain later is exactly the row an
    audit stops on.
    """

    grant_credits = forms.IntegerField(
        required=False, min_value=1, label="Credits to grant"
    )
    grant_reason = forms.CharField(
        required=False, max_length=255, label="Reason (recorded on the ledger row)"
    )


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ["user", "balance", "currency", "auto_recharge_enabled", "updated_at"]
    search_fields = ["user__email", "user__id"]
    # `balance` is a CACHE of the live lots, not the truth (see the model
    # docstring). Editing it here wrote a number the lots do not support:
    # no lot moved, no ledger row explained it, and the next operation —
    # which recomputes the cache under the wallet's lock — silently threw
    # the edit away. The only honest way to change a balance from the admin
    # is the action below, which moves real credits.
    readonly_fields = ["id", "balance", "created_at", "updated_at"]
    action_form = GrantCreditsActionForm
    actions = ["grant_credits"]

    @admin.action(description="Grant credits (writes a ledger row)")
    def grant_credits(self, request, queryset):
        """Add credits to the selected wallets through services.credit().

        Goes through the service, not the ORM: the grant creates a lot,
        updates the cache under the row lock, writes the ledger row that
        explains it and settles any outstanding debt — the four things a
        hand-edited balance skipped.
        """
        from . import services

        credits = request.POST.get("grant_credits")
        reason = (request.POST.get("grant_reason") or "").strip()
        try:
            credits = int(credits)
        except (TypeError, ValueError):
            credits = 0
        if credits <= 0:
            self.message_user(
                request,
                "Enter a positive number of credits to grant.",
                level=messages.ERROR,
            )
            return
        if not reason:
            self.message_user(
                request,
                "Enter a reason — it is written onto the ledger row and is "
                "the only explanation an audit will find.",
                level=messages.ERROR,
            )
            return
        granted = 0
        for wallet in queryset:
            if wallet.user_id is None:
                self.message_user(
                    request,
                    f"Wallet {wallet.id} has no owner (erased) — skipped.",
                    level=messages.WARNING,
                )
                continue
            services.credit(
                user=wallet.user,
                credits=credits,
                type=TransactionType.ADJUSTMENT,
                # Never expires: a manual grant has no billing period behind
                # it, so there is no honest deadline to give it.
                source=LotSource.ADJUSTMENT,
                expires_at=None,
                description=f"Admin grant: {reason}"[:255],
                metadata={
                    "admin_grant": True,
                    "reason": reason,
                    "granted_by": str(getattr(request.user, "id", "")),
                },
            )
            granted += 1
        if granted:
            self.message_user(
                request,
                f"Granted {credits} credit(s) to {granted} wallet(s).",
                level=messages.SUCCESS,
            )


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


@admin.register(CreditDebt)
class CreditDebtAdmin(admin.ModelAdmin):
    list_display = [
        "wallet",
        "reason",
        "credits_outstanding",
        "credits_initial",
        "settled_at",
        "created_at",
    ]
    list_filter = ["reason", "type"]
    search_fields = ["wallet__user__email", "wallet__user__id", "idempotency_key"]
    # A debt is collected by services.credit() under the wallet's row lock;
    # writing one off here would move credits with no ledger row behind it.
    readonly_fields = [
        "id",
        "wallet",
        "credits_initial",
        "credits_outstanding",
        "reason",
        "type",
        "description",
        "metadata",
        "idempotency_key",
        "transaction",
        "settled_at",
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
