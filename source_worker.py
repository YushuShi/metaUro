"""Run each preserved analysis engine in its own process and import namespace."""
from __future__ import annotations

import atexit
import importlib.util
import multiprocessing
import os
from pathlib import Path
import sys
import threading
import traceback

from automation.sync_sources import assembled_source, generation

ROOT = Path(__file__).resolve().parent
SOURCES = ('metaPee', 'MetaVir')


def protect_snapshot(root):
    """Reject writes to source snapshots, including accidental rebuilds."""
    root = str(root.resolve()) + os.sep

    def protected(value):
        return isinstance(value, (str, bytes, os.PathLike)) and (os.path.realpath(value) + os.sep).startswith(root)

    def audit(event, args):
        if event == 'open' and protected(args[0]):
            mode, flags = args[1:3]
            if (mode and any(c in mode for c in 'wax+')) or (flags and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                raise PermissionError('metaUro source snapshots are read-only')
        if event in {'os.remove', 'os.rmdir', 'os.rename', 'os.link', 'os.symlink', 'os.chmod', 'os.utime'}:
            if any(protected(p) for p in args[:2]):
                raise PermissionError('metaUro source snapshots are read-only')
        if event == 'os.mkdir' and protected(args[0]) and not os.path.isdir(args[0]):
            raise PermissionError('metaUro source snapshots are read-only')

    sys.addaudithook(audit)


def serve(source, pipe):
    sys.dont_write_bytecode = True
    folder = assembled_source(ROOT, source)
    runtime = ROOT / 'runtime' / source
    runtime.mkdir(parents=True, exist_ok=True)
    os.environ['MPLCONFIGDIR'] = str(runtime / 'matplotlib')
    os.environ['MPLBACKEND'] = 'Agg'
    os.environ['VERIFICATIONS_FILE'] = str(runtime / 'verifications.json')
    os.chdir(folder)
    # UI calculation updates are separate from the immutable evidence snapshot.
    sys.path[:0] = [str(ROOT / 'runtime_code' / source), str(folder), str(ROOT)]
    protect_snapshot(ROOT / 'sources')
    if folder != ROOT / 'sources' / source:
        protect_snapshot(folder)
    try:
        spec = importlib.util.spec_from_file_location('metauro_engine', folder / 'app.py')
        engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(engine)
        engine.LOG_FILE = str(runtime / 'engine.log')
        engine.app.config.update(TESTING=False)
        client = engine.app.test_client(use_cookies=False)
        pipe.send({'ready': True})
    except Exception:
        pipe.send({'error': traceback.format_exc()})
        return
    while True:
        try:
            message = pipe.recv()
        except EOFError:
            break
        if message is None:
            break
        try:
            response = client.open(**message)
            pipe.send((response.status_code, list(response.headers), response.get_data()))
        except Exception:
            pipe.send((500, [('Content-Type', 'application/json')], b'{"error":"The source analysis could not be completed."}'))
            traceback.print_exc()


class SourceWorker:
    def __init__(self, source):
        if source not in SOURCES:
            raise ValueError('Unknown evidence source')
        self.source, self.lock, self.process, self.pipe = source, threading.Lock(), None, None
        self.generation = None

    def _start(self):
        self.generation = generation(ROOT, self.source)
        context = multiprocessing.get_context('spawn')
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=serve, args=(self.source, child), daemon=True)
        self.process.start()
        child.close()
        if not self.pipe.poll(90):
            self.close()
            raise TimeoutError('Source engine startup timed out')
        ready = self.pipe.recv()
        if not ready.get('ready'):
            self.close()
            raise RuntimeError(ready.get('error', 'Source engine unavailable'))

    def request(self, **message):
        with self.lock:
            if self.generation != generation(ROOT, self.source):
                self.close()
            if not self.process or not self.process.is_alive():
                self._start()
            try:
                self.pipe.send(message)
                if not self.pipe.poll(180):
                    raise TimeoutError('Analysis timed out')
                return self.pipe.recv()
            except (TimeoutError, EOFError, BrokenPipeError):
                self.close()
                raise

    def close(self):
        if self.process:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=5)
        if self.pipe:
            self.pipe.close()
        self.process = self.pipe = None


workers = {source: SourceWorker(source) for source in SOURCES}


@atexit.register
def close_workers():
    for worker in workers.values():
        worker.close()
