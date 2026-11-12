"""单个真实 read worker 的传输与清理；排队和组失败由本地工程实现。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

from w08_io import SupportError, canonical, process_identity, write_json

MAX_CAPTURE_BYTES = 512 * 1024


def _worker():
    import w03_files as files
    request = json.loads(sys.stdin.buffer.readline(16385))
    identity, args = request["identity"], request["arguments"]
    started = time.monotonic()
    control = request.get("control") or {}
    if control.get("ignore_terminate"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print(json.dumps({"event":"started","identity":identity,"pid":os.getpid(),"monotonic":started}), flush=True)
    gate = control.get("gate")
    if gate:
        # 门闩由本地检查驱动给定；业务 CLI 不把它放入模型工具参数。
        gate = Path(gate)
        write_json(gate.with_suffix(".hit.json"), {"identity":identity,"pid":os.getpid(),"monotonic":time.monotonic()})
        while not gate.exists():
            time.sleep(0.01)
    try:
        offset, limit = args.get("offset",1), args.get("limit",40)
        if type(offset) is not int or offset < 1 or type(limit) is not int or not 1 <= limit <= 80:
            raise files.ToolError("invalid_arguments", "offset/limit 无效。")
        current = files.snapshot(Path(request["workspace"]), args["path"])
        data = current.data
        assert data is not None
        with Path("input.bin").open("xb") as handle:
            handle.write(data)
        text = data.decode("utf-8").replace("\r\n","\n").replace("\r","\n")
        lines = text.split("\n") if text else []
        if lines and lines[-1] == "":
            lines.pop()
        if offset > max(1,len(lines)):
            raise files.ToolError("offset_out_of_range", "offset 超过最后一行。")
        end = min(len(lines),offset-1+limit)
        result = {"ok":True,"path":args["path"],"offset":offset,"limit":limit,
                  "lines":[{"line":i+1,"text":lines[i]} for i in range(offset-1,end)],
                  "total_lines":len(lines),"truncated":end<len(lines),
                  "next_offset":end+1 if end<len(lines) else None,
                  "version":hashlib.sha256(data).hexdigest()}
    except files.ToolError as error:
        result = {"ok":False,"error":{"class":error.error_class,"message":str(error)}}
    print(json.dumps({"event":"result","identity":identity,"pid":os.getpid(),"started":started,
                      "finished":time.monotonic(),"result":result}, ensure_ascii=False), flush=True)


class ReadWorker:
    def __init__(self, arguments, *, workspace, identity, evidence_dir, control=None):
        if set(identity) != {"run_id","assistant_id","call_id","input_index"}:
            raise SupportError("invalid_worker_identity", "worker 身份字段无效。")
        if (not isinstance(arguments,dict) or set(arguments)-{"path","offset","limit"}
                or arguments.get("path") not in {"validator.py","test_validator.py"}):
            raise SupportError("invalid_read_arguments", "read 参数或文件范围无效。")
        self.request = {"arguments":arguments,"workspace":str(Path(workspace).resolve(strict=True)),
                        "identity":identity,"control":control}
        self.directory = Path(evidence_dir)
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.output = {"stdout":bytearray(),"stderr":bytearray()}
        self.cleanup = None
        self.identity = identity
        self.process_identity = None

    def start(self):
        self.directory.mkdir(parents=True,exist_ok=False)
        write_json(self.directory/"request.json",self.request)
        env={k:v for k,v in os.environ.items() if k in {"PATH","LANG","LC_ALL","LC_CTYPE"}}
        env.update(PYTHONUNBUFFERED="1",PYTHONDONTWRITEBYTECODE="1")
        self.process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"--read-worker"],
                                      cwd=self.directory,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE,start_new_session=True)
        self.process_identity=process_identity(self.process.pid)
        self.selector.register(self.process.stdout,selectors.EVENT_READ,"stdout")
        self.selector.register(self.process.stderr,selectors.EVENT_READ,"stderr")
        self.process.stdin.write(canonical(self.request)+b"\n")
        self.process.stdin.close()
        info={"pid":self.process.pid,"process_identity":self.process_identity,
              "identity":self.identity,"started_monotonic":time.monotonic()}
        write_json(self.directory/"spawn.json",info)
        return info

    def poll(self):
        for key,_ in self.selector.select(timeout=0):
            chunk=os.read(key.fileobj.fileno(),65536)
            if not chunk:
                self.selector.unregister(key.fileobj)
                continue
            self.output[key.data].extend(chunk)
            if sum(map(len,self.output.values())) > MAX_CAPTURE_BYTES:
                raise SupportError("read_worker_output_limit","read worker 输出超过512KiB。")
        return self.process.poll() is not None and not self.selector.get_map()

    def result(self):
        if not self.poll():
            raise SupportError("read_worker_running","worker 尚未完整结束。")
        if self.process.returncode != 0:
            raise SupportError("read_worker_failed",self.output["stderr"].decode("utf-8",errors="replace"))
        try:
            rows=[json.loads(line) for line in bytes(self.output["stdout"]).splitlines()]
        except (ValueError,UnicodeError) as error:
            raise SupportError("read_worker_protocol",str(error)) from error
        if (len(rows)!=2 or rows[0].get("event")!="started" or rows[1].get("event")!="result"
                or any(row.get("identity")!=self.identity or row.get("pid")!=self.process.pid for row in rows)):
            raise SupportError("read_worker_protocol","worker 返回身份或报文数量不同。")
        record=rows[1]
        if not isinstance(record.get("result"),dict) or type(record["result"].get("ok")) is not bool:
            raise SupportError("read_worker_protocol","worker 未返回明确结果。")
        write_json(self.directory/"result.json",record)
        return record["result"]

    def close(self, *, cancel=False):
        if self.cleanup is not None:
            return self.cleanup
        actions=[]
        error=None
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                    actions.append("terminate")
                    try:
                        self.process.wait(timeout=0.3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        actions.append("kill")
                        self.process.wait(timeout=1)
                while self.selector.get_map():
                    self.poll()
            except (OSError,SupportError,subprocess.TimeoutExpired) as exc:
                error={"class":type(exc).__name__,"message":str(exc)}
            finally:
                self.selector.close()
                for handle in (self.process.stdin,self.process.stdout,self.process.stderr):
                    handle.close()
        else:
            self.selector.close()
        for name,data in self.output.items():
            if self.directory.is_dir():
                with (self.directory/(name+".bin")).open("xb") as handle:
                    handle.write(data)
        self.cleanup={"pid":self.process.pid if self.process else None,"identity":self.identity,
                      "process_identity":self.process_identity,"returncode":self.process.returncode if self.process else None,
                      "cleanup_confirmed":self.process is None or self.process.returncode is not None,
                      "cancel_requested":cancel,"actions":actions,"error":error,"finished_monotonic":time.monotonic()}
        if self.directory.is_dir():
            write_json(self.directory/"cleanup.json",self.cleanup)
        return self.cleanup


if __name__=="__main__":
    if sys.argv[1:] != ["--read-worker"]:
        raise SystemExit("本文件由 read_group 启动，不提供独立业务入口。")
    _worker()
