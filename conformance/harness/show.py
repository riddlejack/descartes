import json, sys
d = sys.argv[1]
for l in open(d + "/out.jsonl"):
    m = json.loads(l)
    t = m.get("type")
    if t == "system" and m.get("subtype") == "init":
        print("INIT tools:", [x for x in m["tools"]])
        print("INIT agents:", m.get("agents")); print("INIT mcp:", m.get("mcp_servers")); print("INIT model:", m.get("model"), "plugins:", [p["name"] for p in m.get("plugins", [])]); print("INIT skills:", m.get("skills"))
    elif t == "assistant":
        for c in m["message"]["content"]:
            if c["type"] == "text": print("A:", c["text"][:1500])
            elif c["type"] == "tool_use": print("TOOL_USE:", c["name"], json.dumps(c["input"])[:600], "parent=", m.get("parent_tool_use_id"))
    elif t == "user":
        cs = m["message"]["content"]
        if isinstance(cs, list):
            for c in cs:
                if c.get("type") == "tool_result":
                    cc = c.get("content"); print("RESULT:", json.dumps(cc)[:700], "parent=", m.get("parent_tool_use_id"))
    elif t == "result":
        print("FINAL:", json.dumps({k: m.get(k) for k in ("result", "terminal_reason", "permission_denials", "subagent_stats", "modelUsage")})[:3000])
