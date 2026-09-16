from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "stapel_billing"
    label = 'billing'
    verbose_name = "Stapel Billing"

    def ready(self):
        from stapel_core.gdpr import gdpr_registry
        from .gdpr import BillingGDPRProvider
        gdpr_registry.register(BillingGDPRProvider())

        # Action subscriptions (in-process in a monolith, bus consumer in
        # microservices — same code, transport chosen by STAPEL_COMM).
        from . import actions  # noqa: F401

        # comm Function providers (billing.check_entitlement /
        # billing.debit). register() is idempotent — ready() may run twice.
        from . import entitlements
        entitlements.register()

        # Configuration that must fail the deploy, not the customer.
        from . import checks  # noqa: F401
