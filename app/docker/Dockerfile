# 飞牛 Docker 迁移工具 — 构建镜像
#
# 关键点：
#   1. apt 源用清华镜像（默认 deb.debian.org 在国内 NAS 上经常超时/拒绝）
#   2. 先改源再 apt-get update，避免 exit code 100
#   3. docker CLI + compose 插件按架构自动选择
#   4. pip 用清华 PyPI 镜像

FROM python:3.11-slim

# 换 Debian apt 源到清华镜像
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
    rsync sshpass openssh-client ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# docker CLI + compose 插件（按容器架构自动选择对应二进制）
RUN set -eux; \
    ARCH=$(dpkg --print-architecture); \
    case "$ARCH" in \
      amd64) DOCKER_ARCH=x86_64;  COMPOSE_ARCH=x86_64;  ;; \
      arm64) DOCKER_ARCH=arm64;   COMPOSE_ARCH=aarch64; ;; \
      *) echo "!! 不支持的架构: $ARCH"; exit 1; ;; \
    esac; \
    curl -fsSL --max-time 120 \
      "https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/static/stable/${DOCKER_ARCH}/docker-24.0.7.tgz" \
      | tar -xz -C /tmp; \
    mv /tmp/docker/docker /usr/local/bin/docker; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL --max-time 120 -o /usr/local/lib/docker/cli-plugins/docker-compose \
      "https://github.com/docker/compose/releases/download/v2.23.3/docker-compose-linux-${COMPOSE_ARCH}"; \
    chmod +x /usr/local/bin/docker /usr/local/lib/docker/cli-plugins/docker-compose; \
    rm -rf /tmp/docker

WORKDIR /app

# pip 用清华源 + 在线安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY . .
RUN chmod +x /app/entrypoint.sh

# 显式以 root 运行，规避 UID/GID 不一致带来的挂载点 Permission denied。
# 上层若以 user 覆盖则以覆盖者为准，但 entrypoint 诊断会提示当前身份。
USER root

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
