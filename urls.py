"""Root URLconf for stapel-billing — v1 canon mount (api-versioning.md §2, §6).

Canon: ``/<mod>/api/v1/...`` — the version segment sits right after ``api/``.
Hosts keep mounting ``include('stapel_billing.urls')`` under their
``.../api/`` prefix; this module contributes the mandatory ``v1/``
sub-prefix. The actual URL set (paths inside unchanged) and the gate
registry live in ``urls_v1.py``; ``GATE_REGISTRY`` is re-exported here.
"""
from django.urls import include, path

from stapel_billing.urls_v1 import GATE_REGISTRY  # noqa: F401  (re-export)

urlpatterns = [
    path('v1/', include('stapel_billing.urls_v1')),
]
