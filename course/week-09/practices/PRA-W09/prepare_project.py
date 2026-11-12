"""独占备份 W08 工程，安装持久决定格式、原候选读取和参数化固定检查。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

MATERIALS = Path(__file__).resolve().parent
W08 = MATERIALS.parents[2] / "week-08/practices/PRA-W08"
W04 = MATERIALS.parents[2] / "week-04/practices/PRA-W04"
W03 = MATERIALS.parents[2] / "week-03/practices/PRA-W03"
REQUIRED = ("main.py", "session.py", "journal.py", "recovery.py", "context.py", "events.py", "read_group.py", "w01_support.py",
            "w02_support.py", "w03_support.py", "w03_files.py", "w03_shell.py", "w04_io.py", "w04_protocol.py", "w04_support.py",
            "w05_support.py", "w06_io.py", "w07_mcp.py", "mcp_server.py", "w08_io.py", "w08_read.py")
SUPPLIED = ("w09_io.py", "w09_shell.py")


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"需要普通单硬链接文件：{path}")
    return path.read_bytes()


def prepare(student_dir):
    student = student_dir.resolve(strict=True)
    if not student.is_dir():
        raise ValueError("student-dir 必须为已有 W08 工程。")
    for name in REQUIRED:
        _regular(student / name)
    runtime, supplies, artifacts = student / "runtime", student / "supplies", student / ".artifacts"
    for path in (runtime, supplies, artifacts):
        if path.is_symlink() or path.exists() and not path.is_dir():
            raise ValueError(f"需要普通目录：{path}")
    for name in ("runner.py", "worker.py", "slow.py"):
        _regular(runtime / name)
    backup = artifacts / "w09-before-upgrade"
    targets = [student / name for name in SUPPLIED] + [supplies / "w09", backup]
    for path in targets:
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"目标已存在，保留原内容：{path}")
    replaced = {student / "w04_protocol.py": W04 / "w04_protocol.py", runtime / "worker.py": W08 / "runtime/worker.py",
                runtime / "runner.py": W03 / "runtime/runner.py"}
    old = {}
    for path, original in replaced.items():
        old[path] = _regular(path)
        if old[path] != _regular(original):
            raise ValueError(f"原供给件已有修改，保留原内容：{path}")
    new = {student / name: _regular(MATERIALS / name) for name in SUPPLIED}
    new.update({student / "w04_protocol.py": _regular(MATERIALS / "w04_protocol.py"),
                runtime / "worker.py": _regular(MATERIALS / "runtime/worker.py"),
                runtime / "runner.py": _regular(MATERIALS / "runtime/runner.py")})
    baseline = _regular(MATERIALS / "inputs/baseline-contract.json")
    sources = [path for path in student.rglob("*.py") if not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(student).parts)]
    snapshots = [(path.relative_to(student), _regular(path)) for path in sources]
    backup.mkdir(parents=True, exist_ok=False)
    for relative, data in snapshots:
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
        target.chmod(0o444)
    (supplies / "w09").mkdir(parents=True, exist_ok=False)
    with (supplies / "w09/baseline-contract.json").open("xb") as handle:
        handle.write(baseline)
    (supplies / "w09/baseline-contract.json").chmod(0o444)
    for path, data in new.items():
        if path not in old:
            with path.open("xb") as handle:
                handle.write(data)
            continue
        temporary = path.parent / (".w09-supply-" + uuid4().hex)
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if _regular(path) != old[path]:
                raise ValueError(f"备份期间原供给发生变化：{path}；检查已保存的备份与材料。")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    return {"student_dir": str(student), "backup": str(backup), "installed": [*SUPPLIED, "supplies/w09/baseline-contract.json"],
            "updated": ["w04_protocol.py", "runtime/runner.py", "runtime/worker.py"], "runtime_image_config": "unchanged"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.student_dir)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
