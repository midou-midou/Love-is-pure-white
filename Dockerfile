#
# 乾纱凪机器人运行镜像。
# 单进程、单实例：SQLite + 内存队列 + 每用户串行，禁止横向扩容。
#
# 默认 INSTALL_LOCAL_EMBEDDINGS=0：只装远程向量后端（EMBEDDING_KIND=openai_compatible）需要的依赖，
# 不会拉 torch / CUDA（本地模型依赖是 pyproject 里的 local-embeddings 可选组）。
# 要在容器内跑本地 HuggingFace 模型：--build-arg INSTALL_LOCAL_EMBEDDINGS=1。
#
# 国内网络构建：这里**故意不写** `# syntax=docker/dockerfile:1.7`，
# 否则 BuildKit 会先去 Docker Hub 拉 docker/dockerfile 前端镜像（registry-1.docker.io 不通时报
# resolve image config ... i/o timeout）。`RUN --mount=type=cache` 由内置前端支持。
# 基础镜像仍来自 Docker Hub，Docker Hub 不可达时用 BASE_IMAGE 指到加速地址：
#   docker compose build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
# 需要固定前端版本时，用环境变量 BUILDKIT_SYNTAX=<加速地址>/docker/dockerfile:1.7 传入。

ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# 安装脚本地址可覆盖（astral.sh 不可达时换成可达的镜像/内网地址）
ARG UV_INSTALLER_URL=https://astral.sh/uv/install.sh

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf "${UV_INSTALLER_URL}" | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && uv --version

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

ARG INSTALL_LOCAL_EMBEDDINGS=0
# 阿里云可用镜像源，例如 https://mirrors.aliyun.com/pypi/simple
ARG UV_DEFAULT_INDEX=

RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; \
    if [ -z "${UV_DEFAULT_INDEX}" ]; then unset UV_DEFAULT_INDEX; fi; \
    if [ "${INSTALL_LOCAL_EMBEDDINGS}" = "1" ]; then \
        uv sync --frozen --no-dev --extra local-embeddings; \
    else \
        uv sync --frozen --no-dev; \
    fi; \
    /opt/venv/bin/python -c "import sana_bot, uvicorn, fastapi; print('deps ok')"


FROM ${BASE_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    RUNTIME_DIR=/app/runtime \
    PORT=8000 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 sana \
    && useradd --uid 10001 --gid sana --create-home --shell /usr/sbin/nologin sana

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# 运行时需要的源码与资料：CLI 管理命令（index/doctor/logs）也在镜像内可用
COPY --chown=sana:sana src ./src
COPY --chown=sana:sana config ./config
COPY --chown=sana:sana doc ./doc
COPY --chown=sana:sana pyproject.toml uv.lock README.md ./
COPY --chown=sana:sana deploy/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod 0755 /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/runtime/logs \
    && chown -R sana:sana /app/runtime /app/config

USER sana
EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request,sys; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
