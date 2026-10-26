"""The small subset of YAML frontmatter that agent files use, read and rewritten exactly.

Agent files start with a `---` block of `key: value` lines. `tools:` is either a comma list
on one line or a block list of `- item` lines. Anything else (a nested mapping, an anchor)
is refused loudly rather than guessed at, so the driver never misreads a role's tools.
"""

from __future__ import annotations

from dataclasses import dataclass

from stemma.driver.errors import AgentFileInvalid

FENCE = "---"  # invariant: the frontmatter delimiter


@dataclass(frozen=True)
class AgentFile:
    lines: tuple[str, ...]  # the frontmatter lines between the fences
    body: str
    fields: dict[str, str | list[str]]

    def scalar(self, key: str) -> str | None:
        v = self.fields.get(key)
        if v is None:
            return None
        if isinstance(v, list):
            raise AgentFileInvalid(f"frontmatter field {key!r} is a list, expected one value")
        return v

    def tools(self) -> list[str]:
        v = self.fields.get("tools")
        if v is None:
            raise AgentFileInvalid("frontmatter has no 'tools' field")
        items = v if isinstance(v, list) else [t for t in v.split(",")]
        out = [_unquote(t.strip()) for t in items]
        if any(not t for t in out):
            raise AgentFileInvalid(f"frontmatter 'tools' has an empty entry: {v!r}")
        return out


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    return s


def parse(text: str, where: str) -> AgentFile:
    lines = text.split("\n")
    if not lines or lines[0].strip() != FENCE:
        raise AgentFileInvalid(f"{where}: does not start with a '---' frontmatter block")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == FENCE)
    except StopIteration:
        raise AgentFileInvalid(f"{where}: frontmatter block is not closed") from None
    fm = lines[1:end]
    body = "\n".join(lines[end + 1 :])
    fields: dict[str, str | list[str]] = {}
    key: str | None = None
    for n, raw in enumerate(fm, start=2):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] in " \t":
            if key is None:
                raise AgentFileInvalid(f"{where}:{n}: indented line before any key")
            item = raw.strip()
            cur = fields[key]
            if item.startswith("- "):
                if isinstance(cur, str) and cur:
                    raise AgentFileInvalid(f"{where}:{n}: list item under scalar {key!r}")
                lst = cur if isinstance(cur, list) else []
                lst.append(_unquote(item[2:].strip()))
                fields[key] = lst
            elif isinstance(cur, str):
                # folded continuation of a scalar (e.g. a long description)
                fields[key] = (cur + " " + item).strip()
            else:
                raise AgentFileInvalid(f"{where}:{n}: unexpected line in list {key!r}")
            continue
        if ":" not in raw:
            raise AgentFileInvalid(f"{where}:{n}: not a 'key: value' line: {raw!r}")
        k, _, v = raw.partition(":")
        key = k.strip()
        if key in fields:
            raise AgentFileInvalid(f"{where}:{n}: duplicate key {key!r}")
        value = v.strip()
        if value in (">", "|", ">-", "|-"):
            value = ""
        if value.startswith(("[", "{", "&", "*")):
            raise AgentFileInvalid(f"{where}:{n}: flow or anchor syntax is not supported")
        fields[key] = _unquote(value)
    return AgentFile(tuple(fm), body, fields)


def set_fields(text: str, where: str, values: dict[str, str]) -> str:
    """Return `text` with top-level scalar keys set (replaced in place, else appended)."""
    agent = parse(text, where)
    fm = list(agent.lines)
    for key, value in values.items():
        idx = [i for i, ln in enumerate(fm) if ln and ln[0] not in " \t#"
               and ln.partition(":")[0].strip() == key]
        line = f"{key}: {value}"
        if idx:
            i = idx[0]
            j = i + 1
            while j < len(fm) and fm[j][:1] in (" ", "\t"):
                j += 1  # drop a folded continuation of the old value
            fm[i:j] = [line]
        else:
            fm.append(line)
    return "\n".join([FENCE, *fm, FENCE]) + "\n" + agent.body
