#!/bin/sh
# 容器入口：无参数时启动 QQ webhook 服务；带参数时直接转给 sana-bot CLI
# （例如 `docker compose run --rm app doctor --offline`）。
set -eu

RUNTIME_DIR="${RUNTIME_DIR:-/app/runtime}"

# 挂载目录属主不对时，裸 mkdir 只会刷屏 `Permission denied`；
# 这里提前给出可执行的修复指引（非 root 容器无法自行 chown）。
if ! mkdir -p "${RUNTIME_DIR}/logs" 2>/dev/null || [ ! -w "${RUNTIME_DIR}" ]; then
    echo "[entrypoint] 运行目录不可写：${RUNTIME_DIR}" >&2
    echo "[entrypoint] 容器内用户：$(id -u):$(id -g)（$(id -un)）" >&2
    echo "[entrypoint] 在宿主机把挂载源目录交给该 uid/gid：" >&2
    echo "[entrypoint]   sudo chown -R $(id -u):$(id -g) <宿主机上对应 ${RUNTIME_DIR} 的目录>" >&2
    echo "[entrypoint] 或让 compose 挂载已经 chown 好的目录：SANABOT_RUNTIME_DIR=/实际/路径" >&2
    echo "[entrypoint] 详见 doc/部署_Aliyun_Docker.md 第 3 节与第 9 节。" >&2
    exit 78
fi

if [ "$#" -gt 0 ]; then
    exec sana-bot "$@"
fi

: "${QQ_APP_SECRET:?QQ_APP_SECRET 未注入，服务拒绝启动（不会静默降级）}"

exec sana-bot serve \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --runtime-dir "${RUNTIME_DIR}"
