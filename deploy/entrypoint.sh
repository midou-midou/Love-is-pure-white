#!/bin/sh
# 容器入口：无参数时启动 QQ webhook 服务；带参数时直接转给 sana-bot CLI
# （例如 `docker compose run --rm app doctor --offline`）。
set -eu

RUNTIME_DIR="${RUNTIME_DIR:-/app/runtime}"
mkdir -p "${RUNTIME_DIR}/logs"

if [ "$#" -gt 0 ]; then
    exec sana-bot "$@"
fi

: "${QQ_APP_SECRET:?QQ_APP_SECRET 未注入，服务拒绝启动（不会静默降级）}"

exec sana-bot serve \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --runtime-dir "${RUNTIME_DIR}"
