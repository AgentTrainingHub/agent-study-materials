"""固定检查命令的 Docker 供给件；副本结果只导出到证据目录。"""
import csv
from dataclasses import asdict
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import stat
import subprocess
import tarfile
import threading
import time
from uuid import uuid4

from w03_support import ToolControl, ToolError


IMAGE_PATTERN = re.compile(r"python@sha256:[0-9a-f]{64}")
DOCKER_ENV = {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"}
if "HOME" in os.environ:
    DOCKER_ENV["HOME"] = os.environ["HOME"]
INPUT_NAMES = ("validator.py", "test_validator.py")
OUTPUT_NAMES = (*INPUT_NAMES, "marker.txt")
MAX_FILE_BYTES = 64 * 1024
MAX_EXPORT_BYTES = 1024 * 1024
MAX_EXPORT_FILES = 8
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MODEL_BYTES = 8 * 1024
CLEANUP_SECONDS = 12.0
TRUSTED_DIR = Path(__file__).resolve().parent / "runtime"


def _docker(args: list[str], timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], env=DOCKER_ENV, capture_output=True,
                          text=True, timeout=max(0.01, timeout))


def _remaining(deadline: float, cap: float = 10) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Docker lifecycle deadline exceeded")
    return min(cap, remaining)


def _save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def preflight(runtime: Path) -> str:
    try:
        value = json.loads(Path(runtime).read_text(encoding="utf-8"))
        image = value["image"]
        if not isinstance(image, str) or IMAGE_PATTERN.fullmatch(image) is None:
            raise ValueError("runtime image must be a prepared python@sha256 digest")
        for args in (["info", "--format", "{{.ServerVersion}}"], ["image", "inspect", image]):
            result = _docker(args)
            if result.returncode:
                raise ValueError(result.stderr.strip() or "Docker preflight failed")
        for name in ("runner.py", "worker.py", "slow.py"):
            if not (TRUSTED_DIR / name).is_file():
                raise ValueError(f"trusted runtime file is missing: {name}")
        return image
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise ToolError("isolation_unavailable", f"Docker preflight: {exc}") from exc


def _mount(source: Path, target: str, readonly: bool = False) -> list[str]:
    buffer = io.StringIO()
    items = ["type=bind", f"src={source.resolve()}", f"dst={target}"]
    if readonly:
        items.append("readonly")
    csv.writer(buffer, lineterminator="").writerow(items)
    return ["--mount", buffer.getvalue()]


def _inputs(workspace: Path) -> dict[str, bytes]:
    result = {}
    root = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in INPUT_NAMES:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            except FileNotFoundError:
                if name == "test_validator.py":
                    continue
                raise
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError(f"input must be a single-link regular file: {name}")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise ValueError(f"input exceeds 64 KiB: {name}")
                data.decode("utf-8-sig")
                if any(len(line) > 4096 for line in data.splitlines()):
                    raise ValueError(f"input line exceeds 4 KiB: {name}")
                result[name] = data
            finally:
                os.close(fd)
    finally:
        os.close(root)
    return result


class _Capture:
    def __init__(self, directory: Path):
        self.files = {name: (directory / f"{name}.bin").open("xb") for name in ("stdout", "stderr")}
        self.counts = {name: 0 for name in self.files}
        self.observed = {name: 0 for name in self.files}
        self.visible = {name: bytearray() for name in self.files}
        self.limit_hit = False
        self.eof = False

    def append(self, name: str, chunk: bytes) -> None:
        self.observed[name] += len(chunk)
        room = max(0, MAX_OUTPUT_BYTES - sum(self.counts.values()))
        saved = chunk[:room]
        self.files[name].write(saved)
        self.counts[name] += len(saved)
        visible_room = max(0, MODEL_BYTES - sum(map(len, self.visible.values())))
        self.visible[name].extend(chunk[:visible_room])
        if sum(self.observed.values()) >= MAX_OUTPUT_BYTES:
            self.limit_hit = True

    def consume(self, process, *, until: float, cancelled, check_limits: bool = True) -> str | None:
        with selectors.DefaultSelector() as selector:
            for name in self.files:
                stream = getattr(process, name)
                if not stream.closed:
                    selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                if check_limits and cancelled():
                    return "cancelled"
                if time.monotonic() >= until:
                    return "timeout" if check_limits else "drain_incomplete"
                for key, _ in selector.select(min(0.05, max(0, until - time.monotonic()))):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    self.append(key.data, chunk)
                    if check_limits and self.limit_hit:
                        return "output_limit"
            self.eof = True
        return None

    def close(self) -> None:
        for stream in self.files.values():
            stream.close()

    def projection(self) -> dict:
        remaining = MODEL_BYTES
        result = {}
        for name in self.files:
            encoded = self.visible[name].decode("utf-8", errors="replace").encode("utf-8")
            text = encoded[:remaining].decode("utf-8", errors="ignore")
            remaining -= len(text.encode("utf-8"))
            result[name] = text
        return result


