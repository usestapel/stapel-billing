"""Package-level public API: __all__ + PEP 562 lazy exports."""

import subprocess
import sys

import stapel_billing


def test_all_lists_the_public_api():
    assert set(stapel_billing.__all__) == {
        "billing_settings",
        "credit",
        "debit",
        "get_provider",
        "PaymentProvider",
        "InsufficientCreditsError",
        "CHECK_ENTITLEMENT",
        "DEBIT",
        "check_entitlement",
    }


def test_lazy_exports_resolve_to_canonical_objects():
    from stapel_billing import entitlements, services
    from stapel_billing.conf import billing_settings
    from stapel_billing.providers.base import PaymentProvider

    assert stapel_billing.billing_settings is billing_settings
    assert stapel_billing.credit is services.credit
    assert stapel_billing.debit is services.debit
    assert stapel_billing.get_provider is services.get_provider
    assert stapel_billing.InsufficientCreditsError is services.InsufficientCreditsError
    assert stapel_billing.PaymentProvider is PaymentProvider
    assert stapel_billing.CHECK_ENTITLEMENT == "billing.check_entitlement"
    assert stapel_billing.DEBIT == "billing.debit"
    assert stapel_billing.check_entitlement is entitlements.check_entitlement


def test_dir_includes_public_api():
    assert set(stapel_billing.__all__) <= set(dir(stapel_billing))


def test_unknown_attribute_raises_attribute_error():
    import pytest

    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        stapel_billing.nope


def test_package_import_pulls_no_django():
    """`import stapel_billing` must work without configured Django settings."""
    code = (
        "import sys; "
        "import stapel_billing; "
        "assert 'django' not in sys.modules, 'django imported eagerly'; "
        "assert 'stapel_billing.services' not in sys.modules; "
        "print(len(stapel_billing.__all__))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "9"
