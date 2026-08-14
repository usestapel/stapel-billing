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
    from .conf import allow_unknown_entitlement_keys
    from .entitlements import declared_keys

    if allow_unknown_entitlement_keys():
        # The hatch silences the typo hunt below, so it must speak for
        # itself — its redirect sibling has said so on every boot since
        # BILL-02 (W102), while this one, which decides who gets paid
        # features, used to return quietly. A hatch turned on "for a week"
        # is found by `manage.py check`, not by an audit a year later.
        return [checks.Warning(
            "STAPEL_BILLING['ALLOW_UNKNOWN_ENTITLEMENT_KEYS'] is on: an "
            "entitlement key outside this deployment's vocabulary is "
            "ALLOWED instead of denied, so a typo or a stale caller reads "
            "as an unrestricted feature and no plan check stands between a "
            "user and a paid capability.",
            id="stapel_billing.W101",
        )]
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


@checks.register(checks.Tags.compatibility)
def check_default_plan_slug(app_configs, **kwargs):
    """E: the plan every user falls back to must exist in the catalogue.

    ``Subscription.plan``'s field default is what a user without a
    subscription row — most users of a fresh deployment — is billed and
    gated as. A host whose ladder is starter/growth/scale never has a
    ``"free"`` entry, so that fallback resolves to nothing at all; the
    entitlement answer for it is now a deny, which is safe but denies
    *everyone*. This check makes that a deploy-time refusal instead.
    """
    from .catalog import PLANS_BY_SLUG
    from .conf import allow_unknown_plan_slugs
    from .models import Subscription

    if allow_unknown_plan_slugs():
        return []
    slug = Subscription._meta.get_field("plan").get_default()
    if slug in PLANS_BY_SLUG:
        return []
    return [checks.Error(
        f"The default subscription plan {slug!r} is not in "
        "STAPEL_BILLING['PLANS'], so every user without a subscription row "
        "resolves to a plan this deployment cannot describe. Add the slug to "
        "the plan catalogue (or change the model default in a host migration).",
        id="stapel_billing.E102",
    )]


@checks.register(checks.Tags.security)
def check_redirect_allowlist(app_configs, **kwargs):
    """W: the open-redirect guard is only as good as the origins it knows."""
    from .conf import allow_unvalidated_redirect_urls
    from .redirects import allowed_origins

    if allow_unvalidated_redirect_urls():
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
