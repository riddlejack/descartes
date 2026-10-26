"""`task.json` in, switches and models filled (CONTRACTS sections 7 and 9)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

from stemma.driver.config import AVAILABLE_MODELS, EFFORTS
from stemma.driver.errors import SwitchInvalid, TaskInvalid
from stemma.register import roles

# Switches whose value selects a text fragment, in assembly order (invariant: section 7's
# order). s5_models is a mapping applied through each role's model and effort instead.
FRAGMENT_SWITCHES: Final = (
    "s1_method", "s2_shape", "s3_hypothesis_in_briefs", "s4_orientation", "s6_cross_family",
)
SWITCH_VALUES: Final[dict[str, tuple[str, ...]]] = {
    "s1_method": ("decomposition", "placebo", "none"),
    "s2_shape": ("team", "single_loop"),
    "s3_hypothesis_in_briefs": ("true", "false"),
    "s4_orientation": ("frame_first", "basic_facts", "primary_record"),
    "s6_cross_family": ("off", "during", "final"),
}
# Defaults (invariant: the design's baseline configuration). The design leaves s3 open;
# false keeps briefs hypothesis-free unless a run asks otherwise.
SWITCH_DEFAULTS: Final[dict[str, str]] = {
    "s1_method": "decomposition",
    "s2_shape": "team",
    "s3_hypothesis_in_briefs": "false",
    "s4_orientation": "frame_first",
    "s6_cross_family": "off",
}

DEFAULT_MODEL: Final = "claude-opus-5-5"
# The lead and the three final reviewers run at the highest effort, the rest at high
# (invariant: section 7, s5 defaults).
MAX_EFFORT_ROLES: Final = frozenset({roles.short(roles.LEAD)} |
                                    {roles.short(r) for r in roles.FINAL_REVIEWERS})
ALL_ROLE_NAMES: Final = tuple(sorted(roles.short(r) for r in roles.ALL_ROLES))

_TASK_KEYS: Final = frozenset({"question", "intake", "attachments", "as_of", "switches"})


@dataclass(frozen=True)
class Task:
    question: str
    intake: dict[str, str] | None
    attachments: tuple[Path, ...]  # absolute source paths
    as_of: str | None
    switches_given: dict[str, Any]
    switches: dict[str, Any]  # every switch, defaults filled; s5_models is the role map


def default_models() -> dict[str, dict[str, str]]:
    return {r: {"model": DEFAULT_MODEL, "effort": "max" if r in MAX_EFFORT_ROLES else "high"}
            for r in ALL_ROLE_NAMES}


def _switch_value(name: str, value: Any) -> str:
    if name == "s3_hypothesis_in_briefs":
        if not isinstance(value, bool):
            raise SwitchInvalid(f"{name} must be true or false, got {value!r}")
        return "true" if value else "false"
    if not isinstance(value, str) or value not in SWITCH_VALUES[name]:
        raise SwitchInvalid(f"{name} must be one of {list(SWITCH_VALUES[name])}, got {value!r}")
    return value


def _models(given: Any) -> dict[str, dict[str, str]]:
    out = default_models()
    if given is None:
        return out
    if not isinstance(given, dict):
        raise SwitchInvalid("s5_models must be an object of role -> {model, effort}")
    for role, spec in given.items():
        r = roles.short(str(role))
        if r not in out:
            raise SwitchInvalid(f"s5_models names an unknown role {role!r}")
        if not isinstance(spec, dict) or not set(spec) <= {"model", "effort"} or not spec:
            raise SwitchInvalid(f"s5_models[{role!r}] must be an object with model and/or effort")
        if "model" in spec:
            if spec["model"] not in AVAILABLE_MODELS:
                raise SwitchInvalid(
                    f"s5_models[{role!r}].model must be one of {list(AVAILABLE_MODELS)}")
            out[r]["model"] = spec["model"]
        if "effort" in spec:
            if spec["effort"] not in EFFORTS:
                raise SwitchInvalid(f"s5_models[{role!r}].effort must be one of {list(EFFORTS)}")
            out[r]["effort"] = spec["effort"]
    return out


def fill_switches(given: Any) -> dict[str, Any]:
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise SwitchInvalid("switches must be an object")
    known = set(SWITCH_VALUES) | {"s5_models"}
    unknown = sorted(set(given) - known)
    if unknown:
        raise SwitchInvalid(f"unknown switches: {unknown}")
    out: dict[str, Any] = {}
    for name in FRAGMENT_SWITCHES:
        if name in given:
            v = _switch_value(name, given[name])
        else:
            v = SWITCH_DEFAULTS[name]
        out[name] = v == "true" if name == "s3_hypothesis_in_briefs" else v
    out["s5_models"] = _models(given.get("s5_models"))
    return out


def fragment_value(switches: dict[str, Any], name: str) -> str:
    """The value as it appears in a fragment file name (`s3_hypothesis_in_briefs.true.md`)."""
    v = switches[name]
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_task(path: Path) -> Task:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TaskInvalid(f"task file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise TaskInvalid(f"task file is not JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise TaskInvalid("task.json must be an object")
    unknown = sorted(set(data) - _TASK_KEYS)
    if unknown:
        raise TaskInvalid(f"task.json has unknown keys: {unknown}")
    q = data.get("question")
    if not isinstance(q, str) or not q.strip():
        raise TaskInvalid("task.json 'question' must be a non-empty string")
    intake = data.get("intake")
    if intake is not None and (not isinstance(intake, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in intake.items()
    )):
        raise TaskInvalid("task.json 'intake' must be an object of question -> answer text")
    atts_raw = data.get("attachments", [])
    if not isinstance(atts_raw, list) or not all(isinstance(a, str) for a in atts_raw):
        raise TaskInvalid("task.json 'attachments' must be a list of paths")
    atts: list[Path] = []
    names: set[str] = set()
    for a in atts_raw:
        p = Path(a).expanduser()
        if not p.is_absolute():
            p = path.parent / p
        if not p.is_file():
            raise TaskInvalid(f"attachment is not a file: {a}")
        if p.name in names:
            raise TaskInvalid(f"two attachments share the name {p.name!r}")
        names.add(p.name)
        atts.append(p.resolve())
    as_of = data.get("as_of")
    if as_of is not None:
        if not isinstance(as_of, str) or not _DATE.match(as_of):
            raise TaskInvalid("task.json 'as_of' must be a YYYY-MM-DD date or absent")
        try:
            date.fromisoformat(as_of)
        except ValueError as exc:
            raise TaskInvalid(f"task.json 'as_of' is not a real date: {as_of}") from exc
    given = data.get("switches") or {}
    return Task(q, intake, tuple(atts), as_of, given, fill_switches(given))
