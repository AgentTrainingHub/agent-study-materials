"""独占备份 W03 源码并安装 W04 供给件，不生成目标实现代码。"""
import argparse
from pathlib import Path

MATERIALS = Path(__file__).resolve().parent
HELPERS = ("w04_io.py", "w04_protocol.py", "w04_support.py")


def snapshot(source, destination):
    files = [path for path in source.rglob("*.py")
             if not any(part.startswith(".") or part == "__pycache__"
                        for part in path.relative_to(source).parts)]
    if not (source / "main.py").is_file() or not files:
        raise ValueError("source 必须包含已有 main.py。")
    if destination.exists():
        raise FileExistsError(f"备份已存在，保留原内容：{destination}")
    destination.mkdir(parents=True, exist_ok=False)
    for path in files:
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(path.read_bytes())
        target.chmod(0o444)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-dir", type=Path, required=True)
    args = parser.parse_args()
    student = args.student_dir.resolve(strict=True)
    required = ("main.py", "w01_support.py", "w02_support.py", "w03_support.py", "w03_files.py", "w03_shell.py")
    if any(not (student / name).is_file() for name in required) or not (student / "runtime").is_dir():
        parser.error("先完成 W03 工程及 runtime 目录。")
    targets = [student / name for name in HELPERS]
    if any(path.exists() or path.is_symlink() for path in targets):
        parser.error("W04 供给文件已存在；请保留现有文件，不要重复安装。")
    backup = student / ".artifacts/w04-before-upgrade"
    snapshot(student, backup)
    for name in HELPERS:
        with (student / name).open("xb") as handle:
            handle.write((MATERIALS / name).read_bytes())
    print(f"W03 源码备份：{backup}\nW04 供给件：{student}")


if __name__ == "__main__":
    main()
