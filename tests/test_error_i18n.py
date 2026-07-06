"""Localized error catalog (``translations/errors.ru.json``) + provenance gate.

i18n-shipping.md §5 / волна 2. stapel-billing applies the *reference*
``stapel_core.i18n`` catalog contour to the ``errors`` domain — piloted in
stapel-auth (волна 1) — 1:1: the en canon lives in ``errors.py``
(``register_service_errors``), ru ships as a flat
``translations/errors.ru.json`` catalog with a ``translations/.state.json``
provenance sidecar, and :func:`check_translation_catalogs` gates coverage,
staleness, params and byte-stability.

Provenance of the ru values (honest, per §5):

* the bulk is **seeded** from the already-curated ``stapel-translate`` builtin
  fixtures (``origin: seed:stapel-builtin``) — requirement 5 ("clients don't
  spend tokens") met by copying the paid-for corpus, not re-running an LLM;
* the handful of billing-only keys the fixtures do not cover are **machine
  translations** recorded here as :data:`_MACHINE_RU` and written with
  ``origin: llm`` (unreviewed — the gate's W-counter). In a live deployment
  ``translate_catalogs --domain errors --lang ru --llm`` produces these through
  the ``STAPEL_I18N["TRANSLATOR"]`` comm seam; offline they come from this map so
  the reference regenerates deterministically without a live LLM.

Regenerate after adding/changing an error key or a translation:

    STAPEL_REGEN_ERROR_I18N=1 python -m pytest tests/test_error_i18n.py::test_regen

then commit ``translations/errors.ru.json`` + ``translations/.state.json`` +
``docs/errors.{en,ru}.md``. Without the env var the same module is the CI gate.
"""
import io
import os
from pathlib import Path

from django.core.management import call_command

from stapel_core.i18n import (
    check_translation_catalogs,
    source_texts,
    summarize,
    translate_catalog,
)
from stapel_core.i18n.catalogs import load_catalog_file

REPO = Path(__file__).resolve().parent.parent
TRANSLATIONS = REPO / "translations"
DOCS = REPO / "docs"
LANGUAGES = ["en", "ru"]

#: stapel-translate builtin fixtures (the curated seed corpus). Overridable for
#: an out-of-tree checkout via STAPEL_TRANSLATE_FIXTURES.
_FIXTURES = Path(
    os.environ.get(
        "STAPEL_TRANSLATE_FIXTURES",
        REPO.parent / "stapel-translate" / "fixtures" / "builtin",
    )
)

#: Machine translations (origin: llm) of the billing-only error keys the
#: builtin fixtures do not cover. All param-free; edit here + regen when the
#: en changes.
_MACHINE_RU = {
    "error.400.redirect_url_not_configured":
        "redirect_url не передан в запросе, а запасной URL не настроен.",
    "error.403.network_blocked":
        "Запросы из этой сети не разрешены.",
    "error.403.verification_enrollment_required":
        "Требуется регистрация фактора подтверждения.",
}


class _DictTranslator:
    """Offline translator seam — returns fixed machine translations by key."""

    def __init__(self, table):
        self._table = table

    def translate(self, entries, source_language, target_language):
        return {k: self._table[k] for k in entries if k in self._table}


def _seed_from_fixtures(lang: str) -> dict[str, str]:
    """Flat ``{error.*: text}`` seed from the builtin fixtures for *lang*."""
    import json

    path = _FIXTURES / f"{lang}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and k.startswith("error.")
        and isinstance(v, str) and v
    }


def test_regen():
    """Regenerate (env-gated) or assert the ru catalog is a no-op regen (drift)."""
    source = source_texts("errors")

    if os.environ.get("STAPEL_REGEN_ERROR_I18N"):
        result = translate_catalog(
            "errors", "ru", TRANSLATIONS,
            source_texts=source,
            seed=_seed_from_fixtures("ru"),
            seed_label="stapel-builtin",
            llm=True,
            translator=_DictTranslator(_MACHINE_RU),
        )
        assert not result.missing, f"still missing: {result.missing}"
        for lang in LANGUAGES:
            call_command("generate_error_docs", "--lang", lang,
                         "--out", str(DOCS), "--translations", str(TRANSLATIONS),
                         stdout=io.StringIO())
        return

    # Drift gate: regenerating in place (kept, since committed hashes match) must
    # not change the committed catalog.
    before = (TRANSLATIONS / "errors.ru.json").read_bytes()
    translate_catalog(
        "errors", "ru", TRANSLATIONS,
        source_texts=source,
        seed=_seed_from_fixtures("ru"),
        seed_label="stapel-builtin",
        llm=True,
        translator=_DictTranslator(_MACHINE_RU),
    )
    after = (TRANSLATIONS / "errors.ru.json").read_bytes()
    assert before == after, (
        "errors.ru.json drifted — run "
        "STAPEL_REGEN_ERROR_I18N=1 pytest tests/test_error_i18n.py::test_regen"
    )


def test_catalog_gate_green():
    """E: missing / stale / params-mismatch / not-byte-stable — all zero."""
    issues = check_translation_catalogs(
        "errors", TRANSLATIONS,
        source_texts=source_texts("errors"),
        languages=LANGUAGES,
    )
    errors, _warnings = summarize(issues)
    blocking = [i for i in issues if i.level == "error"]
    assert not blocking, "\n".join(f"[{i.code}] {i.message}" for i in blocking)
    assert errors == 0


def test_ru_covers_every_canonical_key():
    source = source_texts("errors")
    catalog = load_catalog_file(TRANSLATIONS / "errors.ru.json")
    missing = [k for k in source if k not in catalog]
    assert not missing, f"ru catalog missing {len(missing)} key(s): {missing[:8]}"


def test_ru_preserves_placeholders():
    """Every ru text keeps exactly the canon's ``{param}`` slots (§3)."""
    from stapel_core.i18n.domains import params_of

    source = source_texts("errors")
    catalog = load_catalog_file(TRANSLATIONS / "errors.ru.json")
    for key, ru in catalog.items():
        if key in source:
            assert set(params_of(ru)) == set(params_of(source[key])), key


def test_error_docs_bilingual_exist():
    for lang in LANGUAGES:
        path = DOCS / f"errors.{lang}.md"
        assert path.is_file(), f"missing {path}"
    assert "_(en)_" not in (DOCS / "errors.ru.md").read_text(), (
        "ru error reference has en-fallback rows — ru catalog is incomplete"
    )
