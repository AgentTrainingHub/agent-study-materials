"""独占备份 W06 源码，安装 MCP 供给及前导零需求的固定检查。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

MATERIALS = Path(__file__).resolve().parent
W06 = MATERIALS.parents[2] / "week-06/practices/PRA-W06"
REQUIRED = ("main.py", "w01_support.py", "w02_support.py", "w03_support.py", "w03_files.py",
            "w03_shell.py", "w04_io.py", "w04_protocol.py", "w04_support.py", "w05_support.py", "w06_io.py")
SUPPLIED = ("w07_mcp.py", "mcp_server.py")


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"需要普通单硬链接文件：{path}")
    return path.read_bytes()


def prepare(student_dir):
    student = student_dir.resolve(strict=True)
    if not student.is_dir():
        raise ValueError("student-dir 必须为已有 W06 工程。")
    for name in REQUIRED:
        _regular(student / name)
    runtime = student / "runtime"
    if runtime.is_symlink() or not runtime.is_dir():
        raise ValueError("runtime 必须为普通目录。")
    for name in ("runner.py", "worker.py", "slow.py"):
        _regular(runtime / name)
    artifacts = student / ".artifacts"
    if artifacts.is_symlink() or (artifacts.exists() and not artifacts.is_dir()):
        raise ValueError(".artifacts 必须为普通目录。")
    backup = artifacts / "w07-before-upgrade"
    if backup.exists() or backup.is_symlink():
        raise FileExistsError(f"备份已存在，保留原内容：{backup}")
    for name in SUPPLIED:
        target = student / name
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"供给件已存在，保留原内容：{target}")
    worker = runtime / "worker.py"
    old_worker = _regular(worker)
    if old_worker != _regular(W06 / "runtime/worker.py"):
        raise ValueError("worker.py 与 W06 原供给不同，保留现有修改。")
    supplied = {name: _regular(MATERIALS / name) for name in SUPPLIED}
    new_worker = _regular(MATERIALS / "runtime/worker.py")
    sources = [path for path in student.rglob("*.py")
               if not any(part.startswith(".") or part == "__pycache__"
                          for part in path.relative_to(student).parts)]
    snapshots = [(path.relative_to(student), _regular(path)) for path in sources]
    backup.mkdir(parents=True, exist_ok=False)
    for relative, data in snapshots:
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
        target.chmod(0o444)
    for name, data in supplied.items():
        with (student / name).open("xb") as handle:
            handle.write(data)
    temporary = runtime / (".w07-worker-" + uuid4().hex)
    try:
        with temporary.open("xb") as handle:
            handle.write(new_worker)
            handle.flush()
            os.fsync(handle.fileno())
        if _regular(worker) != old_worker:
            raise ValueError("备份期间 worker 已变化，未替换；检查已保存备份与供给件。")
        os.replace(temporary, worker)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"student_dir": str(student), "backup": str(backup), "installed": list(SUPPLIED),
            "updated": ["runtime/worker.py"], "runtime_image_config": "unchanged"}


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
