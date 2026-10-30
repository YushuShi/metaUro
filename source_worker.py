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
    os.environ['META_PREPRINT_REPO_ROOT']=str(ROOT)
    os.environ['META_PREPRINT_SOURCE_REPO']=source
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
        sys.modules[spec.name]=engine
        spec.loader.exec_module(engine)
        from automation.sync_sources import _effective_file,load_overlay
        def preprint_cache_path_alias(path):
            relative=str(Path(path).resolve().relative_to(folder.resolve()))
            durable=_effective_file(ROOT,source,relative,load_overlay(ROOT,source))
            if durable is None:raise ValueError('The selected source cache is not in the active evidence generation.')
            return str(durable.resolve().relative_to(ROOT.resolve()))
        engine.preprint_cache_path_alias=preprint_cache_path_alias
        if source == 'MetaVir':
            from prostate_scopes import install_engine
            install_engine(engine)
        from runtime_code.trend_ci_runtime import install_core, install_completion
        install_core(sys.modules["meta_analysis"])
        install_completion()
        from display_localization import install_plot_localization
        from scripts import plot_localization
        install_plot_localization(plot_localization)
        # All source-app summary links read the single published output tree.
        from summary_outputs import summary_view_dir
        from flask import request, has_request_context
        from summary_outputs import latest_cohort_mode
        from preprint_ui import request_bool
        engine.active_plot_dir = lambda: str(summary_view_dir(ROOT, source, engine.subexposure_mode(),
            latest_cohort_mode(request.args.get('latest_cohort', request.args.get('keep_latest_cohort', 'yes'))) if has_request_context() else False,
            include_preprints=request_bool(request.args.get('include_preprints')) if has_request_context() else False))
        original_summary_manifest = engine.load_summary_manifest

        def source_summary_manifest():
            manifest = original_summary_manifest()
            provenance = manifest.get('sources', {}).get(source, {})
            return {**manifest, **provenance.get('metadata', {}),
                    'scopes': {key: value for key, value in manifest.get('scopes', {}).items()
                               if key in provenance.get('scopes', [])}}

        engine.load_summary_manifest = source_summary_manifest
        engine.LOG_FILE = str(runtime / 'engine.log')
        engine.app.config.update(TESTING=False)
        from runtime_code.sample_size_summary_native import install_app as install_sample_size_summary
        install_sample_size_summary(engine.app, engine)
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
