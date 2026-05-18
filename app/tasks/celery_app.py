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
    backend=None,
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

    # ── Async worker mode ─────────────────────────────────────────────────────
    # Use the native asyncio pool so async tasks run in a single event loop
    # per worker process — no per-task loop creation, no gevent monkey-patching.
    worker_pool="solo",         # solo pool: one coroutine loop per process
                                # concurrency is controlled at the asyncio level

    # ── Task behaviour ────────────────────────────────────────────────────────
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,

    # ── Retry / error handling ────────────────────────────────────────────────
    task_soft_time_limit=180,
    task_time_limit=240,
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
    worker_max_tasks_per_child=100,
    worker_disable_rate_limits=False,

    # ── Beat schedule (placeholder) ───────────────────────────────────────────
    beat_schedule={},

    # ── Redis-specific broker transport options ───────────────────────────────
    broker_transport_options={
        "polling_interval": 1.0,    # 从 5.0 降到 1.0，任务提交后更快被捡起
        "retry_policy": {
            "timeout": 5.0,
        },
    },

    # ── Connection pool ───────────────────────────────────────────────────────
    broker_pool_limit=8,            # 从 3 提高到 8
    redis_max_connections=10,
    broker_connection_retry_on_startup=True,
)

# ── SSL options: 仅在 rediss:// 时启用，redis:// 本地连接不需要 ────────────────
if settings.REDIS_URL.startswith("rediss://"):
    celery_app.conf.update(
        broker_use_ssl={
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
