#!/usr/bin/env python3
"""Signal-forwarding console logging; compatible with host logrotate."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

os.umask(0o027)
filename = Path(os.environ.get('CONTAINER_LOG_FILE', '/var/log/container.log'))
filename.parent.mkdir(parents=True, exist_ok=True)
if len(sys.argv) < 2:
    raise SystemExit('Daemon command required')
# Qdrant does not implement Docker-style *_FILE settings itself.
secret = os.environ.get('QDRANT__SERVICE__API_KEY_FILE')
if secret:
    os.environ['QDRANT__SERVICE__API_KEY'] = Path(secret).read_text().strip()
child = subprocess.Popen(sys.argv[1:], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
def forward(signum, _):
    try: os.killpg(child.pid, signum)
    except ProcessLookupError: pass
signal.signal(signal.SIGTERM, forward)
signal.signal(signal.SIGINT, forward)
def copy():
    for raw in child.stdout:
        with filename.open('ab', buffering=0) as output: output.write(raw)
        try:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
        except BrokenPipeError: pass
thread = threading.Thread(target=copy)
thread.start()
status = child.wait()
thread.join()
raise SystemExit(status if status >= 0 else 128 - status)
