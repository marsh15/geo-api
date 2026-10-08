FROM python:3.13.16-slim-bookworm@sha256:fdf6f061c0e3829c7ee6753d20d008e4f76d725f95af53b62bcf53b503ffb0fa AS build

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.19
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13.16-slim-bookworm@sha256:fdf6f061c0e3829c7ee6753d20d008e4f76d725f95af53b62bcf53b503ffb0fa

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1
WORKDIR /app
RUN groupadd --system --gid 10001 geo_api \
    && useradd --system --uid 10001 --gid geo_api --home-dir /nonexistent --shell /usr/sbin/nologin geo_api
COPY --from=build /opt/venv /opt/venv
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY entrypoint.sh ./entrypoint.sh
RUN chmod 0555 /app/entrypoint.sh && mkdir -p /tmp/geo_api && chown geo_api:geo_api /tmp/geo_api
USER geo_api:geo_api
EXPOSE 8000
CMD ["/app/entrypoint.sh"]
