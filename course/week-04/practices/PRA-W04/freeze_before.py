"""在 U3 完成后独占保存过程式 W04 版本，供 U4 比较。"""
import argparse
from pathlib import Path
from prepare_project import snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source = args.student_dir.resolve(strict=True)
    snapshot(source, args.output_dir.absolute())
    print(f"W04 重构前源码：{args.output_dir.absolute()}")
    print("仅复制 Python 源码；配置、密钥、会话和运行证据保持独立。")


if __name__ == "__main__":
    main()
