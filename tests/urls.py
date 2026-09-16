from django.urls import path, include

urlpatterns = [
    path("billing/api/", include("stapel_billing.urls_v1")),
    # …and the mount the CONTRACT is emitted at (codegen_urls.py). Without it
    # not one path in the committed docs/schema.json resolved under this
    # urlconf, so nothing in this repository had ever driven the document it
    # ships — the suite was looking somewhere the contract does not describe.
    # Both are kept: the line above is what every existing test addresses, and
    # a mount is cheap. tests/test_contract_wire.py drives the one below.
    path("billing/api/", include("stapel_billing.urls")),
]
