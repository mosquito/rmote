"""Public imports and templates saved before the package move."""

import pickle
from pathlib import Path

from rmote.filters import TEMPLATE_FILTERS as LEGACY_FILTERS
from rmote.filters import TemplateFilter as LegacyFilter
from rmote.template import Template as LegacyTemplate
from rmote.template import TemplateProgram as LegacyProgram
from rmote.templates import TEMPLATE_FILTERS, Template, TemplateFilter, TemplateProgram, render_template


def test_public_imports_share_classes_and_defaults():
    assert Template is LegacyTemplate
    assert TemplateProgram is LegacyProgram
    assert TemplateFilter is LegacyFilter
    assert TEMPLATE_FILTERS is LEGACY_FILTERS
    assert Template.__module__ == "rmote.templates.engine"
    assert TemplateFilter.__module__ == "rmote.templates.filters"
    assert render_template("{{ name|upper }}", name="web") == "WEB"


def test_unpickle_template_saved_before_package_move(monkeypatch):
    # Captured from the original rmote.template implementation, not rebuilt by
    # this test: both IR nodes and filter restore functions use the old paths.
    payload = (Path(__file__).parent / "data" / "legacy_template.pickle").read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Restoring an existing program must not compile it")

    monkeypatch.setattr(Template, "compile", forbidden)
    restored = pickle.loads(payload)
    assert isinstance(restored, Template)
    assert isinstance(restored.program, TemplateProgram)
    assert restored.render(name="web") == "WEB"
    assert restored.render(name="") == ""
    assert pickle.loads(pickle.dumps(restored)).render(name="db") == "DB"
