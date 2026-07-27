"""Minimal MCP client for the SigNoz MCP server (streamable-HTTP transport).

M6 owns `preflight/mcp.py`. This lives under `scripts/` so M5 does not race it;
if `preflight.mcp` ever lands with an equivalent client, this can be deleted and
`scripts/signoz_apply.py` re-pointed at it.

Why hand-rolled rather than the `mcp` SDK: the apply path needs exactly three
things -- initialize, tools/list, tools/call -- and the SigNoz server is
stateless (it returns no `Mcp-Session-Id`, so there is no session to manage).
A ~100-line client over httpx is less surface than a dependency, and it makes
the wire format visible when a payload gets rejected, which is most of the work
in M5.

Transport notes, verified against SigNoz MCP `main-9445cf1`:

* Endpoint is `POST /mcp`. It negotiates on `Accept`: send
  `application/json, text/event-stream` and it answers with whichever it likes.
  It usually answers `application/json`, but it is free to stream SSE -- a
  client that only parses JSON hangs or dies on the `event:` prefix, which is
  why `_parse` handles both.
* Auth is the same `SIGNOZ-API-KEY` header the query API uses.
* `initialize` must precede `tools/call` on a fresh connection.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_URL = "http://localhost:8000/mcp"


class MCPError(RuntimeError):
    """A JSON-RPC error, or a tool result flagged `isError`."""


class MCPClient:
    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 60.0,
    ):
        self.url = url or os.environ.get("SIGNOZ_MCP_URL") or DEFAULT_URL
        key = api_key or os.environ.get("SIGNOZ_API_KEY") or ""
        if not key:
            raise MCPError(
                "SIGNOZ_API_KEY is not set. Run `set -a && . ./.env && set +a` first, "
                "or mint a key with `make bootstrap`."
            )
        self._id = 0
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                # Both, so the server may pick either and we can parse either.
                "Accept": "application/json, text/event-stream",
                "SIGNOZ-API-KEY": key,
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            },
        )
        self._initialized = False

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MCPClient":
        self.initialize()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def initialize(self) -> dict[str, Any]:
        if self._initialized:
            return {}
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "preflight-signoz-apply", "version": "0.1.0"},
            },
        )
        self._initialized = True
        return result

    # -- calls -------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        return self._rpc("tools/list", {}).get("tools", [])

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Invoke one tool and return its parsed payload.

        MCP wraps results in `content[]`. SigNoz returns a single `text` part
        that is itself JSON, so parse it when it parses and hand back the raw
        string when it doesn't -- some tools answer in prose.
        """
        self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        parts = result.get("content") or []
        texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
        blob = "\n".join(t for t in texts if t)
        if result.get("isError"):
            raise MCPError(f"{name} failed: {blob[:2000]}")
        # `structuredContent` is the typed twin of the text part when present.
        if "structuredContent" in result:
            return result["structuredContent"]
        try:
            return json.loads(blob)
        except (ValueError, TypeError):
            return blob

    # -- wire --------------------------------------------------------------

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        resp = self._client.post(
            self.url,
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
        )
        if resp.status_code >= 400:
            raise MCPError(f"{method} -> HTTP {resp.status_code}: {resp.text[:800]}")
        payload = _parse(resp.text)
        if "error" in payload:
            raise MCPError(f"{method} -> {json.dumps(payload['error'])[:2000]}")
        return payload.get("result", {})


def _parse(body: str) -> dict[str, Any]:
    """Parse either a plain JSON body or an SSE stream carrying one message."""
    text = body.strip()
    if text.startswith("{"):
        return json.loads(text)
    # SSE: take the last `data:` line, which is the JSON-RPC response.
    data_lines = [
        line[len("data:") :].strip()
        for line in text.splitlines()
        if line.startswith("data:")
    ]
    if not data_lines:
        raise MCPError(f"unparseable MCP response: {body[:500]}")
    return json.loads(data_lines[-1])


def main() -> int:
    """Probe helper: `python scripts/mcp_client.py <tool> '<json args>'`."""
    import sys

    args = sys.argv[1:]
    with MCPClient() as mcp:
        if not args or args[0] == "list":
            for tool in mcp.list_tools():
                print(f"{tool['name']}")
            return 0
        if args[0] == "schema":
            tools = {t["name"]: t for t in mcp.list_tools()}
            print(json.dumps(tools[args[1]].get("inputSchema", {}), indent=1))
            return 0
        payload = json.loads(args[1]) if len(args) > 1 else {}
        print(json.dumps(mcp.call(args[0], payload), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
