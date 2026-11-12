"""独占备份 W05 源码，安装 W06 供给与 ASCII 需求的固定检查。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
from uuid import uuid4

MATERIALS = Path(__file__).resolve().parent
W03 = MATERIALS.parents[2] / "week-03/practices/PRA-W03"
REQUIRED = ("main.py", "w01_support.py", "w02_support.py", "w03_support.py", "w03_files.py",
            "w03_shell.py", "w04_io.py", "w04_protocol.py", "w04_support.py", "w05_support.py")


def _regular(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"需要普通单硬链接文件：{path}")
    return path.read_bytes()


def prepare(student_dir: Path) -> dict:
    student = student_dir.resolve(strict=True)
    if not student.is_dir():
        raise ValueError("student-dir 必须是已有的 W05 工程目录。")
    for name in REQUIRED:
        _regular(student / name)
    runtime = student / "runtime"
    if runtime.is_symlink() or not runtime.is_dir():
        raise ValueError("请保留 W05 工程的普通 runtime 目录。")
    for name in ("runner.py", "worker.py", "slow.py"):
        _regular(runtime / name)
    artifacts = student / ".artifacts"
    if artifacts.is_symlink() or (artifacts.exists() and not artifacts.is_dir()):
        raise ValueError(".artifacts 必须为普通目录。")
    backup = artifacts / "w06-before-upgrade"
    helper = student / "w06_io.py"
    if backup.exists() or backup.is_symlink():
        raise FileExistsError(f"备份已存在，保留原内容：{backup}")
    if helper.exists() or helper.is_symlink():
        raise FileExistsError("w06_io.py 已存在，保留原内容。")
    worker = runtime / "worker.py"
    old_worker = _regular(worker)
    if old_worker != _regular(W03 / "runtime/worker.py"):
        raise ValueError("runtime/worker.py 与 W03 原供给不同；保留现有修改，不自动替换。")
    new_helper = _regular(MATERIALS / "w06_io.py")
    new_worker = _regular(MATERIALS / "runtime/worker.py")
    sources = [path for path in student.rglob("*.py")
               if not any(part.startswith(".") or part == "__pycache__"
                          for part in path.relative_to(student).parts)]
    snapshots = [(path.relative_to(student), _regular(path)) for path in sources]
    # 所有来源、目标和原供给均已预检；备份失败时不会替换检查器。
    backup.mkdir(parents=True, exist_ok=False)
    for relative, data in snapshots:
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
        target.chmod(0o444)
    with helper.open("xb") as handle:
        handle.write(new_helper)
    temporary = runtime / (".w06-worker-" + uuid4().hex)
    try:
        with temporary.open("xb") as handle:
            handle.write(new_worker)
            handle.flush()
            os.fsync(handle.fileno())
        if _regular(worker) != old_worker:
            raise ValueError("备份期间 worker.py 发生变化；未替换，检查已保存备份和供给件。")
        os.replace(temporary, worker)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"student_dir": str(student), "backup": str(backup),
            "installed": ["w06_io.py"], "updated": ["runtime/worker.py"],
            "runtime_image_config": "unchanged"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.student_dir)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"W05 源码备份：{result['backup']}\nW06 供给件：{result['student_dir']}"
          "\n固定检查已采用 ASCII 输入要求；已有 Python 镜像配置继续使用。")


if __name__ == "__main__":
    main()
