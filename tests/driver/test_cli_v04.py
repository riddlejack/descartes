"""The `stemma` command line: the product commands (report, verify, new) are handed to
builder W3's modules or refused loudly; the driver's own commands still parse."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from stemma.driver import __main__ as cli
from stemma.driver.__main__ import PRODUCT, main


@pytest.mark.parametrize("cmd,args", [("report", ["runs/r1", "out"]), ("verify", ["runs/r1"]),
                                      ("new", ["Should the invented bakery expand?"])])
def test_missing_product_module_is_refused(monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str],
                                           cmd: str, args: list[str]) -> None:
    # None in sys.modules makes the import fail whether or not the module is installed.
    monkeypatch.setitem(sys.modules, PRODUCT[cmd], None)
    assert main([cmd, *args]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == f"stemma {cmd} is not installed in this build: {PRODUCT[cmd]} is " \
                      f"missing\n"


def test_missing_parent_package_is_refused(monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setitem(sys.modules, "stemma.report", None)
    monkeypatch.delitem(sys.modules, "stemma.report.verify", raising=False)
    assert main(["verify", "runs/r1"]) == 2
    assert "stemma verify is not installed in this build: stemma.report.verify is missing" \
        in capsys.readouterr().err


def test_missing_dependency_is_named_not_called_absent(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(name: str) -> Any:
        raise ModuleNotFoundError("No module named 'markdown_it'", name="markdown_it")

    monkeypatch.setattr(cli.importlib, "import_module", broken)
    assert main(["report", "runs/r1", "out"]) == 2
    assert capsys.readouterr().err == ("stemma report cannot start: stemma.report.site needs "
                                       "markdown_it, which is missing\n")


def test_module_without_main_or_exit_code_is_refused(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setitem(sys.modules, PRODUCT["verify"], types.ModuleType(PRODUCT["verify"]))
    assert main(["verify", "runs/r1"]) == 2
    assert "has no main()" in capsys.readouterr().err
    mod = types.ModuleType(PRODUCT["verify"])
    mod.main = lambda argv: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, PRODUCT["verify"], mod)
    assert main(["verify", "runs/r1"]) == 2
    assert "returned None, not an exit code" in capsys.readouterr().err


@pytest.mark.parametrize("cmd", sorted(PRODUCT))
def test_product_command_gets_its_arguments_unparsed(monkeypatch: pytest.MonkeyPatch,
                                                     cmd: str) -> None:
    seen: list[list[str]] = []
    mod = types.ModuleType(PRODUCT[cmd])

    def fake_main(argv: list[str]) -> int:
        seen.append(argv)
        return 7

    mod.main = fake_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, PRODUCT[cmd], mod)
    rest = ["--answers", "a.json", "Q?", "--json", "-h"]
    assert main([cmd, *rest]) == 7
    # After the global option too, through argparse.
    assert main(["--plugin-src", "p", cmd, "x", "y"]) == 7
    assert seen == [rest, ["x", "y"]]


def test_existing_commands_still_parse(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(cli.DriverConfig, "from_env", staticmethod(lambda src: f"cfg:{src}"))

    def create_run(cfg: Any, task: Path, stop_after: str | None) -> Path:
        calls.append(("create", (cfg, task, stop_after)))
        return tmp_path / "run"

    def run_to_end(cfg: Any, run_dir: Path, resume_first: bool) -> int:
        calls.append(("run", (cfg, run_dir, resume_first)))
        return 0

    monkeypatch.setattr(cli, "create_run", create_run)
    monkeypatch.setattr(cli, "run_to_end", run_to_end)
    monkeypatch.setattr(cli, "show_status", lambda cfg, d: {"run": str(d)})
    monkeypatch.setattr(cli, "_attach", lambda rid, f: calls.append(("attach", (rid, f))) or 0)

    assert main(["start", "--frame-only", "t.json"]) == 0
    assert main(["--plugin-src", "p", "start", "t.json"]) == 0
    assert main(["resume", "r"]) == 0
    assert main(["status", "r"]) == 0
    assert main(["attach", "20261026-070412-2b3c98", "f.pdf"]) == 0
    assert calls == [
        ("create", ("cfg:None", Path("t.json"), "frame")),
        ("run", ("cfg:None", tmp_path / "run", False)),
        ("create", ("cfg:p", Path("t.json"), None)),
        ("run", ("cfg:p", tmp_path / "run", False)),
        ("run", ("cfg:None", Path("r"), True)),
        ("attach", ("20261026-070412-2b3c98", Path("f.pdf"))),
    ]
    assert '"run": "r"' in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2
