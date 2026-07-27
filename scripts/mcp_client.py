"""Re-export shim. The SigNoz MCP client now lives in `preflight/mcp.py`.

M5 wrote this module because `preflight/mcp.py` was M6's to own and the two
milestones could not race on the same file; DECISIONS.md recorded that once M6
landed an equivalent client, this one should collapse into it. M6 has landed,
and the transport in `preflight.mcp` *is* this file's transport, moved
unchanged -- so there is exactly one wire implementation in the repo.

This stub survives only because `scripts/signoz_apply.py` imports
`from scripts.mcp_client import MCPClient, MCPError` and that file is frozen
(it is on the demo path). Nothing new should import from here.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from preflight.mcp import (  # noqa: E402,F401
    DEFAULT_URL,
    PROTOCOL_VERSION,
    MCPClient,
    MCPError,
    main,
)

__all__ = ["MCPClient", "MCPError", "DEFAULT_URL", "PROTOCOL_VERSION", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