def _export(name: str, directory: Path, deadline: float) -> tuple[dict[str, bytes], list[str]]:
    """读取停止容器的归档，不提取路径，也不跟随归档链接。"""
    output = directory / "output"
    output.mkdir()
    exported, ignored = {}, []
    total = count = 0
    with (directory / "export.stderr").open("xb") as errors:
        process = subprocess.Popen(["docker", "cp", f"{name}:/workspace/.", "-"],
                                   env=DOCKER_ENV, stdout=subprocess.PIPE, stderr=errors)
        timer = threading.Timer(_remaining(deadline, 4), process.kill)
        timer.start()
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                for member in archive:
                    _remaining(deadline)
                    path = PurePosixPath(member.name)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("unsafe exported path")
                    if member.isdir() and str(path) == ".":
                        continue
                    count += 1
                    total += member.size
                    if count > MAX_EXPORT_FILES or total > MAX_EXPORT_BYTES:
                        raise ValueError("export exceeds file count or total byte limit")
                    if not member.isfile() or member.size > MAX_FILE_BYTES:
                        raise ValueError(f"export rejects links, special files and oversized files: {path}")
                    if len(path.parts) != 1 or path.name not in OUTPUT_NAMES:
                        ignored.append(str(path))
                        continue
                    if path.name in exported:
                        raise ValueError("duplicate exported file")
                    stream = archive.extractfile(member)
                    data = stream.read(MAX_FILE_BYTES + 1)
                    if len(data) != member.size:
                        raise ValueError("incomplete exported file")
                    exported[path.name] = data
            if process.wait(timeout=_remaining(deadline, 2)):
                raise ValueError("Docker copy failed; see export.stderr")
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            process.stdout.close()
    for filename, data in exported.items():
        (output / filename).write_bytes(data)
    return exported, ignored


