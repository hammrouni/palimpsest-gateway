# syntax=docker/dockerfile:1

# =============================================================================
# base — shared runtime: uv, system packages, unprivileged user
# =============================================================================
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    # Outside /app, so the dev overlay's bind mount cannot shadow it.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# curl backs the healthcheck; setpriv (util-linux, in the base image) drops root.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 appuser

# `--frozen` installs exactly what uv.lock says, so the image matches CI.
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /usr/local/bin/

COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh


# =============================================================================
# runtime — production image (default target)
#
# Dependencies install first so their layer is cached across source edits.
# `app/` must be copied before the project install, or the package is empty.
# Installed non-editable: the running code comes from site-packages.
# =============================================================================
FROM base AS runtime

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project

COPY app/ ./app/
RUN uv sync --frozen --no-editable \
    && chown -R appuser:appuser /opt/venv

EXPOSE 8000

# The entrypoint provisions secrets as root, then runs the server as `appuser`.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# X-Forwarded-* is trusted only from FORWARDED_ALLOW_IPS (default 127.0.0.1).
# Set it to your reverse proxy's address, never "*".
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]


# =============================================================================
# dev — development image: dev dependencies and hot reload
#
# Editable install; docker-compose.dev.yml mounts the working tree over /app.
# =============================================================================
FROM base AS dev

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --extra dev

COPY app/ ./app/
COPY tests/ ./tests/
RUN uv sync --frozen --extra dev \
    && chown -R appuser:appuser /opt/venv

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]