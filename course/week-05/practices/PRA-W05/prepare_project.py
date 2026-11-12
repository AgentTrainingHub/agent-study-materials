"""独占保存 W04 源码并安装 W05 传输供给件；流式与事件机制由本地实现。"""
import argparse
from pathlib import Path

MATERIALS = Path(__file__).resolve().parent
HELPERS = ("w05_support.py",)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-dir", type=Path, required=True)
    args = parser.parse_args()
    student = args.student_dir.resolve(strict=True)
    required = ("main.py", "w01_support.py", "w02_support.py",
                "w03_support.py", "w03_files.py", "w03_shell.py", "w04_io.py",
                "w04_protocol.py", "w04_support.py")
    if any(not (student / name).is_file() for name in required) or not (student / "runtime").is_dir():
        parser.error("先完成 W04 工程，保留已有模块和 runtime。")
    backup = student / ".artifacts/w05-before-upgrade"
    if backup.exists() or backup.is_symlink():
        parser.error(f"备份已存在，保留原内容：{backup}")
    if any((student / name).exists() or (student / name).is_symlink() for name in HELPERS):
        parser.error("W05 供给件已存在；不要覆盖已有文件。")
    sources = [path for path in student.rglob("*.py")
               if not any(part.startswith(".") or part == "__pycache__"
                          for part in path.relative_to(student).parts)]
    backup.mkdir(parents=True, exist_ok=False)
    for source in sources:
        destination = backup / source.relative_to(student)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as target:
            target.write(source.read_bytes())
        destination.chmod(0o444)
    for name in HELPERS:
        with (student / name).open("xb") as target:
            target.write((MATERIALS / name).read_bytes())
    print(f"W04 源码备份：{backup}\nW05 供给件：{student}")


if __name__ == "__main__":
    main()
