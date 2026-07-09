"""stapel-billing capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli


def main(argv=None):
    from stapel_billing._codegen import _configure

    _configure()
    from stapel_billing.conf import DEFAULTS
    from stapel_billing.urls import GATE_REGISTRY

    # PAYMENT_PROVIDER is the one CTO-facing axis (payment backend selector).
    # CREDIT_PACKAGES / PLANS are catalog-override extension points (curated
    # in docs/capabilities.meta.json); Stripe keys and redirect URLs are
    # secrets/tuning — neither axes nor extension points.
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/billing/api",
        defaults=DEFAULTS,
        registry=GATE_REGISTRY,
        is_axis=lambda k: k == "PAYMENT_PROVIDER",
        axis_group=axis_group_rules(exact={"PAYMENT_PROVIDER": "billing.provider"}),
        prog="stapel-billing-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
