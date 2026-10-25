"""Custom gunicorn worker for the prod image.

gunicorn launches N of these processes (`-w`). Each runs its own uvicorn server
and event loop, so a CPU-bound op in one worker can't freeze the others.

`limit_concurrency` is a uvicorn setting with no gunicorn CLI flag, so it's baked
into CONFIG_KWARGS here rather than passed on the command line. uvicorn deprecated
the bundled `uvicorn.workers` module in favour of the standalone `uvicorn-worker`
package; we import the new location first and fall back for older uvicorn.
"""
try:
    from uvicorn_worker import UvicornWorker as _BaseWorker
except ImportError:  # pragma: no cover - fallback for older uvicorn
    from uvicorn.workers import UvicornWorker as _BaseWorker

from app.config import settings


class TrinityUvicornWorker(_BaseWorker):
    CONFIG_KWARGS = {
        "loop": "uvloop",
        "http": "auto",
        # Per-worker global request cap. The chat endpoint additionally caps
        # concurrent SSE streams to 50 (app.api.v1.chat). Configurable via
        # settings (tr-3hs).
        "limit_concurrency": settings.worker_limit_concurrency,
    }
