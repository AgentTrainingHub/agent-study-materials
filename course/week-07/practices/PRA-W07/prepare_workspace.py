"""创建 W07 ASCII/闭区间基线工作区；已有目录不覆盖。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

MATERIALS = Path(__file__).resolve().parent


def prepare(destination):
    source = MATERIALS / "inputs/sample_project"
    files = {name: (source / name).read_bytes() for name in ("validator.py", "test_validator.py")}
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"工作区已存在，保留原内容：{destination}")
    destination.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        with (destination / name).open("xb") as handle:
            handle.write(data)
    return destination.resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        workspace = prepare(args.destination)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"workspace": str(workspace), "files": ["validator.py", "test_validator.py"]}))


if __name__ == "__main__":
    main()
