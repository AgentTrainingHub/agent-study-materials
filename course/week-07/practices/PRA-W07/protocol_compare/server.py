"""SDK 2.2.0 的单工具对照服务；不接入主工程 Host。"""
import sys

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

server = MCPServer("w07-protocol-comparison")


@server.tool()
def lookup(document_id: str) -> dict[str, str]:
    """取得固定资料身份与一句要求。"""
    if document_id != "port-format/r1":
        raise ToolError("document_not_found")
    return {"document_id": document_id, "revision": "r1", "text": "多位端口输入不得以 0 开头。"}


if __name__ == "__main__":
    print("protocol comparison server started", file=sys.stderr, flush=True)
    server.run(transport="stdio")
