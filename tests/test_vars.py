"""R6 / D-Q3: `{variable}` manifest templating — strings only, `{{ }}`
escapes, M-413 for unbound references naming variable + dotted path."""

from __future__ import annotations

import pytest

from loom.compiler import compile_manifest
from loom.diagnostics import CompileError
from loom.manifest import load_manifest

TPL = (
    "loom: 0\n"
    "title: \"{title}\"\n"
    "sources:\n"
    "  main: { path: \"wavs/{title}.wav\", kind: bed, layout: \"{layout}\" }\n"
    "elements:\n  bed: { from: main }\n"
    "targets:\n  - { format: iamf, out: \"dist/{title}.iamf\" }\n"
)


def test_substitution_compiles(project):
    mf = project(TPL, {"wavs/ep01.wav": 2}, name="tpl.yaml")
    m = load_manifest(mf, variables={"title": "ep01", "layout": "stereo"})
    plan = compile_manifest(m)
    assert m.title == "ep01"
    assert plan.targets[0].out == "dist/ep01.iamf"
    assert plan.sources["main"]["path"] == "wavs/ep01.wav"


def test_unbound_variable_is_m413_with_path(project):
    mf = project(TPL, {"wavs/ep01.wav": 2}, name="tpl.yaml")
    with pytest.raises(CompileError) as ei:
        load_manifest(mf, variables={"title": "ep01"})   # layout unbound
    codes = ei.value.codes()
    assert codes == ["M-413"]
    d = ei.value.diagnostics[0]
    assert "{layout}" in d.message
    assert "sources.main.layout" in d.path


def test_bare_template_compile_names_every_unbound(project):
    mf = project(TPL, {"wavs/ep01.wav": 2}, name="tpl.yaml")
    with pytest.raises(CompileError) as ei:
        load_manifest(mf)
    assert set(ei.value.codes()) == {"M-413"}
    assert len(ei.value.diagnostics) >= 3   # title, path, layout, out


def test_brace_escape(project):
    text = TPL.replace("title: \"{title}\"", "title: \"{title} {{literal}}\"")
    mf = project(text, {"wavs/ep01.wav": 2}, name="tpl.yaml")
    m = load_manifest(mf, variables={"title": "ep01", "layout": "stereo"})
    assert m.title == "ep01 {literal}"


def test_non_string_scalars_untouched(project):
    text = (
        "loom: 0\ntitle: \"{title}\"\n"
        "sources:\n  main: { path: main.wav, kind: bed, layout: stereo }\n"
        "elements:\n  bed: { from: main }\n"
        "policy:\n  loudness: { normalize: -16 }\n"
        "targets:\n  - { format: iamf, out: dist/a.iamf }\n"
    )
    mf = project(text, {"main.wav": 2}, name="tpl.yaml")
    m = load_manifest(mf, variables={"title": "x"})
    assert m.policy.normalize == -16.0   # float stayed a float


def test_unused_bindings_are_legal(project):
    mf = project(TPL, {"wavs/ep01.wav": 2}, name="tpl.yaml")
    m = load_manifest(mf, variables={"title": "ep01", "layout": "stereo",
                                     "unused_column": "whatever"})
    assert m.title == "ep01"


def test_cli_var_parsing():
    from loom.cli import _parse_vars
    assert _parse_vars(["a=1", "b=x=y"]) == {"a": "1", "b": "x=y"}
    with pytest.raises(SystemExit):
        _parse_vars(["novalue"])


def test_cli_compile_with_vars(project, capsys):
    from loom.cli import main
    mf = project(TPL, {"wavs/ep01.wav": 2}, name="tpl.yaml")
    rc = main(["compile", str(mf), "--var", "title=ep01",
               "--var", "layout=stereo"])
    assert rc == 0
    assert "dist/ep01.iamf" in capsys.readouterr().out
