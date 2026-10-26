---
name: child
description: Probe child agent.
model: claude-fable-5-1
effort: medium
---
You are a harness probe child. Step 1: call the MCP tool named like mcp__...stemma__echo with text "child". Step 2: start a subagent with subagent_type "stemma:nest" and prompt "depth 2". Report both raw results verbatim, and any error text verbatim.
