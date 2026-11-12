"""一次采集真实 SSE 修复、只读续聊和慢工具取消；结果由人工独立核对。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid

MATERIALS = Path(__file__).resolve().parent
W04 = MATERIALS.parents[2] / 'week-04/practices/PRA-W04'
LIMITS = {'A': 8, 'B': 4, 'C': 4}


def load_driver():
    spec = importlib.util.spec_from_file_location('w04_capture', W04 / 'run_experiments.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save(path, value):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def current_rows(path):
    if not path.exists():
        return []
    raw = path.read_bytes()
    return [json.loads(line) for line in raw[:raw.rfind(b'\n') + 1].splitlines()]


class Viewer:
    def __init__(self, python, entry, events, directory, cursor):
        self.stdout = (directory / 'stdout.jsonl').open('xb')
        self.stderr = (directory / 'stderr.txt').open('xb')
        env = {k: v for k, v in os.environ.items() if not k.startswith('DEEPSEEK_')}
        self.command = [python, str(entry), 'watch', '--events', str(events),
                        '--cursor', str(cursor), '--follow']
        self.process = subprocess.Popen(self.command, stdout=self.stdout, stderr=self.stderr, env=env)
        self.directory = directory
        save(directory / 'invocation.json', {'argv': self.command, 'pid': self.process.pid,
             'evidence_source': 'live-events-readonly-viewer', 'api_configuration_present': False})

    def close(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.stdout.close()
        self.stderr.close()
        (self.directory / 'exit-code.txt').write_text(f'{self.process.returncode}\n')


def collect(command, directory, workspace, *, phase, file_driver, entry, python):
    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=entry.parent, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    save(directory / 'invocation.json', {'argv': command, 'cwd': str(entry.parent),
         'phase': phase, 'pid': process.pid, 'started_at': time.time(),
         'evidence_source': 'live-external-api', 'model_request_limit': LIMITS[phase]})
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    pending = bytearray()
    approvals, viewer_records = [], []
    viewer = None
    viewer_stage, cutoff, cancel_sent = 0, None, False
    supervisor_timeout = False
    cursor = directory / 'viewer-cursor.json'
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
    selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
    try:
        while selector.get_map() or process.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed > 340 and not supervisor_timeout:
                supervisor_timeout = True
                process.send_signal(signal.SIGINT)
            if elapsed > 360:
                process.kill()
                process.wait(timeout=5)
                break
            logs = sorted((directory / 'runs').glob('run-*/events.jsonl'))
            if logs:
                events = logs[0]
                rows = current_rows(events)
                if phase == 'A':
                    if viewer_stage == 0 and rows:
                        view_dir = directory / 'viewer-1'; view_dir.mkdir()
                        viewer = Viewer(python, entry, events, view_dir, cursor)
                        viewer_stage = 1
                    elif viewer_stage == 1 and cursor.exists() and process.poll() is None:
                        state = json.loads(cursor.read_text())
                        if state['last_seq'] >= 2:
                            viewer.close(); viewer = None
                            cutoff = json.loads(cursor.read_text())
                            viewer_records.append({'action': 'closed', 'runner_alive': process.poll() is None,
                                                   'elapsed_ms': round(elapsed * 1000), 'cursor': cutoff})
                            viewer_stage = 2
                    elif viewer_stage == 2 and rows and rows[-1]['seq'] > cutoff['last_seq']:
                        view_dir = directory / 'viewer-2'; view_dir.mkdir()
                        viewer_records.append({'action': 'reopened', 'runner_alive': process.poll() is None,
                                               'elapsed_ms': round(elapsed * 1000),
                                               'source_seq_before_reopen': rows[-1]['seq'], 'cursor': cutoff})
                        viewer = Viewer(python, entry, events, view_dir, cursor)
                        viewer_stage = 3
                if phase == 'C' and not cancel_sent and process.poll() is None:
                    markers = list(events.parent.glob('shell-*/copy/marker.txt'))
                    for marker in markers:
                        data = marker.read_bytes()
                        if data:
                            (directory / 'slow-marker-before-signal.txt').write_bytes(data)
                            save(directory / 'cancel-trigger.json', {
                                'evidence_source': 'host-observed-real-container-effect',
                                'marker_path': str(marker), 'bytes_observed': len(data),
                                'elapsed_ms': round(elapsed * 1000), 'runner_pid': process.pid,
                                'events_seq_before_signal': rows[-1]['seq'] if rows else 0})
                            process.send_signal(signal.SIGINT)
                            cancel_sent = True
                            break
            for key, _ in selector.select(timeout=0.05):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[key.data].extend(chunk)
                if key.data != 'stderr':
                    continue
                pending.extend(chunk)
                while b'\n' in pending:
                    line, _, tail = pending.partition(b'\n'); pending[:] = tail
                    if not line.startswith(b'W03_APPROVAL '):
                        continue
                    proposal = json.loads(line[len(b'W03_APPROVAL '):])
                    observation = file_driver.observe_proposal(proposal, directory, workspace)
                    decision = 'approve' if phase == 'A' else 'deny'
                    item = {'time': time.time(), 'proposal': proposal, 'decision': decision,
                            'observation_directory': observation, 'source': 'experiment-driver',
                            'external_change_before_response': False}
                    approvals.append(item)
                    with (directory / 'driver.jsonl').open('a', encoding='utf-8') as handle:
                        handle.write(json.dumps(item, ensure_ascii=False) + '\n')
                    process.stdin.write(f"{decision} {proposal['proposal_id']}\n".encode())
                    process.stdin.flush()
        process.wait(timeout=5)
    finally:
        selector.close()
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try: process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
        if viewer is not None:
            # 运行者已经结束，给 follow 留出处理终态的时间。
            try: viewer.process.wait(timeout=5)
            except subprocess.TimeoutExpired: pass
            viewer.close()
        for handle in (process.stdin, process.stdout, process.stderr): handle.close()
        (directory / 'stdout.json').write_bytes(buffers['stdout'])
        (directory / 'stderr.txt').write_bytes(buffers['stderr'])
        (directory / 'exit-code.txt').write_text(f'{process.returncode}\n')
        save(directory / 'viewer-observations.json', viewer_records)
    result = json.loads(buffers['stdout'])
    if result.get('exit_code') != process.returncode:
        raise ValueError('CLI JSON 与实际退出码不一致')
    return {**result, 'phase': phase, 'directory': str(directory), 'pid': process.pid,
            'supervisor_timeout': supervisor_timeout, 'approval_count': len(approvals),
            'cancel_signal_sent': cancel_sent, 'viewer_stage': viewer_stage}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entry', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    if not os.environ.get('DEEPSEEK_API_KEY') or not os.environ.get('DEEPSEEK_MODEL'):
        parser.error('请在当前终端设置 DEEPSEEK_API_KEY 和 DEEPSEEK_MODEL')
    entry, runtime = args.entry.resolve(strict=True), args.runtime.resolve(strict=True)
    old = load_driver(); file_driver = old.load_w03_driver()
    batch = args.output_dir.resolve() / f'batch-{uuid.uuid4().hex[:12]}'
    batch.mkdir(parents=True, exist_ok=False)
    workspace = file_driver.prepare(batch / 'workspace-ab')
    session = batch / 'session-ab.jsonl'
    rows = []
    print(json.dumps({'batch_directory': str(batch), 'maximum_model_requests': 16}), flush=True)
    try:
        for phase in LIMITS:
            if phase == 'B' and (not rows or rows[0]['status'] != 'stop'):
                rows.append({'phase': 'B', 'status': 'blocked', 'reason': 'A_not_completed',
                             'model_calls': 0, 'exit_code': 2})
                continue
            if phase == 'C':
                workspace = file_driver.prepare(batch / 'workspace-c')
                session = batch / 'session-c.jsonl'
            old.snapshot(workspace, batch / f'before-{phase}')
            command = [args.python, str(entry), 'resume' if phase == 'B' else 'new',
                       '--session', str(session), '--workspace', str(workspace),
                       '--question', str(MATERIALS / 'inputs' / f'question-{phase.lower()}.txt'),
                       '--output-dir', str(batch / phase / 'runs'), '--runtime', str(runtime),
                       '--max-turns', str(LIMITS[phase]), '--max-actions', '16', '--max-tokens', '2048',
                       '--deadline-seconds', '300', '--command-timeout', '30' if phase == 'C' else '5',
                       '--check-profile', 'slow' if phase == 'C' else 'normal']
            result = collect(command, batch / phase, workspace, phase=phase,
                             file_driver=file_driver, entry=entry, python=args.python)
            old.snapshot(workspace, batch / f'after-{phase}')
            if session.exists(): (batch / phase / 'after-session.jsonl').write_bytes(session.read_bytes())
            rows.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if [r['status'] for r in rows] == ['stop', 'stop', 'cancelled'] else 1
    finally:
        save(batch / 'summary.json', rows)
        save(batch / 'experiment.json', {'evidence_source': 'live-external-api',
             'limits': LIMITS, 'maximum_model_requests': 16, 'automatic_retries': 0,
             'answer_semantics': 'requires-independent-review'})


if __name__ == '__main__':
    raise SystemExit(main())
