"""只读检查入口。测试 stdout 不参与完成状态判断。"""
import json
import os
from pathlib import Path
import subprocess
import sys


def worker(phase: str) -> dict | None:
    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "/trusted/worker.py", phase, str(write_fd)],
            pass_fds=(write_fd,), cwd="/workspace",
        )
        os.close(write_fd)
        write_fd = -1
        code = process.wait()
        raw = os.read(read_fd, 4097)
        if code != 0 or not raw or len(raw) > 4096:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("phase") != phase:
            return None
        return value
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def main() -> int:
    if sys.argv[1:] == ["slow"]:
        return subprocess.call([sys.executable, "-I", "-B", "/trusted/slow.py"])
    if sys.argv[1:] != ["normal"]:
        return 20
    fixed = worker("fixed")
    if Path("/contract/contract.json").is_file():
        print("W09_FIXED_CHECKS " + json.dumps(fixed), flush=True)
    if fixed is None or fixed.get("passed") is not True:
        return 10
    if not Path("/workspace/test_validator.py").is_file():
        print("test_validator.py is missing", file=sys.stderr)
        return 11
    tests = worker("tests")
    if Path("/contract/contract.json").is_file():
        print("W09_TEST_CHECKS " + json.dumps(tests), flush=True)
    if tests is None:
        print("test worker did not return a completion receipt", file=sys.stderr)
        return 13
    count = tests.get("tests_run")
    if type(count) is not int or count < 1:
        print("test_validator.py must execute at least one unittest test", file=sys.stderr)
        return 11
    return 0 if tests.get("passed") is True else 12


if __name__ == "__main__":
    sys.exit(main())
