# 阿里云 ECS Docker 部署指南

适用：把本仓库部署到阿里云 ECS，用 Docker 常驻运行 QQ 机器人 webhook 服务。

## 0 一条龙（已备案域名 + 自带 Caddy + 迁移已验证 runtime）

前提：ECS 安全组已放行 22/80/443，域名 A 记录已指向公网 IP。
细节与原理见后面各章。

```bash
# ── 1) 代码到 /opt/sana-bot（任选一种）──
sudo mkdir -p /opt/sana-bot && sudo chown "$USER" /opt/sana-bot
git clone <你的仓库地址> /opt/sana-bot && cd /opt/sana-bot

# ── 2) 迁移本机已校准/审批通过的运行数据（含索引与校准报告）──
rsync -av --exclude 'logs/*.log' ./runtime/ root@<ECS_IP>:/opt/sana-bot/runtime/
# 在 ECS 上：
sudo chown -R 10001:10001 /opt/sana-bot/runtime

# ── 3) 配置 .env（只改这几项，密钥不要外泄）──
cp .env.example .env && chmod 600 .env
vi .env        # DEEPSEEK_API_KEY / QQ_APP_ID / QQ_APP_SECRET
               # EMBEDDING_KIND=openai_compatible + EMBEDDING_API_KEY + EMBEDDING_BASE_URL
               # EMBEDDING_MODEL / EMBEDDING_DIMENSION / EMBEDDING_MAX_TOKENS 必须与索引一致
               # SANABOT_DOMAIN=bot.example.com   ACME_EMAIL=you@example.com

# ── 4) 构建并启动（应用 + Caddy）──
docker compose --profile tls up -d --build

# ── 5) 验证 ──
docker compose ps                                  # app 应为 (healthy)
curl -fsS https://bot.example.com/healthz          # {"status":"ok"}
curl -fsS https://bot.example.com/readyz           # ready=true 才算就绪
docker compose run --rm app doctor --offline       # 容器内只读体检
```

最后到 QQ 开放平台把回调地址填成 `https://bot.example.com/qq/webhook`，完成平台侧校验，
再用沙箱用户发一条消息确认收发。

> 证书由 Caddy 自动申请；首次访问前确认 80 端口可达（HTTP-01 校验需要）。
> 若 `docker compose ps` 里 app 一直不 healthy，先看 `docker compose logs app`。
> 第 4 步若报 Docker Hub 超时（`registry-1.docker.io ... i/o timeout`），先按 §5.1.1 处理。

## 1 先了解约束

| 约束 | 原因 |
| --- | --- |
| **只能单实例**（不要 `docker compose up --scale app=2`） | 入站去重靠 SQLite 唯一键、每用户串行队列在进程内存里，多副本会重复回复或乱序 |
| **`runtime/` 必须持久化** | SQLite（WAL）、索引快照（`vectors.npy` 等）、校准/评估报告都在里面；容器重建不能丢 |
| **必须 HTTPS 公网回调** | QQ 官方要求回调地址为 HTTPS，且校验签名依赖**原始请求体**（反向代理不能改写 body） |
| **镜像默认不含本地向量模型** | 默认构建只装远程向量后端依赖（`EMBEDDING_KIND=openai_compatible`），镜像小很多 |

## 2 前置条件

1. **ECS**：2 vCPU / 4 GiB 起，系统盘 ≥ 40 GiB，Ubuntu 22.04/24.04 或 Alibaba Cloud Linux 3。
2. **Docker**（含 compose 插件）：

   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo systemctl enable --now docker
   docker compose version
   ```

3. **安全组**：入方向只放行 `22/tcp`（限制来源 IP）、`80/tcp`、`443/tcp`（及 `443/udp`，HTTP/3 可选）。
   **不要**对公网开放 `8000`。
4. **域名**：一个解析到该 ECS 公网 IP 的域名。中国大陆地域使用 80/443 对外提供服务，
   按现行要求需完成 ICP 备案；未备案可先选香港/新加坡等地域，或用非 80/443 端口
   （QQ 回调仍需 HTTPS）。
5. **QQ 开放平台**：应用已获 C2C 文本能力，能配置回调地址与沙箱用户。

> `docker pull` 若报 `registry-1.docker.io ... i/o timeout`，先按 §5.1 配好镜像加速再往下走，
> 否则构建与 `--profile tls` 的 Caddy 拉取都会失败。

## 3 准备代码与运行数据

```bash
# 代码
git clone <你的仓库地址> /opt/sana-bot && cd /opt/sana-bot
# 或把本机目录整体 rsync 上去（排除 .venv/.git）

