#!/usr/bin/env python3
"""Prepare consistent memory recovery inputs, then run the existing server Borg job."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys

def main():
    if os.geteuid()!=0:
        raise SystemExit('Server Borg backup wrapper requires root')
    os.umask(0o077)
    os.nice(19)
    subprocess.run(['ionice','-c','3','-p',str(os.getpid())],check=True)
    with Path('/run/lock/ai-memory-borgbackup.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another memory/server backup run is already active')
        subprocess.run([sys.executable,str(Path(__file__).with_name('recovery.py')),'backup'],check=True)
        subprocess.run(['/srv/backup-jobs/borgbackup/borgbackup.py',
            '/srv/backup-jobs/borgbackup/server.json'],check=True)

if __name__=='__main__': main()
