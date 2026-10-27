"""Host settings reach the sessions and the MCP server; host secrets reach only the MCP
server, through a private file outside the run folder (run 3 launch preflight)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from driverkit import Fake, write_task

from stemma.driver.config import HOST_SECRETS, HOST_SETTINGS, DriverConfig, Limits
from stemma.driver.launch import driver_env, host_secrets_path, scrubbed_env
from stemma.driver.runfolder import create_run
from stemma.driver.runner import start
from stemma.register import checks_api

SETTINGS = {k: f"value-of-{k.lower()}" for k in HOST_SETTINGS}
SECRETS = {"ZETARIS_API_KEY": "SECRET-zk-7f3a", "STEMMA_PG_PASSWORD": "SECRET-pg-91c2"}


def test_settings_pass_through_and_secrets_never_do(cfg: DriverConfig, tmp: Path,
                                                    limits: Limits) -> None:
    run = create_run(cfg, write_task(tmp), limits=limits)
    env = driver_env(cfg, run, {**SETTINGS, **SECRETS, "OTHER_HOST_VAR": "x"})
    assert {k: env[k] for k in HOST_SETTINGS} == SETTINGS
    assert "OTHER_HOST_VAR" not in env
    assert not set(env) & set(HOST_SECRETS)
    assert not any(v in " ".join(env.values()) for v in SECRETS.values())
    path = Path(env["STEMMA_HOST_SECRETS"])
    assert path == host_secrets_path(cfg, run)
    assert not str(path).startswith(str(Path(os.path.realpath(run))) + "/")  # outside the run
    assert json.loads(path.read_text()) == SECRETS
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_no_secret_file_without_a_secret(cfg: DriverConfig, tmp: Path, limits: Limits) -> None:
    run = create_run(cfg, write_task(tmp), limits=limits)
    assert "STEMMA_HOST_SECRETS" not in driver_env(cfg, run, {})
    assert not host_secrets_path(cfg, run).exists()


def test_a_run_never_shows_a_secret_to_a_session(cfg: DriverConfig, tmp: Path, fake: Fake,
                                                 limits: Limits,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {**SETTINGS, **SECRETS}.items():
        monkeypatch.setenv(k, v)
    run = create_run(cfg, write_task(tmp), limits=limits)
    start(cfg, run, checks_api, limits)
    calls = fake.calls()
    assert calls
    for c in calls:
        blob = json.dumps(c["env"])
        assert not any(v in blob for v in SECRETS.values()), c["role"]
        assert c["env"]["STEMMA_SEC_IDENTITY"] == SETTINGS["STEMMA_SEC_IDENTITY"]
        assert "STEMMA_HOST_SECRETS" in c["env"]
    # Nothing in the run folder holds a secret either.
    for p in run.rglob("*"):
        if p.is_file() and not p.is_symlink():
            data = p.read_bytes()
            assert not any(v.encode() in data for v in SECRETS.values()), p
    assert not host_secrets_path(cfg, run).exists()  # removed when the driver finished


def test_the_version_probe_writes_no_secret(cfg: DriverConfig,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    env = scrubbed_env(cfg, Path("/nonexistent"))
    assert "STEMMA_HOST_SECRETS" not in env


# --- The MCP server: the only process that loads the secrets ---------------------------

def _secret_file(tmp: Path, mode: int = 0o600) -> Path:
    p = tmp / "secrets.json"
    p.write_text(json.dumps({**SECRETS, "UNRELATED": "x"}))
    os.chmod(p, mode)
    return p


def test_the_server_loads_its_secrets(tmp: Path) -> None:
    from stemma.mcp_server import load_host_secrets

    env: dict[str, str] = {"STEMMA_HOST_SECRETS": str(_secret_file(tmp))}
    assert sorted(load_host_secrets(env)) == sorted(SECRETS)
    assert env["ZETARIS_API_KEY"] == SECRETS["ZETARIS_API_KEY"] and "UNRELATED" not in env


@pytest.mark.parametrize("bad", ["mode", "link"])
def test_the_server_refuses_a_file_that_is_not_private(tmp: Path, bad: str) -> None:
    from stemma.mcp_server import load_host_secrets

    target = _secret_file(tmp, 0o644 if bad == "mode" else 0o600)
    path = target
    if bad == "link":
        path = tmp / "link.json"
        path.symlink_to(target)
    with pytest.raises((RuntimeError, OSError)):
        load_host_secrets({"STEMMA_HOST_SECRETS": str(path)})


def test_query_data_is_served() -> None:
    import anyio

    from stemma.mcp_server import server

    names = {t.name for t in anyio.run(server.list_tools)}
    assert "query_data" in names and "run_analysis" in names


def test_the_read_guard_refuses_the_secrets_file(cfg: DriverConfig, tmp: Path,
                                                 limits: Limits) -> None:
    from stemma.hooks import guards

    run = create_run(cfg, write_task(tmp), limits=limits)
    path = Path(driver_env(cfg, run, SECRETS)["STEMMA_HOST_SECRETS"])
    for role, aid in (("stemma:worker", "a1"), ("stemma:lead", None),
                      ("stemma:claim-checker", None)):
        hook = {"tool_name": "Read", "tool_input": {"file_path": str(path)},
                "agent_type": role, "agent_id": aid, "session_id": "s", "tool_use_id": "t",
                "cwd": str(run)}
        assert not guards.guard_read(hook, run, None).allow
