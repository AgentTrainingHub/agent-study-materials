"""在工作区外创建本机可信 MCP 配置，不覆盖已有文件。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from w07_mcp import load_config


def prepare(student_dir, sdk_python, output, *, rules_mode="normal"):
    student = student_dir.resolve(strict=True)
    # venv 的 python 通常是链接；保留入口路径才能保留该 venv 的 site-packages。
    python = sdk_python.absolute()
    server = student / "mcp_server.py"
    if not server.is_file() or server.is_symlink():
        raise ValueError("先用 prepare_project.py 安装普通 mcp_server.py。")
    if not python.is_file():
        raise ValueError("sdk-python 必须为已创建的独立 venv 解释器。")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"配置已存在，保留原内容：{output}")
    config = {"schema_version": 1, "sdk_python": str(python), "servers": []}
    for name, document_id in (("rules", "port-format/r1"), ("checks", "port-checks/r1")):
        argv = [str(python), str(server), "--server", name]
        if name == "rules" and rules_mode != "normal":
            argv += ["--mode", rules_mode]
        config["servers"].append({"id": name, "argv": argv,
                                  "allowed_tools": {"lookup": {"document_ids": [document_id]}},
                                  "resource_uris": [f"{name}://{document_id}"]})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return load_config(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-dir", type=Path, required=True)
    parser.add_argument("--sdk-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rules-mode", choices=("normal", "slow"), default="normal")
    args = parser.parse_args()
    try:
        config = prepare(args.student_dir, args.sdk_python, args.output, rules_mode=args.rules_mode)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"config": str(args.output.absolute()), "config_digest": config["config_digest"],
                      "sdk_python": config["sdk_python"], "servers": [item["id"] for item in config["servers"]]}))


if __name__ == "__main__":
    main()
