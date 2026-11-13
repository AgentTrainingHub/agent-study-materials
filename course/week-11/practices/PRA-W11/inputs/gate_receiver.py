"""仅用于 controlled-fixture：实际回环 HTTP 接收观察，不连接外部模型。"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import threading
import time
from uuid import uuid4


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--response',type=Path)
    parser.add_argument('--scenario',type=Path)
    parser.add_argument('--hold-headers',type=Path)
    parser.add_argument('--hold-body',type=Path)
    args=parser.parse_args();args.directory.mkdir(parents=True,exist_ok=False)
    lock=threading.Lock();counter=[0];stopping=threading.Event()
    def event(kind,**value):
        with lock:
            with (args.directory/'events.jsonl').open('a') as handle:
                handle.write(json.dumps({'event':kind,'time_ns':time.time_ns(),'evidence_source':'controlled-fixture',**value})+'\n');handle.flush();os.fsync(handle.fileno())
    def wait(path):
        if path is None:return
        path.with_suffix(path.suffix+'.hit').write_text(str(os.getpid()))
        until=time.monotonic()+40
        while not path.exists():
            if stopping.is_set() or time.monotonic()>=until:raise TimeoutError('fixture gate expired')
            time.sleep(.01)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=2*1024*1024:self.send_error(413);return
            raw=self.rfile.read(length)
            with lock:counter[0]+=1;number=counter[0]
            (args.directory/f'request-{number:03d}.json').write_bytes(raw)
            event('request_received',request_index=number,byte_count=len(raw))
            try:
                wait(args.hold_headers)
                if args.scenario:
                    from controlled_sse import build
                    scenario=json.loads(args.scenario.read_text());payload=build(scenario[number-1],json.loads(raw))
                else:payload=args.response.read_bytes()
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(payload)));self.send_header('Connection','close')
                self.send_header('x-request-id','controlled-'+uuid4().hex);self.end_headers();self.wfile.flush()
                event('headers_sent',request_index=number)
                wait(args.hold_body)
                (args.directory/f'response-{number:03d}.sse').write_bytes(payload)
                self.wfile.write(payload);self.wfile.flush()
                event('body_sent',request_index=number)
            except (OSError,TimeoutError) as exc:event('connection_closed',request_index=number,error=type(exc).__name__)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    def stop(*_):
        stopping.set();threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop)
    (args.directory/'manifest.json').write_text(json.dumps({'endpoint':f'http://127.0.0.1:{server.server_port}/chat/completions',
        'pid':os.getpid(),'evidence_source':'controlled-fixture','external_model_requests':0}))
    try:server.serve_forever(poll_interval=.05)
    finally:stopping.set();server.server_close();event('server_stopped')

if __name__=='__main__':main()
