import argparse
import json
import signal
import threading
from .config import Settings
from .importers import (
  import_records,
  iter_export,
  iter_git_history,
  iter_shell_history,
)
from .runtime import build_service

def parser():
    p = argparse.ArgumentParser(prog='ai-memory')
    commands = p.add_subparsers(dest='command', required=True)
    for name in ['mcp', 'init', 'retry-index', 'retry-extraction', 'worker', 'queue', 'download-models']:
        commands.add_parser(name)
    commands.choices['worker'].add_argument('--once', action='store_true')
    commands.choices['worker'].add_argument('--interval', type=float, default=3)
    reindex = commands.add_parser('reindex')
    reindex.add_argument('--reset', action='store_true')
    for name in ['import-json', 'import-export']:
        j = commands.add_parser(name)
        j.add_argument('path')
        j.add_argument(
          '--source', choices=['chatgpt', 'claude', 'gemini'],
          required=True,
        )
    g = commands.add_parser('import-git')
    g.add_argument('repository')
    g.add_argument('--max-commits', type=int, default=10000)
    s = commands.add_parser('import-shell')
    s.add_argument('path')
    s.add_argument('--source')
    b = commands.add_parser('agy-bridge')
    b.add_argument('--socket', required=True)
    b.add_argument('--mode', default='0660')
    b.add_argument('--model-file', default='')
    b.add_argument('--agy-bin', default='agy')
    r = commands.add_parser('refresh-agy-model')
    r.add_argument('--model-file', required=True)
    r.add_argument('--agy-bin', default='agy')
    return p

def main():
    args = parser().parse_args()
    if args.command == 'mcp':
        from .mcp_server import run_stdio
        run_stdio()
        return
    if args.command == 'download-models':
        from .inference import download_models
        download_models()
        return
    if args.command == 'refresh-agy-model':
        from .agy_bridge import refresh_flash_model
        print(json.dumps(refresh_flash_model(agy_bin=args.agy_bin, model_file=args.model_file)))
        return
    if args.command == 'agy-bridge':
        import os, pathlib, uvicorn
        from .agy_bridge import create_bridge_app
        sock = pathlib.Path(args.socket)
        sock.parent.mkdir(parents=True, exist_ok=True)
        if sock.exists():
            sock.unlink()
        app = create_bridge_app(model_file=args.model_file or None, agy_bin=args.agy_bin)
        os.umask(0o777 - int(args.mode, 8))
        uvicorn.run(app, uds=str(sock), access_log=False)
        return
    settings = Settings.from_env()
    service, engine = build_service(settings)
    iid = settings.instance_id
    try:
        if args.command == 'worker':
            stopped = threading.Event()
            for sig in [signal.SIGTERM, signal.SIGINT]:
                signal.signal(sig, lambda *_: stopped.set())
            while not stopped.is_set():
                extracted = service.process_extractions(instance_id=iid, max_items=1)
                indexed = service.process_outbox(instance_id=iid, max_items=20)
                if any(extracted[k] or indexed[k] for k in ['processed', 'failed']):
                    print(json.dumps({'extraction': extracted, 'index': indexed}), flush=True)
                if args.once: break
                stopped.wait(max(0.1, args.interval))
            return
        if args.command == 'init': result = {'schema': 'initialized'}
        elif args.command == 'retry-index': result = service.process_outbox(instance_id=iid)
        elif args.command == 'retry-extraction': result = service.process_extractions(instance_id=iid)
        elif args.command == 'queue': result = service.queue_status(iid)
        elif args.command == 'reindex': result = {'queued': service.reindex(iid, reset=args.reset)}
        elif args.command in {'import-json', 'import-export'}:
            rows = iter_export(args.path, args.source)
            result = import_records(service, iid, rows)
        elif args.command == 'import-git': result = import_records(service, iid, iter_git_history(args.repository, max(1, args.max_commits)))
        elif args.command == 'import-shell': result = import_records(service, iid, iter_shell_history(args.path, args.source))
        print(json.dumps(result, default=str))
    finally:
        engine.dispose()

if __name__ == '__main__': main()
