#!/bin/bash
# 构建飞牛 fnOS 安装包 dockermigrator.fpk
#
# .fpk = gzip 压缩的 tar：
#   外层(扁平,无包裹目录): app.tgz  cmd/  config/  ICON.PNG  ICON_256.PNG  manifest  wizard/
#   其中 app/ 目录打包成内层 app.tgz（含 docker/ ui/ config/，含预置的 Python wheels）
#   manifest 的 checksum 字段 = app.tgz 的 MD5（fnOS 安装时校验，必须算对）
#
# 依赖预置：Python wheels 随包打进 app/docker/wheels，安装时 pip --no-index 离线安装，
#           不再访问 PyPI。wheels 为 py3.11 / manylinux2014_x86_64（对应 platform=x86）。
set -euo pipefail
cd "$(dirname "$0")"

NAME=dockermigrator
PY=${PYTHON:-python3}
WHEELS=app/docker/wheels

# 0. 预置 Python wheels（缺失则自动下载，py3.11 x86_64）
if [ ! -d "$WHEELS" ] || [ -z "$(ls -A "$WHEELS" 2>/dev/null)" ]; then
  echo ">> 下载 Python wheels（py3.11 / manylinux2014_x86_64）..."
  mkdir -p "$WHEELS"
  "$PY" -m pip download -r app/docker/requirements.txt -d "$WHEELS" \
    --python-version 311 --platform manylinux2014_x86_64 --only-binary=:all:
fi

# 0b. 预置 docker CLI + compose 插件（缺失则从国内镜像源下载静态二进制，x86_64）
BIN=app/docker/bin
mkdir -p "$BIN"
if [ ! -s "$BIN/docker" ]; then
  echo ">> 下载 docker CLI 静态二进制（24.0.7 / x86_64）..."
  # 优先使用国内镜像源，避免 docker hub 出口受限
  for url in \
    "https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/static/stable/x86_64/docker-24.0.7.tgz" \
    "https://mirrors.aliyun.com/docker-ce/linux/static/stable/x86_64/docker-24.0.7.tgz"; do
    if curl -fsSL --max-time 120 -o /tmp/docker.tgz "$url"; then
      tar -xzf /tmp/docker.tgz -C /tmp docker/docker && mv /tmp/docker/docker "$BIN/docker"
      break
    fi
  done
fi
if [ ! -s "$BIN/docker-compose" ]; then
  echo ">> 下载 docker compose 插件（v2.23.3 / x86_64）..."
  curl -fsSL --max-time 120 -o "$BIN/docker-compose" \
    "https://github.com/docker/compose/releases/download/v2.23.3/docker-compose-linux-x86_64"
fi
chmod +x "$BIN"/* 2>/dev/null || true
test -s "$BIN/docker"        || { echo "!! docker CLI 下载失败"; exit 1; }
test -s "$BIN/docker-compose" || { echo "!! docker-compose 下载失败"; exit 1; }

# 1. 生成图标（若缺失）
if [ ! -f ICON.PNG ] || [ ! -f app/ui/images/icon_64.png ]; then
  "$PY" gen_icons.py
fi

# 2. 打包 app/ -> app.tgz（含 docker/ + wheels + ui/ + config/）
rm -f app.tgz
tar czf app.tgz -C app docker ui config

# 3. 计算 checksum = app.tgz 的 MD5，写回 manifest
CHECKSUM=$("$PY" - <<'PY'
import hashlib
print(hashlib.md5(open("app.tgz","rb").read()).hexdigest())
PY
)
if grep -q '^checksum' manifest; then
  sed -i "s/^checksum.*/checksum              = ${CHECKSUM}/" manifest
else
  printf 'checksum              = %s\n' "$CHECKSUM" >> manifest
fi
echo "checksum(app.tgz) = $CHECKSUM"

# 4. 赋予 cmd 脚本可执行位
chmod +x cmd/*

# 5. 打包外层 .fpk
rm -f "${NAME}.fpk"
tar czf "${NAME}.fpk" app.tgz cmd config ICON.PNG ICON_256.PNG manifest wizard

echo "✓ 已生成 ${NAME}.fpk"
ls -la "${NAME}.fpk"
