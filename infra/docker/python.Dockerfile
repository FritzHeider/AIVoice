# syntax=docker/dockerfile:1.10
ARG PYTHON_IMAGE=python:3.12.7-slim-bookworm
FROM ${PYTHON_IMAGE} AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
WORKDIR /src
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY apps/ apps/
ARG SERVICE
RUN --mount=type=cache,target=/root/.cache/uv \
    package="$(sed -n 's/^name = "\(.*\)"$/\1/p' "apps/${SERVICE}/pyproject.toml" | head -n 1)" \
    && test -n "$package" \
    && uv sync --frozen --no-dev --package "$package" --no-editable

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tini libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 app && useradd -u 10001 -g app -s /usr/sbin/nologin -M app
COPY --from=builder /src/.venv /app/.venv
COPY models/ /app/models/
COPY assets/ /app/assets/
RUN install -d -o 10001 -g 10001 /app/.sim
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 VA_MODELS_DIR=/app/models VA_ASSETS_DIR=/app/assets
WORKDIR /app
USER 10001:10001
ARG MODULE
ENV VA_MODULE=${MODULE}
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec python -m ${VA_MODULE}"]
