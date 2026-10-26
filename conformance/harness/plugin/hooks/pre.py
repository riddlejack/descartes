import json, os, sys
d = json.load(sys.stdin)
with open(os.environ["PROBE_LOG"], "a") as f:
    f.write(json.dumps({"src": "hook:PreToolUse", "input": d, "env_keys": sorted(k for k in os.environ if k.startswith(("CLAUDE", "STEMMA", "PROBE")))}) + "\n")
name = d.get("tool_name", "")
ti = dict(d.get("tool_input") or {})
out = None
if name.endswith("__echo"):
    ti["injected_caller"] = d.get("agent_type", "main")
    ti["text"] = ti.get("text", "") + "+hooked"
    out = ti
elif name == "Agent":
    for k in ("model", "effort"):
        ti.pop(k, None)
    out = ti
elif name == "Bash" and "ORIGINAL" in ti.get("command", ""):
    ti["command"] = ti["command"].replace("ORIGINAL", "REWRITTEN")
    out = ti
if out is not None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": out}}))
