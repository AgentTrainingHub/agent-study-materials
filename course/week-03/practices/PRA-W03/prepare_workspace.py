"""从只读原始输入创建独立 W03 练习目录；已有目录不会被覆盖。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def prepare(destination: Path) -> Path:
    source = Path(__file__).resolve().parent / "inputs/sample_project/validator.py"
    destination.mkdir(parents=True, exist_ok=False)
    with (destination / "validator.py").open("xb") as handle:
        handle.write(source.read_bytes())
    return destination.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    destination = prepare(args.destination)
    print(json.dumps({"workspace": str(destination), "files": ["validator.py"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
