"""Start the published source worker with request-only plot presentation."""
import multiprocessing

from source_worker import ROOT, SourceWorker, generation, serve


def _install_web_views(app, engine):
    import analysis_settings_backend
    from main_plot_presentation import install
    from sample_size_summary_web import install_app

    install(analysis_settings_backend)
    install_app(app, engine)


def _serve_web(source, pipe):
    # This module runs in a dedicated spawned worker. Keep the worker startup,
    # analysis imports and sealed offline renderers unchanged.
    from runtime_code import sample_size_summary_native
    sample_size_summary_native.install_app = _install_web_views
    serve(source, pipe)


class WebSourceWorker(SourceWorker):
    def _start(self):
        self.generation = generation(ROOT, self.source)
        context = multiprocessing.get_context('spawn')
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=_serve_web, args=(self.source, child), daemon=True)
        self.process.start()
        child.close()
        if not self.pipe.poll(90):
            self.close()
            raise TimeoutError('Source engine startup timed out')
        ready = self.pipe.recv()
        if not ready.get('ready'):
            self.close()
            raise RuntimeError(ready.get('error', 'Source engine unavailable'))


def install_workers(workers):
    """Upgrade the web host's lazy source workers before its first request."""
    for source, worker in workers.items():
        if type(worker) is SourceWorker:
            workers[source] = WebSourceWorker(source)
