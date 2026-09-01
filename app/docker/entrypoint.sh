#!/bin/bash
# 容器启动入口：先打印 uid/gid + 挂载点诊断（方便排查本地映射模式权限问题），再启动 uvicorn
set -eu

echo "=========================================="
echo " Docker Migrator 启动诊断"
echo "=========================================="
echo " 当前用户：uid=$(id -u) gid=$(id -g) user=$(whoami 2>/dev/null || 'N/A')"
echo " 时区：    ${TZ:-N/A}"
echo " 本机 Docker 根目录(LOCAL_DOCKER_ROOT)：${LOCAL_DOCKER_ROOT:-N/A}"
echo " 源 NAS 挂载路径(SOURCE_MOUNT)：         ${SOURCE_MOUNT:-<未填写，本地映射模式请在安装向导或 compose 中配置>}"
echo "------------------------------------------"
echo " 容器内已挂载的卷清单（仅供排障）："
if command -v mount >/dev/null 2>&1; then
  mount | grep -E '^/dev| /vol| /mnt' || echo "  (未发现 /vol /mnt /dev 前缀挂载点)"
else
  cat /proc/mounts | awk '{print $2}' | grep -E '^/vol|^/mnt|/docker$|/docker/' || echo "  (未发现 /vol /mnt /docker 挂载点)"
fi
echo "------------------------------------------"
echo " 检查关键目录可读/可写："
for p in "$LOCAL_DOCKER_ROOT" "$SOURCE_MOUNT"; do
  [ -z "$p" ] && continue
  if [ -d "$p" ]; then
    if [ -r "$p" ] && [ -x "$p" ]; then
      rw="✅ 可读+可遍历"
      if [ -w "$p" ]; then
        rw="$rw +可写"
      else
        rw="$rw，只读"
      fi
    else
      rw="⚠️ 存在但无遍历权限（Permission denied）"
    fi
    # 取几个典型文件的 uid/gid 样本
    sample=$(find "$p" -maxdepth 2 -type f 2>/dev/null | head -3)
    if [ -n "$sample" ]; then
      ids=""
      for f in $sample; do
        ids="$ids $(stat -c '%U(%u):%G(%g)' "$f" 2>/dev/null)"
      done
      echo "    $p -> $rw  （文件样例 uid/gid：$ids）"
    else
      echo "    $p -> $rw  （未找到子文件样例）"
    fi
  else
    echo "    $p -> ❌ 目录不存在（容器内未挂载该路径！）"
  fi
done
echo "=========================================="

exec uvicorn main:app --host 0.0.0.0 --port 8080
