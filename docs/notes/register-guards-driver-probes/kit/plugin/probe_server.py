import json, os, sys
from typing import Any
from mcp.server.mcpserver import MCPServer

LOG = os.environ.get("PROBE_LOG", "/dev/null")

def _ctx(c):
    out = {}
    try:
        rc = getattr(c, "request_context", None)
        out["rc_type"] = type(rc).__name__
        for k in ("meta", "request_id", "headers"):
            v = getattr(rc, k, None)
            out[k] = repr(v)[:800]
        out["dir"] = [a for a in dir(rc) if not a.startswith("_")][:40]
    except Exception as e:
        out["err"] = repr(e)
    return out

class Probe(MCPServer):
    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        with open(LOG, "a") as f:
            f.write(json.dumps({"src": "mcp", "tool": name, "raw_args": arguments, "env_run": os.environ.get("STEMMA_RUN_DIR"), "ctx": _ctx(context)}) + "\n")
        return await super().call_tool(name, arguments, context)

server = Probe("stemma")

@server.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return "echo:" + text

if __name__ == "__main__":
    server.run()
