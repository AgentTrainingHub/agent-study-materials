"""SDK 2.2.0 的独立协议对照，保留现代原报文和三种 SDK 模式。"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
from pathlib import Path
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters

SERVER = Path(__file__).resolve().with_name("server.py")
ENV = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
       "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "w07-comparison", "version": "1.0"}}


async def run(output):
    if sys.version_info[:2] != (3, 12) or importlib.metadata.version("mcp") != "2.2.0":
        raise RuntimeError("对照环境需要 Python 3.12 与 mcp==2.2.0。")
    output.mkdir(parents=True, exist_ok=False)
    result = {"evidence_source": "local-mcp", "external_model_requests": 0,
              "sdk": "2.2.0", "python": sys.version.split()[0], "wire": [], "modes": {}}
    stderr = (output / "stderr.txt").open("wb")
    process = await asyncio.create_subprocess_exec(sys.executable, "-I", str(SERVER), env=ENV,
                                                    stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=stderr)
    try:
        for number, method in enumerate(("server/discover", "tools/list", "tools/call"), 1):
            params = {"_meta": META}
            if method == "tools/call":
                params.update(name="lookup", arguments={"document_id": "port-format/r1"})
            request = {"jsonrpc": "2.0", "id": number, "method": method, "params": params}
            process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
            await process.stdin.drain()
            raw = await asyncio.wait_for(process.stdout.readline(), 5)
            if not raw:
                raise RuntimeError("对照服务未返回响应。")
            response = json.loads(raw)
            if response.get("id") != number:
                raise AssertionError("响应 id 与实际请求不符。")
            result["wire"].append({"request": request, "response": response})
        final = result["wire"][-1]["response"]["result"]
        assert final["resultType"] == "complete" and final["isError"] is False
        assert final["structuredContent"]["document_id"] == "port-format/r1"
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 5)
        result["raw_server_returncode"] = process.returncode
        assert process.returncode == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        stderr.close()
        result["raw_server_cleanup_confirmed"] = process.returncode is not None
        (output / "partial-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for mode in ("auto", "2026-07-28", "legacy"):
        async with Client(StdioServerParameters(command=sys.executable, args=["-I", str(SERVER)], env=ENV), mode=mode) as client:
            tools = await client.list_tools()
            reply = await client.call_tool("lookup", {"document_id": "port-format/r1"})
            assert not reply.is_error and reply.structured_content["revision"] == "r1"
            result["modes"][mode] = {"protocol": client.protocol_version,
                                      "tools": [tool.name for tool in tools.tools],
                                      "structured_content": reply.structured_content}
    assert result["modes"]["auto"]["protocol"] == "2026-07-28"
    assert result["modes"]["legacy"]["protocol"] == "2025-11-25"
    result.update(status="passed", count=6, scope="现代原报文 3 组与 SDK 模式 3 组；主线 Host 另验")
    (output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "passed", "count": 6, "external_model_requests": 0, "output": str(output)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output_dir))


if __name__ == "__main__":
    main()
