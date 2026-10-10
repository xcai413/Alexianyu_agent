FROM node:26-alpine AS web-build

WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    TZ=Asia/Shanghai \
    XIANYU_DATA_DIR=/data

# tzdata 让日志时间戳按 TZ 输出(业务时区固定 +08:00,与宿主机时区无关)。
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip install --no-cache-dir uv==0.12.10

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY alembic.ini ./
COPY src ./src
COPY --from=web-build /web/dist ./frontend/dist

# --frozen 保证镜像与 uv.lock 一致;--no-dev 不装测试依赖。
RUN uv sync --frozen --no-dev

COPY docker ./docker
RUN chmod +x /app/docker/entrypoint.sh

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data \
 && chown -R app:app /app /data
USER app

# 8089 提供 daemon 内嵌健康端点;8090 预留给 MCP Server。
EXPOSE 8089 8090

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
    CMD ["python", "/app/docker/healthcheck.py"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["daemon", "run"]