def run_check(workspace: Path, image: str, evidence_dir: Path, *, deadline: float,
              timeout: float, cancelled, profile: str = "normal") -> dict:
    if IMAGE_PATTERN.fullmatch(image) is None or profile not in {"normal", "slow"}:
        raise ToolError("invalid_argument", "invalid fixed image or check profile")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
        raise ToolError("invalid_argument", "command timeout must be in (0,30]")
    workspace, evidence_dir = Path(workspace).absolute(), Path(evidence_dir).absolute()
    if evidence_dir.resolve().is_relative_to(workspace.resolve()):
        raise ToolError("path_scope", "shell evidence must be outside the tool workspace")
    evidence_dir.mkdir(parents=True, exist_ok=False)
    input_dir, copy_dir = evidence_dir / "input", evidence_dir / "copy"
    input_dir.mkdir()
    copy_dir.mkdir()
    try:
        original = _inputs(workspace)
    except (OSError, ValueError) as exc:
        raise ToolError("path_scope", str(exc)) from exc
    uid, gid = (os.getuid() or 65534), (os.getgid() if os.getuid() else 65534)
    for name, data in original.items():
        (input_dir / name).write_bytes(data)
        path = copy_dir / name
        path.write_bytes(data)
        path.chmod(0o600)
        if os.getuid() == 0:
            os.chown(path, uid, gid)
    copy_dir.chmod(0o700)
    if os.getuid() == 0:
        os.chown(copy_dir, uid, gid)
    name = "w03-" + uuid4().hex
    args = ["create", "--name", name, "--init", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
            "--memory", "256m", "--cpus", "1", "--user", f"{uid}:{gid}",
            "--workdir", "/workspace", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONNOUSERSITE=1",
            *_mount(copy_dir, "/workspace"), *_mount(TRUSTED_DIR, "/trusted", True),
            image, "python", "-I", "-B", "/trusted/runner.py", profile]
    capture = _Capture(evidence_dir)
    lifecycle = {"name": name, "image": image, "create_argv": args, "profile": profile}
    process = None
    status = error = export_error = cleanup_error = None
    exported = None
    ignored = []
    container_id = None
    exit_code = None
    stopped = removed = False
    attachment_interrupted = False
    cleanup_started = None
    try:
        if cancelled():
            status = "cancelled"
        else:
            created = _docker(args, _remaining(deadline))
            if created.returncode:
                raise RuntimeError(created.stderr.strip())
            container_id = created.stdout.strip()
            inspected = _docker(["inspect", name], _remaining(deadline))
            if inspected.returncode:
                raise RuntimeError(inspected.stderr.strip())
            lifecycle["created"] = json.loads(inspected.stdout)[0]
            if cancelled():
                status = "cancelled"
            else:
                _remaining(deadline)
                process = subprocess.Popen(["docker", "start", "--attach", name], env=DOCKER_ENV,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                status = capture.consume(process, until=min(deadline, time.monotonic() + timeout),
                                         cancelled=cancelled)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        status = "task_deadline" if isinstance(exc, TimeoutError) else "shell_error"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup_started = time.monotonic()
        cleanup_deadline = cleanup_started + CLEANUP_SECONDS
        # 先关闭已停止接收的 attach，避免输出背压阻塞 daemon 的 kill/rm。
        if status is not None and process is not None and process.poll() is None:
            attachment_interrupted = True
            process.kill()
            capture.consume(process, until=min(cleanup_deadline, time.monotonic() + 1),
                            cancelled=lambda: False, check_limits=False)
        try:
            inspected = _docker(["inspect", "--format", "{{json .State}}", name],
                                _remaining(cleanup_deadline, 2))
            if inspected.returncode == 0:
                state = json.loads(inspected.stdout)
                lifecycle["before_cleanup"] = state
                if state.get("Running"):
                    killed = _docker(["kill", name], _remaining(cleanup_deadline, 2))
                    lifecycle["kill"] = {"exit_code": killed.returncode, "stderr": killed.stderr}
                    state_result = _docker(["inspect", "--format", "{{json .State}}", name],
                                           _remaining(cleanup_deadline, 2))
                    if state_result.returncode:
                        raise RuntimeError(state_result.stderr.strip())
                    state = json.loads(state_result.stdout)
                lifecycle["stopped_state"] = state
                stopped = state.get("Running") is False and state.get("Pid") == 0
                exit_code = state.get("ExitCode")
                if not stopped:
                    raise RuntimeError("container stop was not confirmed")
                if process is not None:
                    capture.consume(process, until=min(cleanup_deadline, time.monotonic() + 1),
                                    cancelled=lambda: False, check_limits=False)
                try:
                    exported, ignored = _export(name, evidence_dir, cleanup_deadline)
                except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
                    export_error = f"{type(exc).__name__}: {exc}"
            elif "no such" not in inspected.stderr.lower():
                raise RuntimeError(inspected.stderr.strip())
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            cleanup_error = f"{type(exc).__name__}: {exc}"
        try:
            removal = _docker(["rm", "--force", name], _remaining(cleanup_deadline, 2))
            lifecycle["remove"] = {"exit_code": removal.returncode, "stderr": removal.stderr}
            absent = _docker(["inspect", name], _remaining(cleanup_deadline, 2))
            removed = absent.returncode != 0 and "no such" in absent.stderr.lower()
            lifecycle["absence"] = {"exit_code": absent.returncode, "stderr": absent.stderr}
            if not removed:
                raise RuntimeError("container removal was not confirmed")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            cleanup_error = f"{type(exc).__name__}: {exc}"
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=max(0.01, min(1, cleanup_deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                cleanup_error = "Docker attach process did not exit within cleanup deadline"
            for stream in (process.stdout, process.stderr):
                stream.close()
        capture.close()
        lifecycle["cleanup_seconds"] = time.monotonic() - cleanup_started
        lifecycle["stopped_confirmed"] = stopped
        lifecycle["removed_confirmed"] = removed
        lifecycle["attachment_interrupted"] = attachment_interrupted
    original_status = status
    cleanup_confirmed = removed and (stopped or container_id is None) and cleanup_error is None
    if not cleanup_confirmed:
        status = "cleanup_unconfirmed"
    elif export_error:
        status = "evidence_export_failed"
    elif capture.limit_hit:
        status = "output_limit"
    changes = []
    if exported is not None:
        changes = [filename for filename in sorted(set(original) | set(exported))
                   if original.get(filename) != exported.get(filename)]
        changes.extend(ignored)
    version_unchanged = exported is not None and all(
        exported.get(filename) == original.get(filename) for filename in INPUT_NAMES)
    fixed_passed = (status is None and profile == "normal" and version_unchanged
                    and exit_code in {0, 11, 12, 13})
    tests_ran = (status is None and profile == "normal" and version_unchanged
                 and exit_code in {0, 12})
    effect = "unknown" if not cleanup_confirmed or export_error else ("applied" if changes else "none")
    projection = capture.projection()
    complete = capture.eof and not capture.limit_hit and not attachment_interrupted
    result = {"ok": status is None and exit_code == 0,
              "exit_code": exit_code, "timed_out": original_status == "timeout",
              "cancelled": original_status == "cancelled", "capture_complete": complete,
              "stdout_bytes": capture.counts["stdout"], "stderr_bytes": capture.counts["stderr"],
              "observed_bytes": capture.observed, **projection,
              "truncated": sum(capture.observed.values()) > sum(map(len, capture.visible.values())) or not complete,
              "container_id": container_id, "cleanup_confirmed": cleanup_confirmed,
              "control": asdict(ToolControl(blocks_run=status is not None, effect=effect,
                                             scope="container_copy", status=status)),
              "input_files": {filename: str(input_dir / filename) for filename in original},
              "artifact_dir": str(evidence_dir), "new_tests_ran": tests_ran,
              "fixed_checks_passed": fixed_passed, "source_version_unchanged": version_unchanged,
              "original_status": original_status, "export_error": export_error,
              "cleanup_error": cleanup_error}
    if not result["ok"]:
        result["error"] = {"class": status or "command_failed",
                           "message": error or export_error or cleanup_error or f"check exited {exit_code}"}
    lifecycle["status"] = status
    _save(evidence_dir / "container.json", lifecycle)
    _save(evidence_dir / "manifest.json", {"input_files": list(original),
          "output_files": list(exported or {}), "changed_files": changes, "ignored_files": ignored,
          "effect_scope": "container_copy", "main_workspace_published": False})
    _save(evidence_dir / "result.json", result)
    return result
