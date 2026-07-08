"""Canonical-prefix URLconf for contract emission (contract-pipeline.md §2).

The monolith aggregate mounts billing at its canonical public API prefix
(svc-app/core/urls.py: ``path("billing/api/", include("stapel_billing.urls"))``,
no sibling co-mount — unlike auth+gdpr, billing has no co-mounted module under
the same prefix). ``stapel_billing.tests.urls`` already mounts at this same
prefix for the pytest suite, so this file exists to mirror auth's harness shape
(one dedicated codegen urlconf, independent of the test urlconf) rather than to
fix a mount-prefix bug.

This URLconf reproduces the monolith mount **exactly**, so drf-spectacular emits
``/billing/api/...`` paths (and the matching ``billing_api_*`` operationIds) and
``generate_flow_docs`` resolves flow endpoints to the same.
"""
from django.conf.urls import include
from django.urls import path

urlpatterns = [
    path("billing/api/", include("stapel_billing.urls")),
]