# 运行数据：把本机已核对/已启用的 runtime 目录带上去（含索引与校准报告）
rsync -av --exclude 'logs/*.log' ./runtime/ root@<ECS_IP>:/opt/sana-bot/runtime/
```

容器内以 uid `10001` 运行，宿主目录需要可写：

```bash
sudo chown -R 10001:10001 /opt/sana-bot/runtime
# 如果偏好用宿主的 uid/gid，见 docker-compose.yml 里 user: "${SANABOT_UID}:${SANABOT_GID}" 注释
```

**目录路径必须交代清楚**：compose 默认挂载项目目录下的 `./runtime`，而上面的迁移目标是
`/opt/sana-bot/runtime`。代码不在 `/opt/sana-bot` 时，二选一：

- 在 `.env` 里显式指定数据目录：`SANABOT_RUNTIME_DIR=/opt/sana-bot/runtime`（推荐，数据只放一处）
- 或把数据搬进项目目录：`mv /opt/sana-bot/runtime/* /path/to/project/runtime/ && sudo chown -R 10001:10001 /path/to/project/runtime`

否则 Docker 会按默认值在项目目录下新建一个 **root 属主的空 `runtime/`**，
容器内 uid `10001` 无法写入，启动即失败（症状见 §9）。

> 不想带 `runtime/`？那就必须在服务器上重建索引：见 [§7 在容器内管理索引](#7-在容器内管理索引)。
> 重建需要解析资料 + 校准 + 评估 + 审批 + 启用，成本高，首次部署建议直接迁移已验证的 `runtime/`。

## 4 配置 `.env`

```bash
cp .env.example .env
vi .env      # 只在本机/服务器上填真实值，绝不提交
```

必需项（缺失会**启动即失败**，不会静默降级）：

| 变量 | 说明 |
| --- | --- |
| `QQ_APP_ID` / `QQ_APP_SECRET` | 应用凭据；缺 `QQ_APP_SECRET` 时 `serve` 直接退出 |
| `DEEPSEEK_API_KEY` | 生成/事件提取/审核共用 |
| `EMBEDDING_KIND=openai_compatible` | 远程向量后端（镜像默认只装这套依赖） |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` | 向量服务地址与密钥（硅基流动等） |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` / `EMBEDDING_MAX_TOKENS` | 必须与**已建索引的编码器完全一致**，否则启动校验失败 |

非敏感但要注意：

```bash
RUNTIME_DIR=runtime          # 容器内会被 compose 覆盖为 /app/runtime
DEEPSEEK_MODEL=deepseek-flash
LANGCHAIN_TRACING_V2=false
```

使用 HTTPS 反向代理时，把下面两项也写进 `.env`（compose 会读取）：

```bash
SANABOT_DOMAIN=bot.example.com   # 解析到 ECS 的域名
ACME_EMAIL=you@example.com       # Let's Encrypt 通知邮箱
# SANABOT_PORT=8000              # 回环发布端口，可改
# SANABOT_RUNTIME_DIR=./runtime  # 宿主运行目录，可改成 /data/sana-bot/runtime
```

> **关于本地向量后端**：`langchain-huggingface` 与 `sentence-transformers` 是
> `pyproject.toml` 里的可选组 `local-embeddings`，默认镜像**不安装**（不含 torch/CUDA，
> 约 500MB）。所以在容器内跑 `EMBEDDING_KIND=local` 时必须改用
> `--build-arg INSTALL_LOCAL_EMBEDDINGS=1` 构建（镜像会增大约 1GB+），
> 并把模型缓存目录挂载进容器；缺少该可选组时启动会明确报
> `local_embeddings_extra_missing`，不会静默降级。除非确有必要，推荐继续用远程向量。

## 5 构建与启动

### 5.1 在 ECS 上直接构建（本文默认路径）

```bash
cd /opt/sana-bot
# 阿里云内网用 PyPI 镜像可显著提速（可选）
export UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple
docker compose build            # 默认镜像约 500MB（不含 torch/CUDA）
docker compose up -d
```

#### 5.1.1 Docker Hub 不可达（`i/o timeout`）时怎么办

大陆 ECS 常见 `registry-1.docker.io` 连不上，表现为：

- `! app Warning Get "https://registry-1.docker.io/v2/": net/http: request canceled`
- `failed to solve: ... resolve image config for docker.io/docker/dockerfile:1.7 ... i/o timeout`

两种处理方式，任选其一（**方式 A 更省事**）。

**方式 A：给 Docker 配镜像加速器**（对 `docker pull` 和构建都生效，只需做一次）

阿里云 ECS 用[容器镜像服务控制台](https://cr.console.aliyun.com/cn-hangzhou/instances/mirrors)里的
「镜像加速器」专有地址（形如 `https://<你的ID>.mirror.aliyuncs.com`），或任选一个公开加速地址：

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
  "registry-mirrors": ["https://<你的ID>.mirror.aliyuncs.com"]
}
JSON
sudo systemctl daemon-reload && sudo systemctl restart docker
docker info | grep -A2 'Registry Mirrors'    # 确认已生效
docker compose up -d --build
```

**方式 B：不改 daemon，直接指定可达的镜像来源**（`docker-compose.yml` 已把这些做成变量）

构建用的两处 Docker Hub 依赖都可以换掉：基础镜像 `python:3.12-slim` 与 uv 安装脚本。
先挑一个从这台机器可达的加速地址（返回 `401`/`200` 即可用，`000` 表示不通）：

```bash
for h in docker.1ms.run docker.m.daocloud.io docker.xuanyuan.me hub.rat.dev; do
  printf '%-24s ' "$h"; curl -s -o /dev/null -m 5 -w '%{http_code}\n' "https://$h/v2/"
done
```

```bash
cd /opt/sana-bot
# 写进 .env（持久生效，compose 会自动读取）
cat >> .env <<'EOF'
SANABOT_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
SANABOT_CADDY_IMAGE=docker.m.daocloud.io/library/caddy:2.8-alpine
EOF
docker compose --profile tls up -d --build
```

等价的一次性写法：`docker compose build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim`。
`astral.sh` 也拉不动时同理：`SANABOT_UV_INSTALLER_URL=<可达的 uv 安装脚本地址>`。

如果不想改任何变量，也可以先按官方名字打好标签，让 `Dockerfile` 与 compose 完全不用动：

```bash
docker pull docker.m.daocloud.io/library/python:3.12-slim && docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim
docker pull docker.m.daocloud.io/library/caddy:2.8-alpine && docker tag docker.m.daocloud.io/library/caddy:2.8-alpine caddy:2.8-alpine
```

> `Dockerfile` **不再**写 `# syntax=docker/dockerfile:1.7`。该指令会让 BuildKit 先去 Docker Hub 拉
> 前端镜像，是 `resolve image config ... dockerfile:1.7` 超时的直接原因；`RUN --mount=type=cache`
> 由内置前端支持，无需外部镜像。若确实要固定前端版本：
> `docker compose build --build-arg BUILDKIT_SYNTAX=docker.m.daocloud.io/docker/dockerfile:1.7`。
>
> 加速地址请以实际可达为准，示例用的是 `docker.m.daocloud.io`；换其他站点时把前缀整段替换即可。
> 另一条稳妥路线是先在本机构建并推到 ACR，服务器只 `pull`（见 §5.2）。

### 5.2 或：本机构建后推阿里云 ACR（可选）

```bash
docker build -t sana-bot:2026.10.25 .
docker tag sana-bot:2026.10.25 registry.cn-hangzhou.aliyuncs.com/<命名空间>/sana-bot:2026.10.25
docker login --username=<账号> registry.cn-hangzhou.aliyuncs.com
docker push registry.cn-hangzhou.aliyuncs.com/<命名空间>/sana-bot:2026.10.25
```

ECS 上把 `.env` 加上 `SANABOT_IMAGE=registry.cn-hangzhou.aliyuncs.com/<命名空间>/sana-bot:2026.10.25`，
然后 `docker compose pull && docker compose up -d`（无需在服务器构建）。

### 5.3 带 HTTPS 入口

```bash
docker compose --profile tls up -d
```

Caddy 会自动为 `SANABOT_DOMAIN` 申请并续期证书，HTTPS 反代到 `app:8000`；
证书数据放在命名卷 `sana-bot_caddy_data`（升级/重建容器不会丢证书，避免重复申领触发限流）。

已实测：经该反代转发的 `op=13` 回调校验能返回正确 `signature`，说明原始 body 未被改写
（签名校验依赖原始字节；反代只需 `reverse_proxy`，不要加 `request_body` 之类的改写）。

不想用 Caddy（已有 SLB/Nginx）时只跑 `docker compose up -d`：
应用只监听 `127.0.0.1:8000`，由前置代理转发到该端口，**同样必须保留原始 body**。

## 6 验证

```bash
# 1) 容器状态与健康检查
docker compose ps
docker compose logs -f app          # uvicorn 访问日志，不含密钥

# 2) 容器内只读体检（会报告索引、编码器、凭据是否就绪）
docker compose run --rm app doctor --offline

# 3) 存活与就绪
curl -fsS http://127.0.0.1:8000/healthz   # {"status":"ok"}
curl -fsS http://127.0.0.1:8000/readyz    # ready=true 才代表有已启用索引
```

`/healthz` 只表示进程活着（容器健康检查用它，避免索引未就绪时被反复重启）；
`/readyz` 返回 503 时看 `problems` 字段（`no_active_index`、`reply_calibration_missing` 等）。

带域名时：

```bash
curl -fsS https://$SANABOT_DOMAIN/healthz
```

最后到 QQ 开放平台把回调地址填成 `https://<域名>/qq/webhook`，完成平台侧校验，
再用沙箱用户发一条消息确认收发。

## 7 在容器内管理索引

入口脚本会把参数直接交给 `sana-bot`，运行目录默认 `/app/runtime`（已挂载）：

```bash
docker compose run --rm app index review --index <INDEX_ID>
docker compose run --rm app index evaluate --index <INDEX_ID> \
  --samples tests/fixtures/character_package/evaluation.jsonl --mode live --output /app/runtime/evaluation
docker compose run --rm app index approve-evaluation --index <INDEX_ID> --report <报告> --reviewer <人>
docker compose run --rm app index activate --index <INDEX_ID> --expected-active <当前active|none>
docker compose run --rm app index rollback --index <旧INDEX_ID> --expected-active <当前active> --reason rollback
```

镜像里没有 `tests/`，需要样本时把样本目录挂进来，或改用挂载的 `runtime/` 内路径。

## 8 备份、升级与回滚

### 备份（SQLite 建议用官方备份而不是直接拷文件）

```bash
cd /opt/sana-bot
mkdir -p backups
docker compose exec -T app python - <<'PY'
import sqlite3
src = sqlite3.connect('/app/runtime/sana-bot.sqlite3')
dst = sqlite3.connect('/app/runtime/backups/sana-bot.sqlite3.bak')
with dst:
    src.backup(dst)
print('backup ok')
PY
tar -czf backups/runtime-$(date +%F-%H%M).tar.gz runtime
```

### 升级

```bash
git pull                      # 或 docker compose pull
docker compose build          # 用 ACR 时可省略
docker compose up -d          # 只重建有变化的容器，索引与数据不动
docker compose run --rm app doctor --offline
```

### 回滚

```bash
# 镜像回滚
SANABOT_IMAGE=sana-bot:上一个版本 docker compose up -d
# 数据回滚
docker compose down
tar -xzf backups/runtime-<时间>.tar.gz
docker compose up -d
```

索引版本回滚用 `index rollback`（见 §7），业务数据（用户、关系、发送记录）不会被清空。

## 9 排障对照

| 现象 | 定位 |
| --- | --- |
| 启动即退出，日志 `qq_credentials_missing` | `QQ_APP_SECRET` 未注入 |
| `embedding_credentials_missing` / `embedding_revision_missing` | 远程向量缺 key/URL，或误用 `local` 却没给精确 revision |
| `embedding_dimension_mismatch` | `.env` 的维度与已建索引编码器不一致（换模型必须重建索引） |
| `embedding_probe_failed:embedding_bad_request` | 远程向量返回 400：`.env` 的 `EMBEDDING_MODEL` 与服务商实际提供的模型不符，或与索引里的 `embedding_model_id` 不一致。`cp .env.example .env` 会留下模板默认的 `bge-small-zh-v1.5`，而本项目的索引是 `BAAI/bge-large-zh-v1.5`/1024 维。对照索引改回 `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` / `EMBEDDING_MAX_TOKENS` 后再 `docker compose up -d` |
| `/readyz` 503 `no_active_index` | `runtime/` 没挂对，或索引未启用 |
| `/readyz` 503 `reply_calibration_missing` | 索引未完成校准/审批链 |
| 回调 401/403 | 签名校验失败：反向代理改了 body、或 `QQ_APP_SECRET` 不一致 |
| 回复显示「未送达」但页面有内容 | 测试 openid 不是沙箱用户，平台拒绝发送；属预期 |
| 日志 `database is locked` | 同时跑了多个实例或外部脚本直连写库；保持单实例 |
| 日志刷屏 `mkdir: cannot create directory '/app/runtime/logs': Permission denied`（容器反复 Restarting） | 挂载目录属主不是 `10001`。八成是 `SANABOT_RUNTIME_DIR` 没指向已 chown 的数据目录，compose 用了默认 `./runtime`。按 §3 设置路径并 `chown -R 10001:10001`。新版入口脚本会打印一行可照抄的 `chown` 命令后以 78 退出 |
| `local_embeddings_extra_missing` | `.env` 用了 `EMBEDDING_KIND=local` 但镜像未装可选组：改用 `--build-arg INSTALL_LOCAL_EMBEDDINGS=1` 重建，或切回远程向量 |
| `docker compose up` 报找不到 `.env` | compose 的 `env_file` 要求 `.env` 存在，先 `cp .env.example .env` |
| `Get "https://registry-1.docker.io/v2/": ... i/o timeout` | Docker Hub 不可达：按 §5.1.1 配镜像加速，或用 `SANABOT_BASE_IMAGE` / `SANABOT_CADDY_IMAGE` 指到加速地址 |
| `resolve image config for docker.io/docker/dockerfile:1.7 ... DeadlineExceeded` | 同上（旧版 `Dockerfile` 的 `# syntax` 指令会拉前端镜像）；当前 `Dockerfile` 已移除该指令，更新代码后重建即可 |

## 10 安全基线

- 只对公网开放 80/443；`8000` 仅在回环；SSH 限制来源 IP。
- `.env` 权限 `600`，不提交、不进镜像（`.dockerignore` 已排除）。
- 容器以非 root（uid 10001）运行；`runtime/` 属主设为该 uid。
- 日志用 json-file 轮转（compose 已设 10 MiB × 5），诊断正文默认保留 30 天，可 `logs prune`。
- 升级前先备份 `runtime/`；切换索引用 `index activate --expected-active`，失败会回滚到原版本。
