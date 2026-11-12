"""受控 worker 生命周期样例；不创建网络连接，不提供模型答案。"""
import base64
import json
import os
from pathlib import Path
import sys
import time

scenario = sys.argv[1]
Path(sys.argv[2]).write_text(str(os.getpid()), encoding="ascii")
if scenario == "no_input":
    time.sleep(30)
    raise SystemExit(0)
envelope = json.load(sys.stdin)
started = time.monotonic()


def emit(item):
    print(json.dumps(item), flush=True)


def piece(data):
    emit({"type": "chunk", "data": base64.b64encode(data).decode("ascii"),
          "arrival_ms": round((time.monotonic() - started) * 1000)})


if scenario == "early_error":
    emit({"type": "error", "class": "network_error", "message": "controlled failure"})
    raise SystemExit(0)
if scenario == "bad_order":
    piece(b"chunk before headers")
    raise SystemExit(0)
if scenario == "partial_line":
    sys.stdout.write('{"type":"headers"')
    raise SystemExit(0)
emit({"type": "headers", "status": 429 if scenario == "http_error" else 200,
      "request_id": "controlled-worker"})
if scenario == "environment":
    piece(json.dumps({"key_in_env": "DEEPSEEK_API_KEY" in os.environ,
                      "unrelated_in_env": "UNRELATED_SECRET" in os.environ}).encode())
elif scenario == "oversized":
    for _ in range(257):
        piece(b"x" * 8192)
elif scenario == "invalid_line":
    print("not JSON", flush=True)
elif scenario == "nonzero":
    raise SystemExit(7)
elif scenario == "http_error":
    piece(b'{"error":"controlled rate limit"}')
elif scenario == "stderr_noise":
    sys.stderr.write(envelope["config"]["api_key"] * 10000)
    sys.stderr.flush()
    piece(b"safe response")
else:
    piece(b"alpha")
    time.sleep(0.04)
    piece(b"beta")
    if scenario == "hang":
        time.sleep(30)
emit({"type": "end"})
