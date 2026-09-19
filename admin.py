from uuid import uuid4

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ActionForm
from django.utils import timezone

from stapel_core.django.admin.base import StapelModelAdmin

from .models import (
    CompPeriod,
    CreditDebt,
    CreditHold,
    CreditLot,
    StripeWebhookEvent,
    Subscription,
    Transaction,
    Wallet,
)


#: How many wallets one submission of the action may credit. A manual grant
#: is a deliberate act on named accounts; "select all 2 000 wallets and give
#: everyone 500 credits" is a slip, not an instruction, and the changelist's
#: own "select all N across pages" link makes that slip one click away.
MAX_WALLETS_PER_GRANT = 25


def _grant_nonce() -> str:
    """A fresh idempotency key per rendering of the changelist.

    The browser's back button, a double-click on "Go" and a retried POST all
    resubmit the same form. Without a key each one is another grant, and the
    operator's only clue is a balance that climbed further than they asked
    for. The token is rendered hidden in the action bar, so a resubmission
    of the SAME form carries the SAME key and grants once, while a genuine
    second grant — a reloaded page — carries a new one.
    """
    return f"admin-grant:{uuid4()}"


class GrantCreditsActionForm(ActionForm):
    """The three things a manual grant needs: how many, why, and once.

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
    grant_token = forms.CharField(
        required=False, widget=forms.HiddenInput, initial=_grant_nonce
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

    def has_grant_credits_permission(self, request):
        """Backs ``permissions=["grant_credits"]`` on the action below.

        Django resolves an action's declared permission by calling
        ``has_<name>_permission`` on the ModelAdmin, so this method IS the
        gate — without it the declaration silently allows nobody.
        """
        # The codename is LITERAL, not `get_permission_codename(...)`: that
        # helper builds `<action>_<model>` for Django's built-in four, and
        # Wallet.Meta declares this one as plain `grant_credits`. Deriving it
        # produced `grant_credits_wallet`, which nothing grants — the gate
        # then refuses everybody, which is the safe direction and exactly the
        # kind of silent mismatch a test has to catch rather than a reviewer.
        return request.user.has_perm(f"{self.opts.app_label}.grant_credits")

    @admin.action(
        description="Grant credits (writes a ledger row)",
        # WITHOUT THIS the action is offered to anyone who can open the
        # changelist. Django only filters an action by permission when the
        # action says which permission it needs, so a deployment that granted
        # its operators `view_wallet` and nothing else — deliberately, so no
        # one could hand-edit a balance — was also granting them the ability
        # to create credits from nothing, with no way to separate the two.
        permissions=["grant_credits"],
    )
    def grant_credits(self, request, queryset):
        """Add credits to the selected wallets through services.grant_credits().

        Goes through the service, not the ORM: the grant creates a lot,
        updates the cache under the row lock, writes the ledger row that
        explains it and settles any outstanding debt — the four things a
        hand-edited balance skipped. The SAME service the
        ``billing_grant_credits`` management command uses, so a grant made
        from a browser and a grant made from a terminal are the same row.

        Three things this does that the 0.14.0 version did not, each of them
        a way a *working* action still let an operator down:

        1. **It writes to the admin's own log.** A custom action gets no
           ``LogEntry`` for free, so "who changed what in the admin" showed
           nothing for every grant ever made — and an empty
           ``django_admin_log`` reads as "nobody has ever used this", which
           is how an action that works gets diagnosed as broken.
        2. **It is safe to resubmit.** Back button, double-click, retried
           POST: one rendering of the form grants once (:func:`_grant_nonce`).
        3. **It refuses a select-all.** The changelist offers "select all N
           across pages" one click from the action bar, and running a grant
           that way would credit every account in the deployment.
        """
        from . import services

        credits = request.POST.get("grant_credits")
        reason = (request.POST.get("grant_reason") or "").strip()
        token = (request.POST.get("grant_token") or "").strip()
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
        if request.POST.get("select_across") in ("1", "true", "True"):
            self.message_user(
                request,
                "Refusing a select-all grant: tick the wallets you mean. "
                "Credits handed to every account in the deployment cannot be "
                "taken back without another ledger row per account.",
                level=messages.ERROR,
            )
            return

        wallets = list(queryset[: MAX_WALLETS_PER_GRANT + 1])
        if len(wallets) > MAX_WALLETS_PER_GRANT:
            self.message_user(
                request,
                f"Refusing to grant to more than {MAX_WALLETS_PER_GRANT} "
                "wallets at once. Grant in smaller batches, or use "
                "`manage.py billing_grant_credits`, which grants to one "
                "named account at a time.",
                level=messages.ERROR,
            )
            return

        actor = (
            getattr(request.user, "get_username", lambda: "")()
            or str(getattr(request.user, "id", ""))
            or "admin"
        )
        granted = 0
        replayed = 0
        # Taken before the loop so "is this row the one I just wrote" is
        # answered by the row's own age. The alternative — comparing
        # balances — agrees by accident whenever the amount happens to
        # match, which on a resubmitted form is ALWAYS.
        started = timezone.now()
        for wallet in wallets:
            if wallet.user_id is None:
                self.message_user(
                    request,
                    f"Wallet {wallet.id} has no owner (erased) — skipped.",
                    level=messages.WARNING,
                )
                continue
            try:
                txn = services.grant_credits(
                    user=wallet.user,
                    credits=credits,
                    reason=reason,
                    actor=actor,
                    # Per wallet AND per rendering of the form: resubmitting
                    # the same form is a no-op, a freshly loaded page is a
                    # new grant.
                    idempotency_key=f"{token}:{wallet.id}" if token else None,
                )
            except ValueError as exc:
                self.message_user(
                    request,
                    f"Wallet {wallet.id}: {exc} — skipped.",
                    level=messages.ERROR,
                )
                continue
            wallet.refresh_from_db()
            if txn.created_at < started:
                # The token short-circuited this one: the form was
                # resubmitted. Say THAT — reporting a grant that did not
                # happen is the same lie as granting twice, told the other
                # way round — and write no log row, because nothing changed.
                replayed += 1
                self.message_user(
                    request,
                    f"Wallet {wallet.id}: already granted by this form "
                    f"(transaction {txn.id}) — nothing moved. Balance is "
                    f"still {wallet.balance}.",
                    level=messages.WARNING,
                )
                continue
            granted += 1
            # The audit trail a human looks for first. Without it the admin
            # log stays silent about the one action in here that moves money.
            self.log_change(
                request,
                wallet,
                f"Granted {credits} credit(s) ({reason}) — transaction "
                f"{txn.id}, balance now {wallet.balance}.",
            )
            self.message_user(
                request,
                f"Wallet {wallet.id}: balance is now {wallet.balance} "
                f"(transaction {txn.id}).",
                level=messages.INFO,
            )
        if granted:
            self.message_user(
                request,
                f"Granted {credits} credit(s) to {granted} wallet(s).",
                level=messages.SUCCESS,
            )
        elif replayed:
            self.message_user(
                request,
                f"Nothing granted: this form had already credited "
                f"{replayed} wallet(s). Reload the page to grant again.",
                level=messages.WARNING,
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


class ExtendSubscriptionActionForm(ActionForm):
    """Days, why, and whether this is meant to add to comp time already given.

    Same shape as the grant-credits form above and for the same reason: the
    reason is what makes the act auditable, so it sits in the action bar
    rather than in somebody's memory.
    """

    extend_days = forms.IntegerField(
        required=False, min_value=1, label="Comp days"
    )
    extend_reason = forms.CharField(
        required=False, max_length=255, label="Reason (recorded on the comp period)"
    )
    extend_stack = forms.BooleanField(
        required=False,
        label="Add to comp time this account already has",
    )


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = [
        "user",
        "plan",
        "status",
        "current_period_end",
        "cancelled_at",
        "last_provider_event_at",
    ]
    list_filter = ["plan", "status"]
    search_fields = ["user__email", "stripe_subscription_id", "stripe_customer_id"]
    action_form = ExtendSubscriptionActionForm
    actions = ["extend_subscription"]

    def has_extend_subscription_permission(self, request):
        """Backs ``permissions=["extend_subscription"]`` on the action below.

        Literal codename, matching ``Subscription.Meta.permissions`` — the
        derived ``extend_subscription_subscription`` would be a permission
        nothing grants, and the gate would then refuse everybody.
        """
        return request.user.has_perm(f"{self.opts.app_label}.extend_subscription")

    @admin.action(
        description="Give comp subscription time (writes a comp period)",
        permissions=["extend_subscription"],
    )
    def extend_subscription(self, request, queryset):
        """Comp time through ``services.extend_subscription`` — one code path.

        The same function the ``billing_extend_subscription`` command calls,
        so a window granted from a browser and one granted from a terminal
        are the same row with the same rules: the plan comes from the
        subscription, the window starts when the paid period ends, and
        stacking on comp time that already exists has to be asked for.

        No Stripe write happens. Nothing here edits
        ``current_period_end`` — the column the provider overwrites.
        """
        from . import services

        days = request.POST.get("extend_days")
        reason = (request.POST.get("extend_reason") or "").strip()
        stack = bool(request.POST.get("extend_stack"))
        if not days:
            self.message_user(
                request,
                "Enter the number of comp days in the action bar.",
                level=messages.ERROR,
            )
            return
        if not reason:
            self.message_user(
                request,
                "A reason is required — an unexplained comp is the row an "
                "audit stops on.",
                level=messages.ERROR,
            )
            return
        actor = request.user.get_username()
        granted = 0
        for sub in queryset:
            try:
                preview = services.extend_subscription(
                    subscription=sub,
                    days=int(days),
                    reason=reason,
                    actor=actor,
                    stack=stack,
                    apply=True,
                )
            except ValueError as exc:
                self.message_user(
                    request, f"Subscription {sub.id}: {exc}", level=messages.ERROR
                )
                continue
            granted += 1
            self.log_change(
                request,
                sub,
                f"Comp period {preview.comp_period_id}: {preview.days} day(s) "
                f"of {preview.plan} until {preview.ends_at.isoformat()} "
                f"({reason}).",
            )
            self.message_user(
                request,
                f"Subscription {sub.id}: {preview.plan} until "
                f"{preview.ends_at.isoformat()} (comp period "
                f"{preview.comp_period_id}).",
                level=messages.INFO,
            )
        if granted:
            self.message_user(
                request,
                f"Comp time given on {granted} subscription(s).",
                level=messages.SUCCESS,
            )


@admin.register(CompPeriod)
class CompPeriodAdmin(StapelModelAdmin):
    """Read-mostly: the window is written by the action/command, not by hand."""

    list_display = [
        "subscription",
        "plan",
        "starts_at",
        "ends_at",
        "reason",
        "granted_by",
        "revoked_at",
    ]
    list_filter = ["plan"]
    search_fields = ["subscription__user__email", "reason", "granted_by"]
    readonly_fields = [
        "id",
        "subscription",
        "plan",
        "starts_at",
        "ends_at",
        "reason",
        "granted_by",
        "created_at",
    ]


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(StapelModelAdmin):
    list_display = ["stripe_event_id", "event_type", "processed_at", "received_at"]
    list_filter = ["event_type"]
    search_fields = ["stripe_event_id"]
