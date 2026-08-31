# 飞牛 Docker 迁移工具（生产镜像，安装时由飞牛 appcenter 构建）
#
# 依赖预置策略（尽量减少安装时联网）：
#   - Python wheels：随包提供于 wheels/，pip --no-index 离线安装，不访问 PyPI。
#   - docker CLI + compose 插件：随包提供于 bin/，COPY 进镜像，不访问 docker hub。
#   - rsync / sshpass / openssh-client：apt 安装，走飞牛 debian 源（国内快且稳）。
# 仅剩 python:3.11-slim 基础镜像需 docker hub 拉取（飞牛 NAS 通常已缓存或可走镜像加速）。

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    rsync sshpass openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# docker CLI + compose 插件（预置静态二进制，x86_64）
COPY bin/docker /usr/local/bin/docker
COPY bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
RUN chmod +x /usr/local/bin/docker /usr/local/lib/docker/cli-plugins/docker-compose

WORKDIR /app

# 离线安装 Python 依赖（wheels 已随包提供）
COPY wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
