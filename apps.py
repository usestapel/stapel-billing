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

        # The erasure protocol, implemented once in stapel-core:
        # gdpr.erasure.requested -> erase -> gdpr.section.erased with a
        # deterministic receipt inside the erase's transaction, the
        # gdpr.owner.probe answer from the same module, and the deprecated
        # user.deleted. What stays ours is erase_subject (gdpr.py).
        #
        # Registering by name is also what stands core's provider bridge
        # down for this section exactly: until 0.19.0 this module carried
        # its own copy of the protocol, and the bridge could only tell they
        # were the same APP, not the same section (gdpr.W012).
        from stapel_core.gdpr import register_gdpr_owner

        from .gdpr import OWNER, SUBJECT_TYPES, erase_subject

        register_gdpr_owner(OWNER, SUBJECT_TYPES, erase_subject)

        # Action subscriptions (in-process in a monolith, bus consumer in
        # microservices — same code, transport chosen by STAPEL_COMM).
        from . import actions  # noqa: F401

        # comm Function providers (billing.check_entitlement /
        # billing.debit). register() is idempotent — ready() may run twice.
        from . import entitlements
        entitlements.register()

        # Configuration that must fail the deploy, not the customer.
        from . import checks  # noqa: F401
