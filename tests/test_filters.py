"""Tests for Jinja template filters (W9)."""


def test_mdbold_renders_bold(app):
    f = app.jinja_env.filters["mdbold"]
    assert str(f("**Deepgram** wins")) == "<strong>Deepgram</strong> wins"


def test_mdbold_escapes_html(app):
    # agent/LLM text must never inject markup
    f = app.jinja_env.filters["mdbold"]
    out = str(f("<script>alert(1)</script> **ok**"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<strong>ok</strong>" in out


def test_mdbold_newlines_to_br(app):
    f = app.jinja_env.filters["mdbold"]
    assert "<br>" in str(f("line1\nline2"))


def test_mdbold_empty(app):
    f = app.jinja_env.filters["mdbold"]
    assert f("") == ""
    assert f(None) == ""
