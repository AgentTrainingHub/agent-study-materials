"""固定事实检查和 unittest 执行；完成记录写入独立管道。"""
import importlib.util
import json
import os
import sys
import unittest


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fixed_checks() -> dict:
    module = load_module("validator", "/workspace/validator.py")
    parse_port = module.parse_port
    cases = (("0", None), ("1", 1), ("65535", 65535), ("65536", None),
             ("8080", 8080), ("", None), ("+80", None), (" 80", None),
             ("80 ", None), ("abc", None), ("1.5", None), ("１２", None), ("١٢", None),
             ("01", None), ("00080", None),
             (None, None), (False, None), (True, None), (80, None),
             (80.0, None), (b"80", None), ([], None), (["80"], None))
    failures = []
    for text, expected in cases:
        rejected = False
        try:
            actual = parse_port(text)
        except ValueError:
            rejected = True
            actual = None
        passed = rejected if expected is None else (
            not rejected and type(actual) is int and actual == expected)
        if not passed:
            failures.append({"input": repr(text), "expected": "ValueError" if expected is None else expected,
                             "actual": repr(actual), "raised_value_error": rejected})
    if failures:
        print(json.dumps({"fixed_check_failures": failures}, ensure_ascii=False), file=sys.stderr)
    return {"phase": "fixed", "passed": not failures, "case_count": len(cases)}


def new_tests() -> dict:
    sys.path.insert(0, "/workspace")
    module = load_module("test_validator", "/workspace/test_validator.py")
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return {"phase": "tests", "passed": result.wasSuccessful(),
            "tests_run": result.testsRun - len(result.skipped)}


def main() -> None:
    phase, descriptor = sys.argv[1:]
    write = os.write
    encode = json.dumps
    report = fixed_checks() if phase == "fixed" else new_tests()
    write(int(descriptor), encode(report).encode("utf-8"))


if __name__ == "__main__":
    main()
