from stapel_core.gdpr import GDPRProvider


class BillingGDPRProvider(GDPRProvider):
    section = 'billing'

    def export(self, user_id: int) -> dict:
        from .models import CreditHold, CreditLot, Subscription, Transaction, Wallet

        try:
            wallet = Wallet.objects.get(user_id=user_id)
            wallet_data = {
                'balance':   str(wallet.balance),
                'currency':  wallet.currency,
                'created_at': wallet.created_at.isoformat(),
            }
            transactions = list(Transaction.objects.filter(wallet=wallet).values(
                'type', 'amount_cents', 'credits_delta', 'balance_after', 'description', 'created_at',
            ))
            # Where the balance came from and when it dies is part of the
            # answer to "what do you hold about me" — a single number is not.
            lots = list(CreditLot.objects.filter(wallet=wallet).values(
                'source', 'credits_initial', 'credits_remaining', 'expires_at', 'created_at',
            ))
            holds = list(CreditHold.objects.filter(wallet=wallet).values(
                'credits', 'type', 'description', 'status', 'expires_at',
                'resolved_at', 'created_at',
            ))
        except Wallet.DoesNotExist:
            wallet_data   = {}
            transactions  = []
            lots          = []
            holds         = []

        try:
            sub = Subscription.objects.get(user_id=user_id)
            sub_data = {
                'plan':                 sub.plan,
                'status':               sub.status,
                'current_period_start': sub.current_period_start.isoformat() if sub.current_period_start else None,
                'current_period_end':   sub.current_period_end.isoformat() if sub.current_period_end else None,
                'cancelled_at':         sub.cancelled_at.isoformat() if sub.cancelled_at else None,
            }
        except Subscription.DoesNotExist:
            sub_data = {}

        return {
            'wallet':       wallet_data,
            'credit_lots':  _serialize_dates(lots),
            'credit_holds': _serialize_dates(holds),
            'transactions': _serialize_dates(transactions),
            'subscription': sub_data,
        }

    def delete(self, user_id: int) -> None:
        from .models import Subscription, Wallet

        # Stripe customer/subscription IDs are deleted from our DB.
        # Financial transaction records are anonymised (not deleted) for legal retention.
        try:
            sub = Subscription.objects.get(user_id=user_id)
            sub.stripe_subscription_id = ''
            sub.stripe_customer_id     = ''
            sub.save(update_fields=['stripe_subscription_id', 'stripe_customer_id'])
        except Subscription.DoesNotExist:
            pass

        try:
            Wallet.objects.filter(user_id=user_id).update(user_id=None)  # anonymise
        except Exception:
            pass

    def anonymize(self, user_id: int) -> None:
        # Transaction records must be kept for legal retention (POST-MVP: 10 years).
        # Stripe IDs are cleared in delete(); transaction rows have no direct PII.
        pass


def _serialize_dates(rows: list[dict]) -> list[dict]:
    return [
        {k: v.isoformat() if hasattr(v, 'isoformat') else v for k, v in row.items()}
        for row in rows
    ]
