# 飞牛 Docker 迁移工具（生产镜像，安装时由飞牛 appcenter 构建）
#
# 多架构兼容：x86_64 (AMD64) 和 ARM64 均可。
# docker CLI + compose 插件：构建时按容器架构自动下载对应二进制。
# Python 依赖：pip 在线安装，自动匹配容器架构的 wheel。
# rsync / sshpass / openssh-client：apt 安装，走飞牛 debian 源。

FROM python:3.11-slim

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

# 在线安装 Python 依赖（pip 根据容器架构自动选择正确的 wheel）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
