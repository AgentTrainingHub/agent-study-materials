"""两个有限、只读的本地资料服务；控制文件仅供本地故障检查使用。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

import anyio
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
import mcp.types as types

DOCUMENTS = {
    "rules": {
        "port-format/r1": {
            "document_id": "port-format/r1", "revision": "r1",
            "text": "端口输入采用规范十进制字符串：只接受 ASCII 0～9；数值范围为闭区间 1～65535；多位输入不得以 0 开头。01、00080 均应抛出 ValueError。既有的空串、正负号、前后空白、小数和 Unicode 数字拒绝规则继续有效。1、65535、8080 保持成功。只修改 validator.py 与 test_validator.py。"},
        "private-port-plan/r1": {"document_id": "private-port-plan/r1", "revision": "r1",
                                 "text": "合成的受限资料，用于验证宿主在业务派发前执行对象许可。"},
    },
    "checks": {
        "port-checks/r1": {"document_id": "port-checks/r1", "revision": "r1",
                           "text": "保留全部原有测试。增加前导零的拒绝测试，至少覆盖 01 和 00080；继续核对 ASCII 数字、1 与 65535 边界、0 与 65536 越界。修改需要逐笔批准。完成后调用现有 shell check；回答区分实际检查结果、资料要求与未验证内容，并引用两份资料的 document_id/revision。"},
    },
}
INPUT_SCHEMA = {"type": "object", "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"], "additionalProperties": False}
OUTPUT_SCHEMA = {"type": "object", "properties": {name: {"type": "string"}
                                                    for name in ("document_id", "revision", "text")},
                 "required": ["document_id", "revision", "text"], "additionalProperties": False}


def build(args):
    server = Server("w07-" + args.server)
    docs = DOCUMENTS[args.server]
    main_id = "port-format/r1" if args.server == "rules" else "port-checks/r1"
    resource_uri = f"{args.server}://{main_id}"

    def control():
        return json.loads(args.control.read_text()) if args.control else {}

    def diagnostic(event, **fields):
        print(json.dumps({"event": event, "pid": os.getpid(), "server": args.server,
                          "monotonic": time.monotonic(), **fields}, ensure_ascii=False), file=sys.stderr, flush=True)

    def document(document_id):
        # 三种协议入口共用同一服务侧对象集合，不解释为宿主授权。
        if document_id not in docs:
            raise ValueError("document_not_found")
        return dict(docs[document_id])

    @server.list_tools()
    async def list_tools(request: types.ListToolsRequest):
        state = control()
        if state.get("revoke"):
            return types.ListToolsResult(tools=[])
        tool = types.Tool(name="lookup", description=state.get("description", f"查询 {args.server} 服务内的文档；输入 document_id，返回原文和 revision。"),
                          inputSchema=state.get("input_schema", INPUT_SCHEMA), outputSchema=OUTPUT_SCHEMA,
                          annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
        if state.get("duplicate_tools"):
            return types.ListToolsResult(tools=[tool, tool])
        if state.get("bad_cursor"):
            return types.ListToolsResult(tools=[tool], nextCursor="repeat")
        return types.ListToolsResult(tools=[tool])

    @server.call_tool(validate_input=False)
    async def lookup(name, arguments):
        if name != "lookup" or not isinstance(arguments, dict) or set(arguments) != {"document_id"}:
            return types.CallToolResult(content=[types.TextContent(type="text", text="invalid_arguments")], isError=True)
        document_id = arguments["document_id"]
        diagnostic("lookup_received", document_id=document_id)
        state = control()
        mode = state.get("mode", args.mode)
        if mode == "crash":
            os._exit(23)
        if mode == "slow":
            while True:
                diagnostic("heartbeat", document_id=document_id)
                await anyio.sleep(0.05)
        if mode == "business-error":
            return types.CallToolResult(content=[types.TextContent(type="text", text="document_unavailable")], isError=True)
        if mode == "bad-frame":
            sys.stdout.buffer.write(b"not-json\n")
            sys.stdout.buffer.flush()
            await anyio.sleep(10)
        try:
            data = document(document_id)
        except (ValueError, TypeError) as error:
            return types.CallToolResult(content=[types.TextContent(type="text", text=str(error))], isError=True)
        if mode == "bad-output":
            data["revision"] = 12
        if mode == "oversized":
            data["text"] = "x" * 70000
        content = [types.TextContent(type="text", text=str(data["text"]))]
        if mode == "resource-link":
            content.append(types.ResourceLink(type="resource_link", uri=resource_uri, name=main_id,
                                              mimeType="text/plain", description="同一资料的资源入口"))
        return types.CallToolResult(content=content, structuredContent=data, isError=False)

    @server.list_resources()
    async def list_resources(request: types.ListResourcesRequest):
        cursor = request.params.cursor if request.params else None
        if cursor is None:
            return types.ListResourcesResult(resources=[types.Resource(uri=resource_uri, name=main_id,
                                                                        mimeType="text/plain")], nextCursor="page-2")
        if cursor == "page-2":
            return types.ListResourcesResult(resources=[types.Resource(uri=f"{args.server}://index/r1", name="index/r1",
                                                                        mimeType="text/plain")])
        raise McpError(types.ErrorData(code=-32602, message="unknown_cursor"))

    async def read_resource(request):
        uri = str(request.params.uri)
        diagnostic("resource_received", uri=uri)
        if uri == f"{args.server}://index/r1":
            text = f"资料索引：{main_id}，revision=r1。"
        elif uri.startswith(f"{args.server}://"):
            try:
                text = document(uri.removeprefix(f"{args.server}://"))["text"]
            except ValueError as error:
                raise McpError(types.ErrorData(code=-32602, message="resource_not_found")) from error
        else:
            raise McpError(types.ErrorData(code=-32602, message="resource_not_found"))
        return types.ServerResult(types.ReadResourceResult(contents=[
            types.TextResourceContents(uri=uri, text=text, mimeType="text/plain")]))
    server.request_handlers[types.ReadResourceRequest] = read_resource

    @server.list_prompts()
    async def list_prompts():
        return [types.Prompt(name="explain_rule", description="生成包含资料的待发送用户消息。",
                             arguments=[types.PromptArgument(name="document_id", required=True)])]

    @server.get_prompt()
    async def get_prompt(name, arguments):
        if name != "explain_rule" or not isinstance(arguments, dict) or set(arguments) != {"document_id"}:
            raise McpError(types.ErrorData(code=-32602, message="invalid_prompt"))
        data = document(arguments["document_id"])
        return types.GetPromptResult(description="资料解释模板", messages=[types.PromptMessage(
            role="user", content=types.TextContent(type="text", text=f"解释 {data['document_id']}@{data['revision']} 的要求：\n{data['text']}"))])

    if args.mode == "protocol-error":
        async def protocol_error(request):
            raise McpError(types.ErrorData(code=-32602, message="controlled_protocol_error"))
        server.request_handlers[types.CallToolRequest] = protocol_error

    async def run():
        diagnostic("started", inherited_model_key=any("API_KEY" in key for key in os.environ))
        capabilities = server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={})
        async with stdio_server() as (read, write):
            await server.run(read, write, InitializationOptions(server_name="w07-" + args.server,
                                                               server_version="1.0", capabilities=capabilities))
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", choices=("rules", "checks"), required=True)
    parser.add_argument("--mode", choices=("normal", "slow", "crash", "business-error", "protocol-error",
                                           "bad-output", "oversized", "resource-link", "bad-frame"), default="normal")
    parser.add_argument("--control", type=Path)
    args = parser.parse_args()
    anyio.run(build(args))


if __name__ == "__main__":
    main()
