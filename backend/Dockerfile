# Multi-stage build. `base` is the only build needed for development —
# docker-compose.yml mounts ./backend over /app and overrides CMD with
# uvicorn --reload, so source changes hot-reload. `prod` is what
# docker-compose.prod.yml targets; it bakes the source into the image
# and runs gunicorn with 4 uvicorn workers (true multi-core parallelism).

FROM python:3.12-slim AS base

WORKDIR /app

RUN pip install --upgrade pip --no-cache-dir

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[prod]"

COPY . .

# Default CMD matches the prod stage so building without a target
# (`docker build ./backend`) yields a runnable production image.
#
# gunicorn supervises 4 UvicornWorker processes. limit_concurrency (a uvicorn
# setting, not a gunicorn flag) lives in TrinityUvicornWorker.CONFIG_KWARGS.
# --timeout is gunicorn's worker-heartbeat timeout; UvicornWorker self-notifies
# the arbiter, so long-lived SSE streams aren't killed by it. --keep-alive is
# short now that a cheap /health/live exists for liveness probes.
CMD ["gunicorn", "app.main:app", \
     "-k", "app.core.worker.TrinityUvicornWorker", \
     "-w", "4", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--keep-alive", "5", \
     "--bind", "0.0.0.0:8000"]


FROM base AS prod
# Explicit target for docker-compose.prod.yml. Identical to base today;
# kept as a named stage so prod-specific tweaks (e.g. stripping dev tools)
# have a clear home later without disturbing the dev image.
