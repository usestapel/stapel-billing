# stapel-billing — contract emission + drift gate (contract-pipeline.md §2-3).
#
# This module emits its OWN contract triad (schema.json + flows.json + errors.json)
# per-module, byte-identical to the monolith aggregate's billing slice, from a
# single-module {billing + core} Django instance mounted at the canonical
# /billing/api/ prefix (see _codegen.py / _codegen_settings.py / codegen_urls.py).
#
# PYTHON must have the module + its deps importable (the workspace venv, or a CI
# venv). The authoritative CI gate is tests/test_contract.py (run under pytest);
# these targets are the dev-loop convenience.
PYTHON ?= python3

.PHONY: contract contract-check

# Emit the contract triad + capabilities.json + llms.txt (the fifth contract
# artifact, stapel_tools.llms_txt) into docs/.
#
# README.md is the SIXTH artifact (tracker #257): assembled by
# stapel_tools.readme from docs/readme.md (the human half — what this module
# is, how to think about it) plus everything emitted above. Badges, version,
# surface counts and doc links are generated, so a release cannot leave them
# behind. Edit docs/readme.md; never README.md.
# LLMS_BUDGET is raised from the 4000-token default DELIBERATELY (0.8.0): the
# module's callable surface grew by a third — credit lots, hold/capture/release
# and three scheduled workers — and the errors section (~700 tokens) is owned by
# stapel-core, so a key added upstream would otherwise turn this release red for
# reasons nothing in this repo can fix. Trim intents before raising it again.
#
# Raised again in 0.11.0, after trimming: the audit wave added twelve callable
# entries — non-provider plan grants (3), debts and partial charges (2), the
# read-only affordability check, the clawback primitive and the three refund
# webhooks that drive it, plus the grant worker. Every one of them is a
# mechanism a consumer must call instead of writing its own, which is exactly
# what this file exists to tell an agent; the intents were cut to one or two
# sentences first, and the remaining ~950 tokens are the surface itself.
#
# Raised again in 0.12.0, by the two entries the account merge adds
# (`merge_wallets`, `merge_idempotency_key`). Both are mechanisms a consumer
# must call instead of writing its own — moving credits by hand is how an
# expiring bundle silently becomes non-expiring cash — so neither belongs
# outside this file. Their intents were written short before the ceiling moved.
LLMS_BUDGET ?= 5600

contract:
	$(PYTHON) -m stapel_billing._codegen --out docs
	$(PYTHON) -m stapel_billing._capabilities --out docs
	$(PYTHON) -m stapel_tools.llms_txt . --budget $(LLMS_BUDGET)
	$(PYTHON) -m stapel_tools.readme .

# Drift gate: regenerate into a temp dir and diff against the committed docs/*.json
# (mirrors the monolith's `make codegen-check` and the frontend's `gen:*:check`).
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_billing._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_billing._capabilities --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_tools.llms_txt . --budget $(LLMS_BUDGET) --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json capabilities.json llms.txt; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	$(PYTHON) -m stapel_tools.readme . --check || rc=1; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors,capabilities,llms.txt} + README.md up to date"; fi; \
	exit $$rc


.PHONY: migration-lint

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict
