"""Connection facts: loopback endpoint, MCP launcher, masked token."""
from vibeforma import connect


def test_endpoint_is_loopback():
    assert connect.endpoint(8787) == "http://127.0.0.1:8787"


def test_mcp_command_points_into_the_install_root():
    command = connect.mcp_command(r"D:\VibeForma")
    assert command.replace("\\", "/").endswith("VibeForma/mcp.cmd")


def test_mask_token_keeps_only_the_tail():
    assert connect.mask_token("abcdefgh") == "****efgh"
    assert connect.mask_token("abcd") == "****"
    assert connect.mask_token("") == ""
