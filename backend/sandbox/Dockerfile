# Sandbox execution service (epic tr-s3y1). Slim, no DB/secrets. The manager runs as
# container-root holding ONLY a constrained cap set (compose: cap_drop ALL + cap_add
# [CHOWN,SETUID,SETGID,DAC_OVERRIDE,FOWNER]); every untrusted CHILD is dropped to a
# distinct uid from the pool below (tr-an0g.7) so one conversation's 0700 workspace
# files are unreadable to another. Untrusted code itself never runs as root.
FROM python:3.12-slim

# Per-execution uid pool (tr-an0g.7): 60000-60031 = persistent kernels, 60032-60039 =
# stateless runs. Each uid gets its own matching gid, no home (-M), nologin shell. The
# manager chowns each per-exec workdir to its uid and drops the child via Popen
# user=/group=. Numeric uids work even without these entries, but real passwd/group rows
# keep getpwuid()/id resolvable and the children unable to log in.
RUN for uid in $(seq 60000 60039); do \
        groupadd --gid "$uid" "sbx$uid" && \
        useradd --uid "$uid" --gid "$uid" --no-create-home --no-log-init \
                --shell /usr/sbin/nologin "sbx$uid"; \
    done

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Phase 4 (tr-5ktl): headless matplotlib + single-thread BLAS so openblas thread stacks
# don't eat into the 512 MB per-exec RLIMIT_AS. MPLCONFIGDIR is intentionally NOT global
# (tr-an0g.7): it is injected per-execution into <workdir>/.tmp/.mpl, owned by the exec
# uid, so a dropped child writes its font/config cache inside its own 0700 workdir and
# never to the shared /tmp.
ENV MPLBACKEND=Agg \
    OPENBLAS_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    OMP_NUM_THREADS=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The `sandbox` package lives at /app/sandbox so `uvicorn sandbox.server:app`
# and `python -m sandbox.runner` resolve.
COPY . /app/sandbox/

# Make `sandbox` importable even under `python -I` (isolated mode ignores
# PYTHONPATH and the cwd/script dir, but the `site` module STILL processes
# system-site `.pth` files). Point a .pth at /app so `import sandbox` resolves
# under the runner's `python -I -m sandbox.runner`.
RUN printf '/app\n' > "$(python -c 'import site; print(site.getsitepackages()[0])')/sandbox.pth"

# /workspace is a tmpfs at runtime (compose) mounted root-owned mode 0711; just create
# the mountpoint (the runtime mount replaces its ownership/mode). NO `USER` directive:
# the manager must run as root to create per-exec subdirs in 0711 /workspace, chown them
# to the exec uid, and setuid each child. The five capabilities it keeps are added back
# in compose; everything else is dropped.
RUN mkdir -p /workspace

EXPOSE 9000
CMD ["uvicorn", "sandbox.server:app", "--host", "0.0.0.0", "--port", "9000"]
