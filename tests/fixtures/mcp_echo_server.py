#!/usr/bin/env python3
"""A minimal MCP server, for the tests: stdio JSON-RPC, two tools, no deps.

It is the smallest thing that behaves like the real ones — initialize, list,
call — so the client is tested against a conversation rather than a mock.
"""
import json
import sys

TOOLS = [
    {"name": "echo", "description": "Repeat what you are given.",
     "inputSchema": {"type": "object", "required": ["text"],
                     "properties": {"text": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "shout", "description": "Repeat it, louder, and record that it happened.",
     "inputSchema": {"type": "object", "required": ["text"],
                     "properties": {"text": {"type": "string"}}}},
]


def reply(message_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, message_id = message.get("method"), message.get("id")
        if message_id is None:
            continue                                   # a notification
        if method == "initialize":
            reply(message_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                               "serverInfo": {"name": "echo", "version": "1.0"}})
        elif method == "tools/list":
            reply(message_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = message.get("params") or {}
            text = str((params.get("arguments") or {}).get("text") or "")
            if params.get("name") == "shout":
                text = text.upper()
            elif params.get("name") != "echo":
                reply(message_id, {"isError": True,
                                   "content": [{"type": "text", "text": "no such tool"}]})
                continue
            reply(message_id, {"content": [{"type": "text", "text": text}]})
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id,
                                         "error": {"code": -32601, "message": "unknown method"}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
