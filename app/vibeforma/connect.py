"""Connection facts a human copies into a local AI agent."""
from __future__ import annotations

import os


def endpoint(port: int) -> str:
    """The loopback endpoint the HTTP API publishes."""
    return f"http://127.0.0.1:{int(port)}"


def mcp_command(install_dir: str) -> str:
    """The MCP launcher an AI client starts.

    The installer copies app/ to the target root, so the launcher sits
    beside main.py, not inside an app/ subfolder.
    """
    return os.path.join(install_dir, "mcp.cmd")


def mask_token(token: str) -> str:
    """A token shown in the window: never the whole secret."""
    token = str(token or "")
    if not token:
        return ""
    if len(token) <= 4:
        return "*" * len(token)
    return "*" * (len(token) - 4) + token[-4:]
