#!/bin/bash
# 构建飞牛 fnOS 安装包 dockermigrator.fpk
#
# .fpk = gzip 压缩的 tar：
#   外层(扁平,无包裹目录): app.tgz  cmd/  config/  ICON.PNG  ICON_256.PNG  manifest  wizard/
#   其中 app/ 目录打包成内层 app.tgz（含 docker/ ui/ config/）
#   manifest 的 checksum 字段 = app.tgz 的 MD5（fnOS 安装时校验，必须算对）
#
# 多架构支持：platform=all
#   Docker 构建时由 Dockerfile 自动检测容器架构，下载对应的 docker CLI + compose 插件
#   Python 依赖由 pip 在线安装，自动匹配架构
#   基础镜像 python:3.11-slim 支持多架构（x86_64 + ARM64）
set -euo pipefail
cd "$(dirname "$0")"

NAME=dockermigrator
PY=${PYTHON:-python3}

# 1. 生成图标（若缺失）
if [ ! -f ICON.PNG ] || [ ! -f app/ui/images/icon_64.png ]; then
  "$PY" gen_icons.py
fi

# 2. 打包 app/ -> app.tgz（含 docker/ + ui/ + config/）
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
