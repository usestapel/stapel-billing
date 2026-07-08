"""stapel-billing contract-emission harness (contract-pipeline.md §2-3).

Emits the module's own contract triad into ``docs/`` from a single-module
``{billing + core}`` Django instance mounted at the canonical ``billing/api/``
prefix:

  docs/schema.json   drf-spectacular OpenAPI, this module only, canonical prefix
  docs/flows.json    generate_flow_docs machine artifact (empty — no @flow_step
                     annotations in this module yet)
  docs/errors.json   generate_error_keys registry (already the etalon)

Copied from stapel-auth's reference implementation (contract-pipeline.md §7 Wave
1 recipe). The *mechanism* is stapel_tools.codegen (unchanged, shared); this file
is the thin per-module *config* that wires the module's settings + canonical
mount into it.

Usage:
    python -m stapel_billing._codegen --out docs        # `make contract`
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _configure() -> None:
    """Configure + boot the single-module Django instance for emission."""
    from django.conf import settings

    if not settings.configured:
        from stapel_billing._codegen_settings import settings_kwargs

        settings.configure(
            **settings_kwargs(root_urlconf="stapel_billing.codegen_urls", contract=True)
        )

    import django

    django.setup()

    # drf-spectacular froze its settings singleton at import time (before this
    # harness ran configure()), so it is on drf defaults — the same state the
    # monolith emits under. The one knob to force is SCHEMA_PATH_PREFIX: left None,
    # drf derives the operationId prefix from the common path of all endpoints —
    # "/" across the multi-module monolith (operationIds keep the mount segment,
    # billing_api_*), but "/billing/api" in a single-module harness (which would
    # strip it to bare wallet_retrieve). Pin it to the monolith's common prefix so
    # the operationIds are byte-identical; SCHEMA_PATH_PREFIX_TRIM stays False
    # (default) so the path *keys* keep /billing/api/ on both sides.
    from drf_spectacular.settings import spectacular_settings

    from stapel_billing._codegen_settings import CODEGEN_SCHEMA_PATH_PREFIX

    spectacular_settings.SCHEMA_PATH_PREFIX = CODEGEN_SCHEMA_PATH_PREFIX

    # In the monolith, *some* module's urls.py calls
    # stapel_core.django.openapi.swagger.get_swagger_urls() (auth+gdpr both do,
    # via get_dev_urls / get_app_swagger_urls), which as an import-time side
    # effect registers a drf-spectacular OpenApiAuthenticationExtension for
    # JWTCookieAuthentication ("JWTCookieAuth" apiKey-in-cookie scheme). That
    # registration is *process-global*: once registered, drf-spectacular adds
    # the resulting `security` block to every operation in the same schema
    # generation pass, including billing's — even though billing itself never
    # calls get_swagger_urls. Auth's single-module harness gets this "for free"
    # because it co-mounts stapel_gdpr.urls (which calls get_app_swagger_urls)
    # at the same auth/api/ prefix for unrelated (real endpoint) reasons.
    # Billing has no such sibling to co-mount — mounting one under billing/api/
    # would incorrectly add its paths to this module's own schema — so the
    # harness registers the extension directly, reproducing the monolith's
    # process-global side effect without borrowing any sibling's endpoints.
    # Without this, every operation is missing its `security` block relative to
    # the monolith slice (drf-spectacular logs "could not resolve authenticator"
    # and silently drops the block instead).
    from stapel_core.django.openapi.swagger import _register_jwt_auth_extension

    _register_jwt_auth_extension()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stapel-billing-contract",
        description="Emit this module's contract triad (schema.json + flows.json "
        "+ errors.json) into --out, canonical /billing/api/ prefix.",
    )
    parser.add_argument(
        "--out",
        default="docs",
        help="Output directory for the triad (default: docs).",
    )
    args = parser.parse_args(argv)

    _configure()

    # Reuse the shared mechanism's byte-stable emitters (contract-pipeline.md §2:
    # "the single-module harness already exists"). We call the three triad
    # emitters directly rather than generate(), which would also emit the
    # features/ Gherkin bundle — a separate concern.
    from stapel_tools.codegen import emit_errors, emit_flows, emit_schema

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = emit_schema(out / "schema.json")
    flows = emit_flows(out / "flows.json")
    errors = emit_errors(out / "errors.json")

    print(
        f"stapel-billing contract: {paths} paths, {flows} flows, {errors} error keys "
        f"→ {out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
