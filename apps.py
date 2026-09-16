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

        # `grant_credits` ACTS: it creates credits out of nothing. Declared
        # operator-only HERE, beside the Wallet.Meta that introduces it, so
        # the declaration and the definition are read together — and so a
        # group fixture naming it is refused by stapel_core's importer rather
        # than granting it to every staff member, which is what the Staff
        # group actually is (the JWT mirror enrols them on sight).
        #
        # Guarded: a host on stapel-core < 0.75.0 simply does not get the
        # refusal, and must keep grant_credits out of its fixture by hand.
        try:
            from stapel_core.django.groups import register_operator_only_permission
        except ImportError:  # pragma: no cover — older stapel-core
            pass
        else:
            register_operator_only_permission("billing.grant_credits")

        # Configuration that must fail the deploy, not the customer.
        from . import checks  # noqa: F401
