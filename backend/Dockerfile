# tri-coach backend runtime image (PROJECT_BRIEF.md §6, §12.1).
# One image serves the api, worker and beat services; Compose overrides the
# command for worker/beat. Multi-stage: uv resolves locked dependencies into a
# virtualenv in the builder, then the venv + app package are copied into a slim
# runtime stage with no dev tooling installed.

FROM python:3.12-slim AS builder

# Official uv install pattern (uv Docker integration docs).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Layer cache: resolve the frozen lock first, without the project itself.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Install the tri-coach project (hatchling packages=["app"]) on top.
COPY app ./app
RUN uv sync --frozen --no-dev

FROM python:3.12-slim

RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

# Same /app path in both stages so venv console-script shebangs stay valid.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/app ./app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1

# Raw FIT archive mount point (§5.2, ING-6): the Compose volume is created
# root-owned, so the non-root app user needs a pre-owned directory to write to.
RUN mkdir -p /data/fit && chown app:app /data/fit

USER app

EXPOSE 8000

# api service (worker and beat override the command in Compose, §12.1).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
