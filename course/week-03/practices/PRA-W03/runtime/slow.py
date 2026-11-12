"""实际启动脱离会话的后代，并在副本中持续留下效果。"""
import os
import time


child = os.fork()
if child == 0:
    os.setsid()
    grandchild = os.fork()
    if grandchild == 0:
        while True:
            with open("/workspace/marker.txt", "a", encoding="utf-8") as stream:
                stream.write(f"heartbeat pid={os.getpid()} ppid={os.getppid()}\n")
                stream.flush()
            time.sleep(0.05)
    while True:
        time.sleep(1)
else:
    print(f"slow fixture started pid={os.getpid()} child={child}", flush=True)
    while True:
        time.sleep(1)
