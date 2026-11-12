"""固定事实检查和 unittest 执行；完成记录写入独立管道。"""
import importlib.util
import ast
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest


# The supplied baseline methods remain intact; learner-added methods are allowed.
LEGACY_TEST_METHODS = {
    "ParsePortTest.test_lower_boundary_one_is_valid": "dd18c596236adc894bcf102568f1f49ad8da9e2018920fac2881fe2c333065a6",
    "ParsePortTest.test_upper_boundary_65535_is_valid": "1b082219bb61cd25265751ab6488a086f8f9f19666f44b395df2fe83e2a16b88",
    "ParsePortTest.test_zero_is_rejected": "9ee896f365b767064fbc7dda9d7fb255aefdb9591b308775969cd98a0ec35ba6",
    "ParsePortTest.test_65536_is_rejected": "20b595930bfcee79e48e1cd06735a841d3416f6a65d4f75339c68e68d1e772a0",
    "ParsePortTest.test_ordinary_port_is_valid": "48b18571c056ee305121b7c320a16d30bd9fcf7d8b3652813a7dd609b47430df",
    "ParsePortTest.test_leading_zero_is_rejected": "baeb6510c5842b13eb3c1abaca3841958ef7b45e88b38eccef7f87a011e37073",
    "ParsePortTest.test_non_decimal_format_is_rejected": "81826c5b7646ff36d0fb864db4414ef615ff79433f2f1a29a0f823386271fda9",
    "ParsePortTest.test_empty_string_is_rejected": "4eb5636a6b5dd0fb6246423c0894c5f812f7bc6d6ef01ddcc793a496c9c68311",
    "ParsePortTest.test_unicode_digits_are_rejected": "bfc8cd567a62c2f159408f67aac1f6ad75dd5ae1ff84c148b72a76cea94f6bf1",
    "ParsePortTest.test_signs_and_whitespace_are_rejected": "b63b692d237e8506d5ec9ac9764f451488f96e9c6e7a07586cebf5ef426755a5",
    "ParsePortTest.test_non_string_values_are_rejected": "858949c724572b9f296ba9765b9b1a09b50cfb56517b0a40826ebc6989481797"
}


def contract():
    path = Path("/contract/contract.json")
    if not path.exists():
        return None
    value = json.loads(path.read_bytes())
    if (not isinstance(value, dict)
            or set(value) != {"separator", "duplicates", "requirement_revision", "baseline_contract"}
            or not isinstance(value["separator"], str) or value["separator"] not in {",", ";"}
            or not isinstance(value["duplicates"], str) or value["duplicates"] not in {"keep", "unique"}
            or type(value["requirement_revision"]) is not int or value["requirement_revision"] < 1
            or not isinstance(value["baseline_contract"], dict)):
        raise ValueError("invalid trusted workflow contract")
    return value


def list_cases(value):
    separator = value["separator"]
    join = lambda *parts: separator.join(parts)
    repeated = [80, 443, 80] if value["duplicates"] == "keep" else [80, 443]
    other = ";" if separator == "," else ","
    return (("1", [1]), ("65535", [65535]), (join("80", "443"), [80, 443]),
            (join("80", "443", "80"), repeated), (join("1", "65535"), [1, 65535]),
            ("", None), (join("80", "", "443"), None), (separator+"80", None),
            ("80"+separator, None), (join("80", " 443"), None), (join("80", "443 "), None),
            (join("80", "0"), None), (join("80", "65536"), None), (join("80", "01"), None),
            (join("80", "+443"), None), (join("80", "１２"), None), (join("80", "١٢"), None),
            (join("80", "abc"), None), ("80"+other+"443", None),
            (None, None), (False, None), (80, None), (b"80", None), (["80"], None))


def legacy_methods():
    tree = ast.parse(Path("/workspace/test_validator.py").read_bytes())
    return {node.name+"."+method.name: hashlib.sha256(ast.dump(method, include_attributes=False).encode()).hexdigest()
            for node in tree.body if isinstance(node, ast.ClassDef)
            for method in node.body if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            and method.name.startswith("test_")}


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
    value = contract()
    count = len(cases)
    if value is not None:
        parse_ports = getattr(module, "parse_ports", None)
        if not callable(parse_ports):
            failures.append({"function": "parse_ports", "error": "missing callable"})
        else:
            for text, expected in list_cases(value):
                rejected = False
                try:
                    actual = parse_ports(text)
                except ValueError:
                    actual, rejected = None, True
                passed = rejected if expected is None else (
                    not rejected and type(actual) is list and actual == expected
                    and all(type(item) is int for item in actual))
                if not passed:
                    failures.append({"function": "parse_ports", "input": repr(text),
                                     "expected": "ValueError" if expected is None else expected,
                                     "actual": repr(actual), "raised_value_error": rejected})
        count += len(list_cases(value))
    if failures:
        print(json.dumps({"fixed_check_failures": failures}, ensure_ascii=False), file=sys.stderr)
    return {"phase": "fixed", "passed": not failures, "case_count": count}


def new_tests() -> dict:
    sys.path.insert(0, "/workspace")
    value = contract()
    observed = set()
    unchanged = True
    if value is not None:
        current_methods = legacy_methods()
        unchanged = all(current_methods.get(name) == fingerprint for name, fingerprint in LEGACY_TEST_METHODS.items())
        validator = load_module("validator", "/workspace/validator.py")
        parse_ports = validator.parse_ports
        separator = value["separator"]
        def tracked(text):
            if not isinstance(text, str):
                observed.add("non_string")
            else:
                items = text.split(separator)
                if "" in items:
                    observed.add("empty_item")
                if len(items) != len(set(items)) and all(item and item.isascii() and item.isdecimal()
                        and not item.startswith("0") and len(item)<=5 and 1<=int(item)<=65535 for item in items):
                    observed.add("duplicates")
                if any(not item or not item.isascii() or not item.isdecimal()
                       or item.startswith("0") or len(item)>5 or not 1<=int(item)<=65535 for item in items if item):
                    observed.add("item_error")
            return parse_ports(text)
        validator.parse_ports = tracked
    module = load_module("test_validator", "/workspace/test_validator.py")
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    class ObservedResult(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.executed_ids = set()
        def startTest(self, test):
            self.executed_ids.add(test.id())
            super().startTest(test)
        def addSkip(self, test, reason):
            self.executed_ids.discard(test.id())
            super().addSkip(test, reason)
    result = unittest.TextTestRunner(verbosity=2, resultclass=ObservedResult).run(suite)
    report = {"phase": "tests", "passed": result.wasSuccessful(),
              "tests_run": result.testsRun - len(result.skipped)}
    if value is not None:
        required = {"duplicates", "empty_item", "item_error", "non_string"}
        missed_legacy = sorted(name for name in LEGACY_TEST_METHODS if "test_validator."+name not in result.executed_ids)
        report.update(observed_categories=sorted(observed), missing_categories=sorted(required-observed),
                      legacy_tests_unchanged=unchanged, legacy_tests_not_run=missed_legacy)
        report["passed"] = report["passed"] and not (required-observed) and unchanged and not missed_legacy
        if not report["passed"]:
            print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
    return report


def main() -> None:
    phase, descriptor = sys.argv[1:]
    write = os.write
    encode = json.dumps
    report = fixed_checks() if phase == "fixed" else new_tests()
    write(int(descriptor), encode(report).encode("utf-8"))


if __name__ == "__main__":
    main()
