"""
app/tasks/celery_app.py
───────────────────────
Celery application factory.

The module is imported by:
  - The FastAPI app (to enqueue tasks)
  - The Celery worker process (to execute tasks)
  - Flower (to monitor tasks)

Keep this module free of heavy imports so the FastAPI startup is fast.
"""

from __future__ import annotations

import logging
import ssl

from celery import Celery
from celery.signals import after_setup_logger, worker_ready, worker_shutdown
from kombu import Exchange, Queue

from app.core.config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Celery instance
# ─────────────────────────────────────────────────────────────────────────────
celery_app = Celery(
    "seo_analysis",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.pipeline"],          # auto-discover tasks
)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
celery_app.conf.update(
    # ── Serialisation ─────────────────────────────────────────────────────────
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # ── Timezone ──────────────────────────────────────────────────────────────
    timezone="UTC",
    enable_utc=True,

    # ── Task behaviour ────────────────────────────────────────────────────────
    task_track_started=True,               # STARTED state visible in backend
    task_acks_late=True,                   # ack after task completes (safer)
    task_reject_on_worker_lost=True,       # re-queue on worker crash
    worker_prefetch_multiplier=1,          # one task per worker at a time

    # ── Result backend ────────────────────────────────────────────────────────
    result_expires=settings.TASK_RESULT_TTL,
    result_extended=True,                  # store task args/kwargs/worker name

    # ── Retry / error handling ────────────────────────────────────────────────
    task_soft_time_limit=180,              # raises SoftTimeLimitExceeded at 3 min
    task_time_limit=240,                   # SIGKILL at 4 min (hard limit)
    task_max_retries=2,

    # ── Queues ────────────────────────────────────────────────────────────────
    task_queues=(
        Queue(
            "seo_analysis",
            Exchange("seo_analysis", type="direct"),
            routing_key="seo_analysis",
        ),
    ),
    task_default_queue="seo_analysis",
    task_default_exchange="seo_analysis",
    task_default_routing_key="seo_analysis",

    # ── Worker ────────────────────────────────────────────────────────────────
    worker_max_tasks_per_child=100,        # recycle worker after 100 tasks
    worker_disable_rate_limits=False,

    # ── Beat schedule (placeholder) ───────────────────────────────────────────
    beat_schedule={},

    # ── Redis-specific broker transport options ───────────────────────────────
    broker_transport_options={
        "visibility_timeout": 3600,        # re-queue task if not acked in 1 h
        "retry_policy": {
            "timeout": 5.0,
        },
    },

    # ── Connection pool ───────────────────────────────────────────────────────
    broker_pool_limit=10,
    redis_max_connections=20,
)

# ── SSL options: 仅在 rediss:// 时启用，redis:// 本地连接不需要 ────────────────
if settings.REDIS_URL.startswith("rediss://"):
    celery_app.conf.update(
        broker_use_ssl={
            "ssl_cert_reqs": ssl.CERT_REQUIRED,
        },
        redis_backend_use_ssl={
            "ssl_cert_reqs": ssl.CERT_REQUIRED,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Signals
# ─────────────────────────────────────────────────────────────────────────────
@after_setup_logger.connect
def configure_celery_logging(logger: logging.Logger, **kwargs: object) -> None:
    """Ensure Celery workers share the same log format as the app."""
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    for handler in logger.handlers:
        handler.setFormatter(formatter)


@worker_ready.connect
def on_worker_ready(**kwargs: object) -> None:
    logger.info("Celery worker is ready — broker: %s", settings.celery_broker_url)


@worker_shutdown.connect
def on_worker_shutdown(**kwargs: object) -> None:
    logger.info("Celery worker is shutting down")
