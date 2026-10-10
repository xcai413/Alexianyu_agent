#!/usr/bin/env bash
# 容器入口:迁移 → 健康门禁 → 启动 xianyu-agent。
# 迁移必须在这里做:daemon 启动路径不会自动建表/升级。
set -Eeuo pipefail

data_dir="${XIANYU_DATA_DIR:-/data}"

if ! mkdir -p "${data_dir}/logs" 2>/dev/null; then
  echo "[entrypoint] 无法创建数据目录 ${data_dir}" >&2
  echo "[entrypoint] 容器内 UID=$(id -u) GID=$(id -g);检查 bind mount 属主" >&2
  exit 1
fi

if [ ! -w "${data_dir}" ]; then
  echo "[entrypoint] 数据目录不可写:${data_dir}" >&2
  echo "[entrypoint] 容器内 UID=$(id -u) GID=$(id -g);宿主机执行:" >&2
  echo "[entrypoint]   sudo chown -R $(id -u):$(id -g) <宿主机数据目录>" >&2
  exit 1
fi

if [ "${XIANYU_SKIP_MIGRATIONS:-0}" != "1" ]; then
  echo "[entrypoint] alembic upgrade head"
  alembic upgrade head
fi

if [ "${XIANYU_SKIP_DOCTOR:-0}" != "1" ]; then
  echo "[entrypoint] doctor 门禁"
  if ! xianyu-agent doctor --startup; then
    echo "[entrypoint] doctor 存在 FAIL,拒绝启动(可设 XIANYU_SKIP_DOCTOR=1 跳过)" >&2
    exit 1
  fi
fi

exec xianyu-agent "$@"
