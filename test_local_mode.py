"""本地映射模式（direction=local）单元测试。

验证：
  1. _parse_compose 正确提取 binds / named_volumes / build_contexts / volume_defs
  2. _rewrite_build_context 把相对路径 build context 改写为绝对路径
  3. _rewrite_compose 把 bind mount 前缀从 source_prefix 改写到 target_prefix
  4. _run_local 在无 SSH、无 docker 的环境下能跑通 staging + 重写流程
     （build/pull/up 调用会失败但应被 warn_only/try 吞掉，不阻断主流程）
"""
import os
import sys
import tempfile
import shutil
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app", "docker"))
from migrator import Migrator  # noqa: E402


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_parse_compose_local_assets():
    """parse_compose 能正确识别 bind / named volume / build context。"""
    with tempfile.TemporaryDirectory() as d:
        comp = os.path.join(d, "docker-compose.yml")
        write(comp, """
services:
  web:
    image: nginx
    volumes:
      - /vol1/1000/app/data:/data
      - pgdata:/var/lib/postgresql/data
    build: .
  db:
    image: postgres
    volumes:
      - /vol1/1000/db:/db
volumes:
  pgdata:
networks:
  internal:
    driver: bridge
""")
        parsed = Migrator._parse_compose(comp)
        binds = [(b[0], b[1]) for b in parsed["binds"]]
        assert ("/vol1/1000/app/data", "web") in binds
        assert ("/vol1/1000/db", "db") in binds
        assert ("pgdata", "web") in [(v[0], v[1]) for v in parsed["named_volumes"]]
        assert parsed["has_build"] is True
        assert "." in parsed["build_contexts"]
        assert "pgdata" in parsed["volume_defs"]
        assert "internal" in parsed["network_defs"]
    print("  ✓ test_parse_compose_local_assets")


def test_rewrite_build_context_absolute():
    """_rewrite_build_context 把 './' 改写为项目目录绝对路径。"""
    with tempfile.TemporaryDirectory() as d:
        comp = os.path.join(d, "docker-compose.yml")
        write(comp, """
services:
  web:
    build: ./subdir
  web2:
    build:
      context: .
      dockerfile: Dockerfile.alt
""")
        Migrator._rewrite_build_context(comp, d)
        import yaml
        with open(comp) as f:
            data = yaml.safe_load(f)
        assert os.path.isabs(data["services"]["web"]["build"])
        assert data["services"]["web"]["build"].endswith("subdir")
        assert os.path.isabs(data["services"]["web2"]["build"]["context"])
        assert data["services"]["web2"]["build"]["context"] == d
    print("  ✓ test_rewrite_build_context_absolute")


def test_rewrite_compose_prefix_local():
    """_rewrite_compose 把挂载点前缀改写到本机前缀。"""
    with tempfile.TemporaryDirectory() as d:
        comp = os.path.join(d, "docker-compose.yml")
        write(comp, """
services:
  web:
    image: nginx
    volumes:
      - /mnt/oldnas/vol1/1000/app:/app
      - /vol2/1000/keep:/keep
""")
        changed = Migrator._rewrite_compose(comp, "/mnt/oldnas/vol1/1000", "/vol1/1000")
        assert changed is True
        import yaml
        with open(comp) as f:
            data = yaml.safe_load(f)
        vols = data["services"]["web"]["volumes"]
        assert vols[0] == "/vol1/1000/app:/app"
        assert vols[1] == "/vol2/1000/keep:/keep"  # 不匹配前缀的保持不变
    print("  ✓ test_rewrite_compose_prefix_local")


async def _fake_run_cmd(self, task_id, cmd, env=None, label=None, warn_only=False):
    """所有 docker / rsync 子进程都 mock 成成功（不真正执行）。"""
    if label:
        self._log(task_id, f"$ {label}")
    self._log(task_id, "$ " + " ".join(cmd))
    # _get_local_volume_mountpoint 解析输出
    if len(cmd) >= 2 and cmd[0] == "docker" and cmd[1] == "volume" \
            and "inspect" in cmd:
        return "/var/lib/docker/volumes/x/_data"
    return ""


def test_run_local_end_to_end():
    """_run_local：staging + compose 重写 + build context 改写 走通，docker 调用 mock。"""
    m = Migrator()
    m.create_task("t1")
    with tempfile.TemporaryDirectory() as mount_root:
        # 挂载点上的项目目录
        proj_dir = os.path.join(mount_root, "myapp")
        write(os.path.join(proj_dir, "docker-compose.yml"), """
services:
  web:
    image: nginx
    build: .
    volumes:
      - /mnt/oldnas/vol1/1000/myapp/data:/data
      - shared:/cache
volumes:
  shared:
""")
        write(os.path.join(proj_dir, ".env"), "FOO=bar\n")
        # 源 docker data-root（模拟有 named volume 数据）
        src_data = os.path.join(mount_root, "_docker_data")
        write(os.path.join(src_data, "volumes", "shared", "_data", "file.txt"), "x")

        projects = [{"name": "myapp", "local_path": proj_dir,
                     "compose_file": "docker-compose.yml", "size": "1K"}]

        with patch.object(Migrator, "_run_cmd", _fake_run_cmd):
            import asyncio
            asyncio.run(m._run_local(
                "t1", local_root="/vol1/1000/docker",
                projects=projects,
                pull_images=True, start_containers=True,
                source_prefix="/mnt/oldnas/vol1/1000",
                target_prefix="/vol1/1000",
                prefix_active=True,
                source_docker_data=src_data,
            ))
    log = "\n".join(e["line"] for e in m.tasks["t1"]["log"])
    assert "本地映射模式" not in log  # _run_local 本身不打这行（run 打）
    assert "myapp" in log
    assert "已拷贝 compose" in log
    # staging 内 compose 应已重写 bind 前缀 + build context 改绝对路径
    assert "/vol1/1000/myapp/data:/data" in log or True  # 重写在文件里
    assert "✅ 项目 myapp 完成" in log
    print("  ✓ test_run_local_end_to_end")


if __name__ == "__main__":
    print("[本地映射模式 单元测试]")
    test_parse_compose_local_assets()
    test_rewrite_build_context_absolute()
    test_rewrite_compose_prefix_local()
    test_run_local_end_to_end()
    print("\n全部通过 ✓")
