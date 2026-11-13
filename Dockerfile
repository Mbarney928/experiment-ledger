# syntax=docker/dockerfile:1
# ---- build stage: resolve and install locked dependencies into a venv ----
FROM python:3.15-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_FROZEN=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --extra postgres --no-install-project
COPY src ./src
RUN uv sync --no-dev --extra postgres --no-editable

# ---- runtime stage: no build tools, non-root user, no secrets baked in ----
FROM python:3.15-slim AS runtime
RUN groupadd --system app && useradd --system --gid app --home /app --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"
# JWT_SECRET and DATABASE_URL must be provided at runtime (see docker-compose.yml).
CMD ["sh", "-c", "experiment-ledger migrate && exec uvicorn --factory experiment_ledger.main:app_factory --host 0.0.0.0 --port 8000 --proxy-headers"]
