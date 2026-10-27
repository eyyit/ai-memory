from pathlib import Path
import ast
import os
import subprocess
import sys
import time
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

def test_private_networks_ports_devices_secrets_and_cpu_budget():
    definition = yaml.safe_load((ROOT/'deploy/compose.yaml').read_text())
    assert all(network['internal'] for network in definition['networks'].values())
    services = definition['services']
    assert all(not row.get('ports') and not row.get('devices') and not row.get('privileged') for row in services.values())
    assert not any('container' in row.get('networks',[]) or row.get('network_mode')=='host' for row in services.values())
    assert services['ai-memory-postgres']['networks'] == ['memory-api-db','memory-worker-db']
    assert services['ai-memory-qdrant']['networks'] == ['memory-api-vector','memory-worker-vector']
    assert services['ai-memory-mcp']['networks'] == ['memory-client']
    assert services['ai-memory-inference']['environment']['HF_HUB_OFFLINE']=='1'
    assert services['ai-memory-inference']['deploy']['resources']['limits']['memory']=='12G'
    assert all('docker.sock' not in mount for row in services.values() for mount in row.get('volumes',[]))

def test_all_python_operator_scripts_parse():
    for file in (ROOT/'deploy/scripts').glob('*.py'): ast.parse(file.read_text())
    ast.parse((ROOT/'scripts/maintenance.py').read_text())

@pytest.mark.skipif(os.name!='posix',reason='Signal forwarding verified only on POSIX host')
def test_log_wrapper_forwards_sigterm_and_keeps_console(tmp_path):
    import signal
    log = tmp_path/'console.log'
    env = {**os.environ, 'CONTAINER_LOG_FILE':str(log)}
    process = subprocess.Popen([sys.executable,str(ROOT/'deploy/scripts/log-daemon.py'),sys.executable,'-u','-c',
        'import time; print("synthetic-log-ready",flush=True); time.sleep(30)'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        assert process.stdout.readline().strip()==b'synthetic-log-ready'
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5)==128+signal.SIGTERM
        assert 'synthetic-log-ready' in log.read_text()
    finally:
        if process.poll() is None: process.kill()
