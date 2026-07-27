"""Client for the SigNoz MCP server, and the glue that hands its tools to a model.

M5 shipped dashboards and alerts through MCP with a hand-rolled client under
`scripts/mcp_client.py`; DECISIONS.md recorded that M6 owns `preflight/mcp.py`
and that the scripts copy should collapse into it. That is what this is -- the
transport below *is* M5's, moved rather than rewritten, so there is one wire
implementation in the repo. `scripts/mcp_client.py` is now a re-export shim
because `scripts/signoz_apply.py` imports from it and is frozen.

What M6 adds on top is the second half of the file: turning the server's
advertised tool schemas into Anthropic tool definitions, so a model can drive
SigNoz directly instead of a human transcribing query bodies.

Transport notes, verified against SigNoz MCP `main-9445cf1`:

* Endpoint is `POST /mcp`. It negotiates on `Accept`: send
  `application/json, text/event-stream` and it answers with whichever it likes.
  It usually answers `application/json`, but it is free to stream SSE -- a
  client that only parses JSON hangs or dies on the `event:` prefix, which is
  why `_parse` handles both. (A blocking `curl` with no `Accept` header hangs
  outright: the server picks SSE and never closes the stream.)
* Auth is the same `SIGNOZ-API-KEY` header the query API uses.
* `initialize` must precede `tools/call` on a fresh connection.
* The server is stateless -- no `Mcp-Session-Id` comes back, so there is no
  session to keep alive and no reason to reach for the `mcp` SDK.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable

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
        client_name: str = "preflight",
    ):
        self.url = url or os.environ.get("SIGNOZ_MCP_URL") or DEFAULT_URL
        key = api_key or os.environ.get("SIGNOZ_API_KEY") or ""
        if not key:
            raise MCPError(
                "SIGNOZ_API_KEY is not set. Run `set -a && . ./.env && set +a` first, "
                "or mint a key with `make bootstrap`."
            )
        self._id = 0
        self._client_name = client_name
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
        self._tool_cache: list[dict[str, Any]] | None = None

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
                "clientInfo": {"name": self._client_name, "version": "0.1.0"},
            },
        )
        self._initialized = True
        return result

    # -- calls -------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        """Every tool the server advertises. Cached: it does not change mid-run."""
        if self._tool_cache is None:
            self.initialize()
            self._tool_cache = self._rpc("tools/list", {}).get("tools", [])
        return self._tool_cache

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


# --- MCP tools -> Anthropic tool definitions -------------------------------
#
# The server advertises 42 tools and its `inputSchema`s are verbose: pasting
# them into a prompt verbatim costs well over 10k input tokens *per turn* of a
# tool-use loop. This project has a $1 credit ceiling, so the diagnosis agent
# gets a curated subset with pruned parameters.
#
# The schemas are still **read from the live server**, not transcribed here --
# curation picks which tools and which parameters survive, but the types and
# descriptions are whatever the deployment actually advertises. A tool or
# parameter that disappears upstream is therefore a loud KeyError at startup
# rather than a 400 mid-investigation.


def _truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def anthropic_tools(
    mcp: MCPClient,
    spec: dict[str, Iterable[str]],
    *,
    overrides: dict[str, str] | None = None,
    desc_limit: int = 200,
    param_desc_limit: int = 170,
) -> list[dict[str, Any]]:
    """Build Anthropic `tools=[...]` entries from the server's own schemas.

    `spec` maps tool name -> the parameter names to keep. `overrides` replaces a
    tool's top-level description where the server's own wording assumes a chat
    UI rather than this agent.
    """
    advertised = {t["name"]: t for t in mcp.list_tools()}
    out: list[dict[str, Any]] = []
    for name, keep in spec.items():
        try:
            tool = advertised[name]
        except KeyError:
            raise MCPError(
                f"the SigNoz MCP server does not advertise {name!r}. "
                f"It offers {len(advertised)} tools; run "
                "`python -m preflight.mcp list` to see them."
            ) from None
        schema = tool.get("inputSchema") or {}
        props = schema.get("properties") or {}
        kept: dict[str, Any] = {}
        for param in keep:
            if param not in props:
                raise MCPError(
                    f"{name}: parameter {param!r} is no longer in the advertised "
                    f"schema (has: {sorted(props)})"
                )
            p = dict(props[param])
            p.pop("default", None)
            p["description"] = _truncate(p.get("description", ""), param_desc_limit)
            # SigNoz advertises union types -- `"type": ["integer", "string"]` --
            # because its Go handlers coerce either. That is legal JSON Schema
            # but not something to bet a paid API call on, so collapse it to the
            # most specific member. Picking `integer` over `string` also gets
            # the model to emit `limit: 20` rather than `limit: "20"`.
            if isinstance(p.get("type"), list):
                types = [t for t in p["type"] if t != "null"]
                p["type"] = next((t for t in types if t != "string"), "string")
            kept[param] = p
        required = [r for r in (schema.get("required") or []) if r in kept]
        out.append(
            {
                "name": name,
                "description": _truncate(
                    (overrides or {}).get(name) or tool.get("description", ""),
                    desc_limit,
                ),
                "input_schema": {
                    "type": "object",
                    "properties": kept,
                    "required": required,
                },
            }
        )
    return out


def compact(payload: Any) -> Any:
    """Strip a SigNoz MCP result down to the part that carries information.

    Three things dominate these responses and none of them are signal:

    * the `{"status": ..., "data": {"data": {"results": [...]}}}` envelope;
    * per-column metadata (`signal`, `fieldContext`, `columnType`, …) repeated
      for every column of every result;
    * the ~30 always-null well-known fields (`k8s.*`, `db.*`, `cloud.*`, …)
      that every span row carries whether or not the span is HTTP or SQL.

    A `signoz_get_trace_details` response for one golden-suite case is ~17.5k
    characters raw and ~2k compacted, which in a tool-use loop is the difference
    between the investigation fitting in the budget and not. Anything this
    function does not recognise is returned untouched.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return payload
    if not isinstance(payload, dict):
        return payload

    node = payload
    for _ in range(3):  # {"status","data"} -> {"type","meta","data"} -> {"results"}
        if isinstance(node, dict) and "results" in node:
            break
        if isinstance(node, dict) and isinstance(node.get("data"), dict):
            node = node["data"]
        else:
            return payload
    results = node.get("results") if isinstance(node, dict) else None
    if not isinstance(results, list):
        return payload

    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        entry: dict[str, Any] = {}
        if r.get("columns") and isinstance(r.get("data"), list):
            entry["columns"] = [c.get("name") for c in r["columns"]]
            entry["rows"] = r["data"]
        if isinstance(r.get("rows"), list):
            entry["spans"] = [
                {k: v for k, v in (row.get("data") or {}).items() if v not in (None, "")}
                for row in r["rows"]
                if isinstance(row, dict)
            ]
        if isinstance(r.get("series"), list):
            entry["series"] = r["series"]
        out.append(entry or r)
    return out if len(out) != 1 else out[0]


def render_result(payload: Any, *, limit: int = 3000) -> str:
    """Flatten a tool result into the string handed back to the model.

    Compacted, then truncated hard and on purpose: in a tool-use loop every turn
    re-sends the whole transcript, so one curious tool call that dumps a raw
    trace inflates the cost of every subsequent turn as well as its own.
    """
    payload = compact(payload)
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated, {len(text)} chars total]"


def main() -> int:
    """Probe helper: `python -m preflight.mcp [list|schema <tool>|<tool> '<json>']`."""
    import sys

    args = sys.argv[1:]
    with MCPClient() as mcp:
        if not args or args[0] == "list":
            for tool in mcp.list_tools():
                print(tool["name"])
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
