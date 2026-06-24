from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "stapel_billing"
    label = 'billing'
    verbose_name = "Iron Billing"

    def ready(self):
        from stapel_core.gdpr import gdpr_registry
        from .gdpr import BillingGDPRProvider
        gdpr_registry.register(BillingGDPRProvider())
