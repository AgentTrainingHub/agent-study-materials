"""显式准备本机 W03 镜像；运行检查不会自动拉取。"""
import argparse
import json
from pathlib import Path
import subprocess

from w03_shell import DOCKER_ENV, IMAGE_PATTERN


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("配置已存在；请使用新的输出路径")
    subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                   env=DOCKER_ENV, check=True, timeout=15)
    subprocess.run(["docker", "pull", "python:3.12-slim"],
                   env=DOCKER_ENV, check=True, timeout=300)
    result = subprocess.run(
        ["docker", "image", "inspect", "python:3.12-slim", "--format", "{{json .RepoDigests}}"],
        env=DOCKER_ENV, check=True, capture_output=True, text=True, timeout=15,
    )
    candidates = [item for item in json.loads(result.stdout) if IMAGE_PATTERN.fullmatch(item)]
    if not candidates:
        raise RuntimeError("镜像没有可用的 Python RepoDigest")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump({"image": candidates[0], "source": "python:3.12-slim"}, stream, indent=2)
        stream.write("\n")
    print(str(args.output.resolve()))


if __name__ == "__main__":
    main()
