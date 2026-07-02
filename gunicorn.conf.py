"""Gunicorn configuration for spotify-duration-matcher.

Tuned for 80-90 concurrent users on a typical 2-4 core server.
Start with: gunicorn -c gunicorn.conf.py wsgi:application
"""

import multiprocessing
import os

# ── Workers ────────────────────────────────────────────────────────────────────
# Formula: 2 × CPU + 1 gives good utilisation for mixed I/O + CPU workloads.
cpu_count = multiprocessing.cpu_count()
workers   = int(os.getenv("GUNICORN_WORKERS", (2 * cpu_count) + 1))

# Threads per worker: audio scanning is I/O-bound, so extra threads help.
threads   = int(os.getenv("GUNICORN_THREADS", 4))

# Worker class: sync works fine; use "gthread" for better thread utilisation.
worker_class = "gthread"

# ── Network ───────────────────────────────────────────────────────────────────
bind    = os.getenv("GUNICORN_BIND", "0.0.0.0:5050")
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
    server.log.info("Duration Matcher starting — %d worker(s) × %d thread(s)", workers, threads)

def worker_exit(server, worker):
    server.log.info("Worker %d exited.", worker.pid)
