FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project \
    && useradd --uid 10001 --create-home student
COPY --chown=student:student app ./app
COPY --chown=student:student mcp_servers ./mcp_servers
USER student
CMD ["python", "-m", "app"]
