# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: Builder — install deps into a virtual environment
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# System build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create venv
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip + install wheels
COPY requirements.txt .
RUN pip install --upgrade pip wheel && \
    pip install --no-cache-dir -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: Runtime — lean final image
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Labels
LABEL maintainer="SEO Trust Path Team"
LABEL description="SEO Trust Path Analysis Service"

# Runtime system deps (curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Non-root user for security
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --no-create-home --shell /bin/bash appuser

# Working directory
WORKDIR /app

# Copy application source
COPY --chown=appuser:appgroup . .

# Switch to non-root
USER appuser

# Python optimizations
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PYTHONUTF8=1

# Expose port
EXPOSE 8000

# Default command (overridden in docker-compose per service)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
