import json, os, sys
d = json.load(sys.stdin)
with open(os.environ["PROBE_LOG"], "a") as f:
    f.write(json.dumps({"src": "hook:" + sys.argv[1], "input": d}) + "\n")
