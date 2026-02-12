"""Gunicorn production config."""

import os
import multiprocessing

# Server
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get('WEB_CONCURRENCY', multiprocessing.cpu_count() * 2 + 1))
worker_class = 'gthread'
threads = 4
timeout = 120
keepalive = 5
max_requests = 1000
max_requests_jitter = 50

# Logging
accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('LOG_LEVEL', 'info')
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Security
forwarded_allow_ips = '*'
proxy_protocol = False

# Lifecycle
preload_app = True
graceful_timeout = 30


def on_starting(server):
    print(f"[Gunicorn] Starting with {workers} workers on {bind}")


def post_fork(server, worker):
    print(f"[Gunicorn] Worker {worker.pid} spawned")
