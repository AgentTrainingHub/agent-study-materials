"""创建 W06 练习工作区，含闭区间基线、旧测试与项目资源；已有目录不覆盖。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

MATERIALS = Path(__file__).resolve().parent


def prepare(destination: Path) -> Path:
    contents = {}
    for name in ("sample_project", "project_resources"):
        source = MATERIALS / "inputs" / name
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"供给文件不能是链接：{path}")
            if path.is_file():
                relative = path.relative_to(source)
                if relative in contents:
                    raise ValueError(f"供给文件路径重复：{relative}")
                contents[relative] = path.read_bytes()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"工作区已存在，保留原内容：{destination}")
    destination.mkdir(parents=True, exist_ok=False)
    for relative, data in contents.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
    return destination.resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        destination = prepare(args.destination)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    files = sorted(str(path.relative_to(destination)) for path in destination.rglob("*") if path.is_file())
    print(json.dumps({"workspace": str(destination), "files": files}, ensure_ascii=False))


if __name__ == "__main__":
    main()
