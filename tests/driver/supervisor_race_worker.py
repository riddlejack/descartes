"""Worker for the two-supervisor race test (run in spawned processes).

Not a test module: pytest does not collect it; the race test imports it by name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stemma.driver.config import DriverConfig
from stemma.driver.errors import DriverBusy
from stemma.driver.runner import run_to_end
from stemma.register import checks_api


def run(cfg_fields: dict[str, str], run_dir: str, me: int, barrier: Any, results: Any) -> None:
    cfg = DriverConfig(runs_dir=Path(cfg_fields["runs_dir"]),
                       claude_bin=Path(cfg_fields["claude_bin"]),
                       claude_config=Path(cfg_fields["claude_config"]),
                       python=Path(cfg_fields["python"]),
                       plugin_src=Path(cfg_fields["plugin_src"]))
    barrier.wait()  # both supervisors are released at once
    try:
        results.put((me, "code", run_to_end(cfg, Path(run_dir), resume_first=True,
                                            api=checks_api)))
    except DriverBusy as exc:
        results.put((me, "busy", str(exc)))
