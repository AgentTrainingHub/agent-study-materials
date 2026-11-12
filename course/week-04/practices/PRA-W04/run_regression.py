"""从相同 CLI 边界比较 W04 重构前后八项公开、隔离的行为任务。"""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from check_cli import REGRESSION_TESTS, SOURCE, run_suite


TASKS = [
    ("R01", "新建并结束", "test_new_completed"),
    ("R02", "跨进程续聊", "test_resume_new_process"),
    ("R03", "UI 显示与模型投影", "test_ui_display_projection"),
    ("R04", "完整轮次分支", "test_completed_turn_branch"),
    ("R05", "分支不改文件", "test_branch_keeps_files"),
    ("R06", "损坏尾拒绝", "test_bad_tail_rejected"),
    ("R07", "无效父链拒绝", "test_invalid_parent_rejected"),
    ("R08", "配对与不完整终态拒绝", "test_pairing_incomplete_rejected"),
]


def task_facts(directory, method):
    paths = list(directory.glob(method + "-*/facts.json"))
    if len(paths) != 1:
        return None
    return json.loads(paths[0].read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-entry", type=Path, required=True)
    parser.add_argument("--after-entry", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    for entry in (args.before_entry, args.after_entry):
        if not entry.is_file():
            parser.error(f"CLI 入口不存在：{entry}")
    if args.before_entry.resolve() == args.after_entry.resolve():
        parser.error("前后入口不能为同一路径；先冻结可运行的 W04 过程式基线")
    root = args.output_dir.resolve() / f"regression-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=False)
    manifest = {"evidence_source": SOURCE, "purpose": "public-regression", "held_out": False,
                "comparison": "working-W04-procedural-baseline-to-AgentSession-refactor",
                "external_model_requests": 0, "shell": "controlled-boundary-no-container",
                "files_and_cli_processes": "actual", "model_network": "forbidden",
                "before_entry": str(args.before_entry.resolve()), "after_entry": str(args.after_entry.resolve()),
                "tasks": [{"id": ident, "name": title, "method": method, "purpose": "public-regression"}
                          for ident, title, method in TASKS]}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    before = run_suite(args.before_entry, root / "before", REGRESSION_TESTS)
    after = run_suite(args.after_entry, root / "after", REGRESSION_TESTS)
    rows = []
    for ident, title, method in TASKS:
        old = task_facts(root / "before", method)
        new = task_facts(root / "after", method)
        rows.append({"id": ident, "name": title, "method": method,
                     "before_facts": old, "after_facts": new,
                     "same_observable_facts": bool(old) and old == new})
    passed = before["verdict"] == after["verdict"] == "pass" and all(row["same_observable_facts"] for row in rows)
    result = {"evidence_source": SOURCE, "purpose": "public-regression", "held_out": False,
              "external_model_requests": 0, "tasks_per_arm": 8, "before": before, "after": after,
              "cases": rows, "verdict": "pass" if passed else "fail", "output_directory": str(root),
              "scope": "八项机制行为及接口重构对照；不代表真实模型效果或通用成功率"}
    (root / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
