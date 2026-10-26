import json, os, sys, time
d = json.load(sys.stdin)
cmd = (d.get("tool_input") or {}).get("command", "")
with open(os.environ["PROBE_LOG"], "a") as f:
    f.write(json.dumps({"guard": sys.argv[1], "cmd": cmd}) + "\n")
if "CRASH" in cmd:
    raise RuntimeError("guard crashed on purpose")
if "SLOW" in cmd:
    time.sleep(20)
if "GARBAGE" in cmd:
    print("{not json")
