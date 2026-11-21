"""Minimal MCP stdio server with one tool, ``shell``: run a bash command in /testbed of one container (docker exec).

    python box_mcp.py CONTAINER

The blind solver (6a) gets this as its only tool, so its prompt can say "your shell tool" verbatim while every
command runs inside the attempt's own network-less task container.
"""
import json
import subprocess
import sys

CONTAINER = sys.argv[1]
MAX_CALLS, TIMEOUT = 150, 900
TOOL = {"name": "shell", "description": "Run a bash command in /testbed of the task container; returns its output.",
        "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}
calls = 0


def reply(i, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": i, **({"error": error} if error else {"result": result})}
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    meth, i = m.get("method"), m.get("id")
    if i is None:
        continue
    if meth == "initialize":
        reply(i, {"protocolVersion": (m.get("params") or {}).get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "box", "version": "1"}})
    elif meth == "tools/list":
        reply(i, {"tools": [TOOL]})
    elif meth == "tools/call":
        calls += 1
        cmd = ((m.get("params") or {}).get("arguments") or {}).get("command", "")
        if calls > MAX_CALLS:
            reply(i, {"content": [{"type": "text", "text": f"call limit ({MAX_CALLS}) reached"}], "isError": True})
            continue
        try:
            p = subprocess.run(["docker", "exec", "-w", "/testbed", CONTAINER, "bash", "-lc", cmd], capture_output=True,
                               text=True, errors="replace", timeout=TIMEOUT)
            out, err = (p.stdout + p.stderr)[-30000:], p.returncode != 0
        except subprocess.TimeoutExpired:
            out, err = f"command timed out after {TIMEOUT} s", True
        reply(i, {"content": [{"type": "text", "text": out}], "isError": err})
    else:
        reply(i, error={"code": -32601, "message": f"unknown method {meth}"})
