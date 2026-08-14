"""Django system checks for stapel-billing configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with. A plan's ``entitlements`` map decides who may use
paid features; a key nobody recognises in there is a typo that silently
changes a paywall, and the honest moment to find it is deploy, not the
first customer support ticket.
"""
from django.core import checks


@checks.register(checks.Tags.compatibility)
def check_plan_entitlement_keys(app_configs, **kwargs):
    """E: every entitlement key a configured plan mentions must be declared."""
    from .catalog import PLANS
    from .conf import billing_settings
    from .entitlements import declared_keys

    if billing_settings.ALLOW_UNKNOWN_ENTITLEMENT_KEYS:
        return []
    known = declared_keys()
    errors = []
    for plan in PLANS:
        for key in sorted((plan.entitlements or {})):
            if key in known:
                continue
            errors.append(checks.Error(
                f"STAPEL_BILLING['PLANS'] plan {plan.slug!r} declares the "
                f"entitlement key {key!r}, which is not part of this "
                "deployment's vocabulary. Add it to "
                "STAPEL_BILLING['ENTITLEMENT_KEYS'] if it is real — "
                "otherwise it is a typo and the feature it names is denied "
                "at runtime.",
                id="stapel_billing.E101",
            ))
    return errors


@checks.register(checks.Tags.security)
def check_redirect_allowlist(app_configs, **kwargs):
    """W: the open-redirect guard is only as good as the origins it knows."""
    from .conf import billing_settings
    from .redirects import allowed_origins

    if billing_settings.ALLOW_UNVALIDATED_REDIRECT_URLS:
        return [checks.Warning(
            "STAPEL_BILLING['ALLOW_UNVALIDATED_REDIRECT_URLS'] is on: a "
            "request-supplied checkout/portal redirect URL is forwarded to "
            "the payment provider unchecked, which makes the billing "
            "endpoints an authenticated open redirect.",
            id="stapel_billing.W102",
        )]
    if not allowed_origins():
        return [checks.Warning(
            "No redirect origin is configured, so every request-supplied "
            "checkout/portal redirect URL is rejected. Set "
            "STAPEL_BILLING['REDIRECT_ALLOWED_ORIGINS'], one of the "
            "CHECKOUT_*/PORTAL_RETURN_URL fallbacks, or FRONTEND_URL.",
            id="stapel_billing.W103",
        )]
    return []
