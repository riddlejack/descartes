"""Why the driver launches the CLI itself rather than through claude-agent-sdk 0.2.174.

Shown against the fake binary: the SDK passes the parent's whole environment to the child
(only CLAUDECODE removed; `options.env` can add or override keys, never remove one), and
it always adds flags the probe-proven launch does not use (`--input-format stream-json`,
`--system-prompt ""`), with the prompt sent on stdin rather than `-p <prompt>`.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import anyio
import pytest
from driverkit import Fake

sdk = pytest.importorskip("claude_agent_sdk")


def test_sdk_leaks_parent_environment(fake: Fake, tmp: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    assert sdk.__version__ == "0.2.174"
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    monkeypatch.setenv("DESKTOP_APP_SENTINEL", "inherited")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    (tmp / "config").mkdir(exist_ok=True)
    fake.configure({})

    async def go() -> None:
        opts = sdk.ClaudeAgentOptions(cli_path=str(fake.path), cwd=str(tmp),
                                      env={"CLAUDE_CONFIG_DIR": str(tmp / "config")})
        with anyio.fail_after(5):  # resource limit: the fake writes its record at once
            async for _ in sdk.query(prompt="hi", options=opts):
                pass

    # The fake does not speak the SDK's control protocol, so the query itself fails.
    with contextlib.suppress(Exception):
        anyio.run(go)
    calls = fake.calls()
    assert calls, "the SDK did not start the fake binary"
    env, argv = calls[0]["env"], calls[0]["argv"]
    assert env["DESKTOP_APP_SENTINEL"] == "inherited"
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
    assert env["CLAUDE_CODE_ENTRYPOINT"] == "sdk-py"
    assert "--input-format" in argv and "-p" not in argv
    assert argv[argv.index("--system-prompt") + 1] == ""
