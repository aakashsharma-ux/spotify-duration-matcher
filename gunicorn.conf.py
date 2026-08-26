"""Gunicorn configuration for spotify-duration-matcher.

Defaults are tuned for Render's free web service tier (512 MB RAM / 0.1
CPU) so 80-90 users can use the tool reliably over time without the
process running out of memory. On a bigger box, raise GUNICORN_WORKERS.
Start with: gunicorn -c gunicorn.conf.py wsgi:application
"""

import multiprocessing
import os

# ── Workers ────────────────────────────────────────────────────────────────────
# IMPORTANT: defaults to a SINGLE worker process. `multiprocessing.cpu_count()`
# reports the HOST machine's core count, not the CPU quota actually granted
# to this container — on Render's free web service (512 MB RAM / 0.1 CPU)
# that old "2 × CPU + 1" formula could spawn 5-9 full worker PROCESSES, each
# a separate copy of Flask + every dependency loaded in memory. That blows
# past 512 MB, so the platform kills workers mid-request. From the browser
# this looks exactly like random "undefined" upload errors and requests
# that hang forever, because the connection was dropped mid-transfer.
#
# One worker + several THREADS (gthread) covers this workload fine, since
# uploads, Google Sheets calls, and ffprobe are I/O-bound, not CPU-bound —
# threads share one process's memory instead of multiplying it. Raise
# GUNICORN_WORKERS explicitly if this ever moves to a bigger instance.
cpu_count = multiprocessing.cpu_count()
workers   = int(os.getenv("GUNICORN_WORKERS", 1))

# Threads per worker: uploads + audio scanning are I/O-bound, so extra
# threads help far more than extra processes on a memory-constrained host.
threads   = int(os.getenv("GUNICORN_THREADS", 6))

# Worker class: sync works fine; use "gthread" for better thread utilisation.
worker_class = "gthread"

# ── Network ───────────────────────────────────────────────────────────────────
# Render (and most PaaS free tiers) inject a PORT env var the app must bind
# to; fall back to 5050 for local runs. Set GUNICORN_BIND to override both.
bind    = os.getenv("GUNICORN_BIND", f"0.0.0.0:{os.getenv('PORT', '5050')}")
backlog = 2048

# ── Timeouts ──────────────────────────────────────────────────────────────────
# Duration extraction of large libraries can take 30-60 s on first run.
timeout       = int(os.getenv("GUNICORN_TIMEOUT", 180))
keepalive     = 5
graceful_timeout = 30

# ── Memory management ─────────────────────────────────────────────────────────
# Recycle workers periodically to prevent slow memory leaks.
max_requests          = 1000
max_requests_jitter   = 100   # randomise so all workers don't restart at once

# ── Logging ───────────────────────────────────────────────────────────────────
accesslog  = "-"           # stdout
errorlog   = "-"           # stderr
loglevel   = os.getenv("LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── Server mechanics ──────────────────────────────────────────────────────────
preload_app  = True        # load app once in master → shared memory; faster startup
daemon       = False       # let the process manager (systemd, Docker) handle daemonising
forwarded_allow_ips = "*"  # trust X-Forwarded-For (set to your proxy IP in production)

# ── Hooks ─────────────────────────────────────────────────────────────────────
def on_starting(server):
    server.log.info(
        "Duration Matcher starting — %d worker(s) × %d thread(s) "
        "(host reports %d CPU core(s); worker count is intentionally "
        "decoupled from that on memory-constrained hosting)",
        workers, threads, cpu_count,
    )

def worker_exit(server, worker):
    server.log.info("Worker %d exited.", worker.pid)
