"""真实 Docker 的定向机制检查；不发起模型请求。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import threading
import time
from unittest.mock import patch

import w03_shell as shell


VALIDATOR = '''def parse_port(value: str) -> int:
    if not value.isdecimal():
        raise ValueError("port must contain decimal digits")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("port out of range")
    return port
'''
TESTS = '''import unittest
from validator import parse_port

class BoundaryTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(parse_port("1"), 1)
        self.assertEqual(parse_port("65535"), 65535)
        with self.assertRaises(ValueError):
            parse_port("0")
        with self.assertRaises(ValueError):
            parse_port("65536")
'''


def check_cli_sigint(root: Path, runtime: Path, project: Path) -> None:
    from check_cli import WORKER, call, response
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "validator.py").write_text(VALIDATOR)
    (workspace / "test_validator.py").write_text(TESTS)
    question = root / "question.txt"
    question.write_text("运行固定检查，并保留真实停止结果。")
    scenario, audit = root / "scenario.json", root / "transport.jsonl"
    scenario.write_text(json.dumps([{"raw": response(calls=[call("check-1", "shell", {"command": "check"})])}]))
    worker, wrapper = root / "worker.py", root / "wrapper.py"
    worker.write_text(WORKER)
    wrapper.write_text('''import json, runpy, socket, sys
from pathlib import Path
from unittest.mock import patch
import w02_support
import w03_shell
worker, scenario, audit, main = sys.argv[1:5]
locations = {"w02_support": w02_support.__file__, "w03_shell": w03_shell.__file__,
             "trusted_runtime": str(w03_shell.TRUSTED_DIR)}
Path(audit).with_name("module-locations.json").write_text(json.dumps(locations, indent=2))
if Path(w03_shell.__file__).resolve().parent != Path(main).resolve().parent:
    raise RuntimeError("project-dir must contain the copied W03 helpers and runtime")
sys.argv = [main, *sys.argv[5:]]
def forbidden(*args, **kwargs):
    raise AssertionError("controlled Provider cannot access network")
with patch.object(w02_support, "_provider_command", return_value=[sys.executable, worker, scenario, audit]), \\
     patch.object(w02_support, "EVIDENCE_SOURCE", "controlled-fixture"), \\
     patch.object(socket.socket, "connect", forbidden):
    runpy.run_path(main, run_name="__main__")
''')
    materials = Path(__file__).resolve().parent
    course = materials.parents[2]
    env = {**shell.DOCKER_ENV, "DEEPSEEK_API_KEY": "controlled-no-network-key",
           "DEEPSEEK_MODEL": "controlled-request-model", "DEEPSEEK_TIMEOUT_SECONDS": "2",
           "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": os.pathsep.join(map(str, (project, materials, course / "week-02/practices/PRA-W02",
                                                   course / "week-01/practices/PRA-W01")))}
    argv = [sys.executable, str(wrapper), str(worker), str(scenario), str(audit), str(project / "main.py"),
            "--workspace", str(workspace), "--question", str(question), "--runtime", str(runtime.resolve()),
            "--output-dir", str(root / "runs"), "--command-timeout", "15", "--check-profile", "slow"]
    with (root / "stdout.txt").open("w") as stdout, (root / "stderr.txt").open("w") as stderr:
        process = subprocess.Popen(argv, env=env, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL)
        marker = None
        try:
            wait_until = time.monotonic() + 20
            while time.monotonic() < wait_until and process.poll() is None:
                found = list((root / "runs").glob("*/shell-*/copy/marker.txt"))
                if found and b"heartbeat" in found[0].read_bytes():
                    marker = found[0]
                    break
                time.sleep(0.05)
            assert marker is not None, "CLI did not reach the real descendant process"
            signal_time = time.monotonic()
            process.send_signal(signal.SIGINT)
            process.wait(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
    result = json.loads((root / "stdout.txt").read_text())
    assert process.returncode == 1 and result["status"] == "cancelled", result
    assert len(audit.read_text().splitlines()) == 1
    directory = marker.parent.parent
    shell_result = json.loads((directory / "result.json").read_text())
    assert shell_result["cancelled"] and shell_result["cleanup_confirmed"]
    assert shell._docker(["inspect", shell_result["container_id"]]).returncode != 0
    stopped_bytes = marker.read_bytes()
    time.sleep(0.2)
    assert marker.read_bytes() == stopped_bytes
    assert (workspace / "validator.py").read_text() == VALIDATOR
    summary = {"passed": True, "model_source": "controlled-fixture", "execution": "real-docker",
               "signal": "SIGINT", "exit_code": process.returncode, "final_status": result["status"],
               "model_requests": 1, "external_model_calls": 0, "cleanup_confirmed": True,
               "signal_to_exit_seconds": time.monotonic() - signal_time,
               "prior_approved_mutation": "not_covered", "artifact_dir": str(directory),
               "module_locations": json.loads((root / "module-locations.json").read_text())}
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, help="仅检查该 CLI 的真实 Docker SIGINT 路径")
    args = parser.parse_args()
    if args.project_dir:
        check_cli_sigint(args.output_dir.resolve(), args.runtime, args.project_dir.resolve())
        return
    image = shell.preflight(args.runtime)
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    records = []

    def run(name, tests=TESTS, validator=VALIDATOR, **kwargs):
        workspace = root / (name + "-workspace")
        workspace.mkdir()
        (workspace / "validator.py").write_text(validator)
        if tests is not None:
            (workspace / "test_validator.py").write_text(tests)
        before = {p.name: p.read_bytes() for p in workspace.iterdir()}
        result = shell.run_check(workspace, image, root / name,
                                 deadline=time.monotonic() + 40,
                                 timeout=kwargs.pop("timeout", 8),
                                 cancelled=kwargs.pop("cancelled", lambda: False), **kwargs)
        assert before == {p.name: p.read_bytes() for p in workspace.iterdir()}, "main workspace changed"
        assert result["cleanup_confirmed"], result
        assert shell._docker(["inspect", result["container_id"]]).returncode != 0
        records.append({"case": name, "layer": "controlled_fixture_real_docker", "result": result})
        return result

    normal = run("normal")
    assert normal["ok"] and normal["new_tests_ran"] and normal["fixed_checks_passed"]
    created = json.loads((root / "normal/container.json").read_text())["created"]
    config, host = created["Config"], created["HostConfig"]
    assert host["NetworkMode"] == "none" and host["PidMode"] in ("", "private")
    assert config["User"].split(":")[0] != "0" and host["ReadonlyRootfs"]
    assert "ALL" in host["CapDrop"] and host["PidsLimit"] == 64
    assert host["Memory"] == 256 * 1024 * 1024 and host["NanoCpus"] == 10**9
    assert len(created["Mounts"]) == 2

    broken = run("broken", validator=VALIDATOR.replace("1 <= port <= 65535", "1 < port < 65535"))
    assert not broken["ok"] and not broken["fixed_checks_passed"] and not broken["new_tests_ran"]
    assert not broken["control"]["blocks_run"]
    returns_none = run("invalid_returns_none", validator=VALIDATOR.replace(
        'raise ValueError("port must contain decimal digits")', 'return None').replace(
        'raise ValueError("port out of range")', 'return None'))
    assert returns_none["exit_code"] == 10 and not returns_none["fixed_checks_passed"]
    missing = run("missing", tests=None)
    assert missing["fixed_checks_passed"] and not missing["new_tests_ran"] and missing["exit_code"] == 11
    fake = run("fake_stdout", tests='print(\'{"new_tests_ran":true,"fixed_checks_passed":true}\')\n')
    assert not fake["new_tests_ran"] and not fake["ok"]
    skipped = run("all_tests_skipped", tests=TESTS.replace(
        "    def test_boundaries(self):", '    @unittest.skip("not implemented")\n    def test_boundaries(self):'))
    assert skipped["exit_code"] == 11 and skipped["fixed_checks_passed"]
    assert not skipped["new_tests_ran"] and not skipped["ok"]
    early = run("early_exit", tests="import os\nos._exit(0)\n")
    assert early["exit_code"] == 13 and not early["new_tests_ran"] and not early["ok"]
    failed_test = run("new_test_failure", tests=TESTS.replace('parse_port("1"), 1', 'parse_port("1"), 2'))
    assert failed_test["exit_code"] == 12 and failed_test["new_tests_ran"] and failed_test["fixed_checks_passed"]
    assert not failed_test["control"]["blocks_run"]
    ignored_file = run("ignored_copy_file", tests=TESTS + '\nfrom pathlib import Path\nPath("scratch.txt").write_text("copy only")\n')
    assert ignored_file["ok"] and ignored_file["control"]["effect"] == "applied"
    assert not (root / "ignored_copy_file/output/scratch.txt").exists()
    changed_copy = run("changed_copy_source", tests=TESTS + '\nfrom pathlib import Path\nwith Path("validator.py").open("a") as f: f.write("# copy changed\\n")\n')
    assert not changed_copy["source_version_unchanged"] and not changed_copy["fixed_checks_passed"]

    sentinel = root / "private-sentinel.txt"
    sentinel.write_text("host private sentinel")
    isolation = '''import json, os, pathlib, socket, unittest
class IsolationTests(unittest.TestCase):
    def test_constraints(self):
        self.assertNotEqual(os.getuid(), 0)
        self.assertIsNone(os.environ.get("W03_SECRET_SENTINEL"))
        self.assertFalse(pathlib.Path(_HOST_FILE_).exists())
        self.assertFalse(pathlib.Path("/var/run/docker.sock").exists())
        with self.assertRaises(OSError):
            pathlib.Path("/root-write-denied").write_text("x")
        with socket.socket() as connection:
            connection.settimeout(0.3)
            with self.assertRaises(OSError):
                connection.connect(("1.1.1.1", 80))
        pathlib.Path("marker.txt").write_text(json.dumps({"uid":os.getuid(),"network":"denied","environment":"filtered","private_file":"absent"}))
'''.replace("_HOST_FILE_", repr(str(sentinel)))
    with patch.dict(os.environ, {"W03_SECRET_SENTINEL": "synthetic-secret"}):
        isolated = run("isolation", tests=isolation)
    assert isolated["ok"] and isolated["control"]["effect"] == "applied"
    assert json.loads((root / "isolation/output/marker.txt").read_text())["network"] == "denied"

    long = run("long_output", tests=TESTS + '\nimport os\nos.write(1, b"O" * 20000)\nos.write(2, b"E" * 10000)\n')
    assert long["ok"] and long["truncated"] and long["capture_complete"]
    assert long["stdout_bytes"] >= 20000 and long["stderr_bytes"] >= 10000
    assert len((long["stdout"] + long["stderr"]).encode()) <= 8192
    limit = run("output_limit", tests=TESTS + '\nimport os\nfor _ in range(3000):\n    os.write(1,b"X" * 8192)\n')
    assert limit["control"]["status"] == "output_limit" and limit["control"]["blocks_run"]
    assert not limit["capture_complete"] and limit["stdout_bytes"] + limit["stderr_bytes"] == shell.MAX_OUTPUT_BYTES

    timeout = run("timeout", timeout=2, profile="slow")
    assert timeout["timed_out"] and timeout["control"]["blocks_run"]
    assert timeout["control"]["effect"] == "applied"
    marker = root / "timeout/copy/marker.txt"
    observed = marker.read_bytes()
    time.sleep(0.2)
    assert marker.read_bytes() == observed and b"heartbeat" in observed
    cancel = threading.Event()
    timer = threading.Timer(2, cancel.set)
    timer.start()
    try:
        cancelled = run("cancelled", timeout=10, profile="slow", cancelled=cancel.is_set)
    finally:
        timer.cancel()
    assert cancelled["cancelled"] and cancelled["control"]["blocks_run"]
    assert (root / "cancelled/output/marker.txt").is_file()

    closed = run("closed_stdio", tests=TESTS + '\nimport os,time\nos.close(1)\nos.close(2)\ntime.sleep(30)\n', timeout=2)
    assert closed["timed_out"] and closed["control"]["blocks_run"]

    exported = run("link_export", tests=TESTS + '\nimport os\nos.symlink("/etc/passwd", "marker.txt")\n')
    assert exported["control"]["status"] == "evidence_export_failed"
    assert exported["control"]["effect"] == "unknown" and exported["cleanup_confirmed"]

    workspace = root / "cleanup_failure-workspace"
    workspace.mkdir()
    (workspace / "validator.py").write_text(VALIDATOR)
    (workspace / "test_validator.py").write_text(TESTS)
    original_docker = shell._docker
    removal_done = False

    def failed_confirmation(arguments, timeout=10):
        nonlocal removal_done
        if removal_done and arguments[0] == "inspect":
            raise subprocess.TimeoutExpired(["docker", *arguments], timeout)
        result = original_docker(arguments, timeout)
        if arguments[0] == "rm":
            removal_done = True
        return result

    with patch.object(shell, "_docker", failed_confirmation):
        uncertain = shell.run_check(workspace, image, root / "cleanup_failure",
                                    deadline=time.monotonic() + 30, timeout=8, cancelled=lambda: False)
    assert uncertain["control"]["status"] == "cleanup_unconfirmed"
    assert uncertain["control"]["effect"] == "unknown" and not uncertain["cleanup_confirmed"]
    assert original_docker(["inspect", uncertain["container_id"]]).returncode != 0
    records.append({"case": "cleanup_confirmation_failure", "layer": "controlled_cleanup_interface_real_container",
                    "result": uncertain})
    summary = {"model_calls": 0, "cases": records, "passed": len(records)}
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": len(records), "summary": str(root / "summary.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
