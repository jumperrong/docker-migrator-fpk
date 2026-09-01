"""迁移逻辑（通用版：pull + push 两种模式，全要素无损迁移）。

pull  模式：部署在「目标 NAS」，SSH 连接源 NAS，拉项目目录 + bind mount + named volumes + build context →
           本机按 compose 重写路径、create volume / network、compose build（如有）+ pull + up。
push  模式：部署在「源 NAS」，扫描本机项目 → staging 重写 compose → 推送项目目录 + bind mount + named volumes →
           目标 NAS create volume / network、compose build（如有）+ pull + up。

覆盖的要素：
  ✅ docker-compose.yml / compose.yaml（含长/短语法）
  ✅ bind mount（绝对路径）+ 路径前缀映射重写
  ✅ named volumes（数据在 /var/lib/docker/volumes/<name>/_data）
  ✅ custom networks（driver + options，跳过 external）
  ✅ build 指令（build context 随项目目录一起同步）
  ✅ .env 文件（在项目目录内，自动随 rsync 一起走）
  ✅ 镜像（compose pull；build 项目先 compose build）
  ✅ 文件权限 / 软链 / symlink（rsync -a --numeric-ids --copy-links）
"""
import os
import shlex
import asyncio
import shutil
import tempfile

import yaml

from ssh_client import SSHClient


class Migrator:
    def __init__(self):
        self.tasks = {}  # task_id -> {status, log:[...]}

    # ==========================================================================
    # Docker 常见错误 → 中文修复建议（保持原有）
    # ==========================================================================
    DOCKER_ERROR_HINTS = [
        (["i/o timeout"],
         "💡 修复建议：镜像仓库 443 出网超时。请检查该 NAS 出网；国内环境可在 Docker daemon.json "
         "配 registry-mirrors 镜像加速器，或配置 HTTP_PROXY/HTTPS_PROXY。"),
        (["context deadline exceeded"],
         "💡 修复建议：Docker 拉取触发 context deadline exceeded。典型原因是对端 registry 太慢或不可达。"
         "建议在对应 NAS 上配 registry-mirrors 或代理；或关闭『拉取镜像』手动 docker save/load 离线 tarball。"),
        (["TLS handshake timeout"],
         "💡 修复建议：registry TLS 握手超时 = HTTPS 链路慢/被墙。配镜像加速器/代理即可；"
         "自建仓请确认证书 CN 匹配域名且未过期。"),
        (["no such host"],
         "💡 修复建议：DNS 解析失败（no such host）。检查该 NAS /etc/resolv.conf 是否能解析镜像仓库域名；"
         "常见：私有仓内网 DNS 未配置、或对端 NAS 使用了只能本局域网解析的域名。"),
        (["lookup .* on .*: no such host"],
         "💡 修复建议：DNS 解析失败。检查该 NAS /etc/resolv.conf；私有仓内网 DNS 需可达。"),
        (["pull access denied"],
         "💡 修复建议：pull access denied = 对该镜像无权访问。通常原因：① 私有仓未 docker login；"
         "② 镜像名/namespace 拼写错误；③ 公网镜像限流稍后或配 registry-mirrors。"),
        (["repository does not exist"],
         "💡 修复建议：repository does not exist。请核对 compose image 字段（拼写、tag、registry 前缀）；"
         "私有仓请先 docker login。"),
        (["unauthorized", "authentication required"],
         "💡 修复建议：镜像仓 401 Unauthorized。请在对应 NAS 上先 docker login <镜像仓域名>。"),
        (["denied: requested access to the resource is denied"],
         "💡 修复建议：镜像仓 denied = 登录账号无拉权限。换账号或联系镜像仓管理员授权。"),
        (["manifest unknown"],
         "💡 修复建议：manifest unknown = tag 不存在（可能 compose 写错 tag、latest 被清）。"
         "核对源 NAS docker images 的实际 tag，改 compose 后重试。"),
        (["manifest for .* not found"],
         "💡 修复建议：镜像 tag 未找到。核对 compose 中 image:tag 与源 NAS docker images 输出是否一致；"
         "如果源是本地 build 没 push，关『拉取镜像』手动 docker save/load。"),
        (["connection reset by peer", "EOF"],
         "💡 修复建议：registry 连接被重置 / EOF，出口不稳或被限流。"
         "配 registry-mirrors / 离线 docker save / 闲时重试。"),
        (["Cannot connect to the Docker daemon"],
         "💡 修复建议：无法连接 Docker daemon。检查：① 该 NAS 是否已装并启动 Docker 应用；"
         "② /var/run/docker.sock 权限（飞牛 docker-project 资源自动授权）。"),
        (["docker: command not found"],
         "💡 修复建议：docker 命令不存在。推模式目标 NAS 需先装 Docker + compose v2 插件。"),
        (["\"docker\": executable file not found"],
         "💡 修复建议：docker 命令不存在。推模式目标 NAS 需先装 Docker + compose v2 插件。"),
        (["compose is not a docker command"],
         "💡 修复建议：缺 compose v2 插件。升级 Docker 或安装 docker-compose-plugin。"),
        (["port is already allocated"],
         "💡 修复建议：宿主机端口被占用。docker ps 看占用 → 停冲突容器或改 compose host 端口。"),
        (["no space left on device"],
         "💡 修复建议：目标 NAS 磁盘空间不足。docker system prune -a 清理或换大容量卷。"),
    ]

    # 权限类错误 → 中文修复建议（本地映射模式 rsync/compose 读取挂载点文件）
    PERMISSION_ERROR_HINTS = [
        (["Permission denied", "permission denied", "EACCES", "权限不够"],
         "💡 权限修复建议（本地映射模式）：常见原因 ① NFS/USB 挂载时启用了 root_squash / all_squash "
         "将容器内 uid=0 映射成 nobody，源目录属主又不是 nobody；② 源目录 POSIX 位只有属主可读；"
         "③ mount 参数是 ro 而挂载点恰好需要遍历写入。排查：在飞牛主机 `ls -ld <挂载路径>` + "
         "`id`，对比容器日志启动诊断的 uid/gid 是否匹配；NFS 改 /etc/exports 加 no_root_squash，"
         "或挂载参数 anonuid=0,anongid=0；或对整个挂载点递归 chmod o+rX 给『其他用户』可读。"),
        (["No such file or directory", "does not exist", "不存在"],
         "💡 路径修复建议（本地映射模式）：源目录/文件不存在 —— 大概率是「挂载点没有透传到容器内」。"
         "请检查 compose.yaml volumes 是否包含该路径，或重新安装向导中「源 NAS 挂载根路径」是否填写；"
         "容器日志启动诊断中会输出当前容器可见的挂载卷，与用户填写路径对比即可定位。"),
        (["Read-only file system", "EROFS", "read-only"],
         "💡 只读文件系统修复：源数据读取为 ro 没问题；但 named volume 复制 / compose staging 写入时若报 "
         "EROFS，请确认本机 LOCAL_DOCKER_ROOT 所在挂载是 rw，以及 docker volumes 目录 (默认 "
         "/var/lib/docker/volumes) 是 rw，飞牛 compose 中目标卷需写 ':rw'。"),
    ]

    @classmethod
    def _diagnose_permission_error(cls, all_lines):
        """rsync / compose 出错时匹配权限类修复建议。"""
        if not all_lines:
            return None
        import re
        blob = "\n".join(all_lines).lower()
        for kws, hint in cls.PERMISSION_ERROR_HINTS:
            hit = False
            for kw in kws:
                if re.search(kw.lower(), blob):
                    hit = True
                    break
            if hit:
                return hint
        return None

    @classmethod
    def _diagnose_docker_error(cls, all_lines):
        if not all_lines:
            return None
        import re
        blob = "\n".join(all_lines).lower()
        for kws, hint in cls.DOCKER_ERROR_HINTS:
            all_hit = True
            for kw in kws:
                if not re.search(kw.lower(), blob):
                    all_hit = False
                    break
            if all_hit:
                return hint
        return None

    # ==========================================================================
    # 任务状态
    # ==========================================================================
    def create_task(self, task_id):
        self.tasks[task_id] = {"status": "pending", "log": []}

    def _log(self, task_id, line, stream="stdout"):
        task = self.tasks.get(task_id)
        if task is None:
            return
        task["log"].append({"line": line, "stream": stream})

    # ==========================================================================
    # 执行本机子进程
    # ==========================================================================
    async def _run_cmd(self, task_id, cmd, env=None, label=None, warn_only=False):
        if label:
            self._log(task_id, f"$ {label}")
        self._log(task_id, "$ " + " ".join(shlex.quote(c) for c in cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as e:
            if warn_only:
                self._log(task_id, f"警告：命令不存在 {e}，跳过", "stderr")
                return None
            raise RuntimeError(f"命令不存在：{e}")

        all_lines = []

        async def drain(stream, stream_name):
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip("\r\n")
                if text:
                    self._log(task_id, text, stream_name)
                    all_lines.append(text)

        await asyncio.gather(drain(proc.stdout, "stdout"), drain(proc.stderr, "stderr"))
        rc = await proc.wait()
        if rc != 0:
            if warn_only:
                self._log(task_id, f"警告：exit={rc}（warn_only，继续）", "stderr")
                return None
            hinted = False
            if cmd and (cmd[0].endswith("docker") or "docker" in cmd[0]):
                hint = self._diagnose_docker_error(all_lines)
                if hint:
                    self._log(task_id, hint, "stderr")
                    hinted = True
            # 权限类修复建议（rsync/cp/ls 等所有本地命令）
            if not hinted:
                perm_hint = self._diagnose_permission_error(all_lines)
                if perm_hint:
                    self._log(task_id, perm_hint, "stderr")
                    hinted = True
            if not hinted:
                self._log(task_id,
                          "💡 通用排查：① docker info 正常吗？② 路径/镜像/tag 存在吗？③ 磁盘(df -h)和权限(ls -ld)？",
                          "stderr")
            raise RuntimeError(f"命令失败 (exit={rc})")
        return "\n".join(all_lines)

    # ==========================================================================
    # 远端命令（推模式）
    # ==========================================================================
    async def _run_remote_cmd(self, task_id, remote_cfg, cmd, label=None, warn_only=False):
        if label:
            self._log(task_id, f"$ {label}（远端）")
        self._log(task_id, "$ [remote] " + " ".join(shlex.quote(c) for c in cmd))

        client = SSHClient(
            remote_cfg["host"], remote_cfg["port"], remote_cfg["username"],
            remote_cfg.get("password"), remote_cfg.get("key_path"),
        )

        loop = asyncio.get_running_loop()

        def _run_sync():
            lines = []
            def on_line(text, stream):
                self._log(task_id, text, stream)
                lines.append(text)
            ok, tail = client.exec_stream(cmd, on_line)
            return ok, tail, lines

        ok, tail, lines = await loop.run_in_executor(None, _run_sync)
        if not ok:
            if warn_only:
                self._log(task_id, f"警告：远端命令失败 {tail}（warn_only，继续）", "stderr")
                return None
            is_docker = any("docker" in str(c).lower() for c in cmd)
            if is_docker:
                hint = self._diagnose_docker_error(lines)
                if hint:
                    self._log(task_id, hint + "（以上提示针对目标 NAS 远端环境）", "stderr")
                else:
                    self._log(task_id,
                              "💡 远端通用排查：① docker info 正常？② compose 镜像/tag 存在？③ 磁盘和出网通畅？",
                              "stderr")
            raise RuntimeError(f"远端命令失败 {tail}")
        return "\n".join(lines)

    # ==========================================================================
    # compose 解析 — bind mounts / named volumes / networks / build
    # ==========================================================================
    @staticmethod
    def _parse_compose(compose_path):
        """一次性解析 compose 文件，返回 {binds, named_volumes, networks, has_build}。

        binds:         [(src_host_path, svc_name), ...]  # 绝对路径 bind mount
        named_volumes: [(vol_name, svc_name), ...]       # 非绝对路径 = named volume
        volume_defs:   {vol_name: {external: bool, driver: str|None, opts: dict}}
        network_defs:  {net_name: {external: bool, driver: str|None, opts: dict,
                                   attachable: bool, enable_ipv6: bool, subnets: list}}
        has_build:     bool                               # 任一 service 有 build
        build_contexts: set(str)                          # 相对路径 build context（相对 compose 文件所在目录）
        """
        with open(compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return {"binds": [], "named_volumes": [], "volume_defs": {},
                    "network_defs": {}, "has_build": False, "build_contexts": set()}

        binds = []
        named_volumes = []
        has_build = False
        build_contexts = set()

        for svc_name, svc in (data.get("services") or {}).items():
            # ---- volumes ----
            for v in (svc.get("volumes") or []):
                if isinstance(v, str):
                    parts = v.split(":")
                    if len(parts) < 2:
                        continue
                    if parts[0].startswith("/"):
                        binds.append((parts[0], svc_name))
                    else:
                        named_volumes.append((parts[0], svc_name))
                elif isinstance(v, dict):
                    src = v.get("source")
                    target = v.get("target", "")
                    vol_type = v.get("type", "volume")
                    if vol_type == "bind" and isinstance(src, str) and src.startswith("/"):
                        binds.append((src, svc_name))
                    elif vol_type == "volume":
                        if isinstance(src, str) and not src.startswith("/"):
                            named_volumes.append((src, svc_name))
                        elif isinstance(target, str) and target.startswith("/"):
                            # 无 name 的 named volume 用 target 的哈希？不可能无 name 但有 target
                            pass

            # ---- build ----
            build = svc.get("build")
            if build:
                has_build = True
                if isinstance(build, str):
                    ctx = build
                elif isinstance(build, dict):
                    ctx = build.get("context", ".")
                else:
                    ctx = "."
                build_contexts.add(ctx)

        # ---- top-level volumes ----
        volume_defs = {}
        for name, vdef in (data.get("volumes") or {}).items():
            vdef = vdef or {}
            volume_defs[name] = {
                "external": bool(vdef.get("external", False)),
                "driver": vdef.get("driver"),
                "opts": vdef.get("driver_opts") or {},
            }

        # ---- top-level networks ----
        network_defs = {}
        for name, ndef in (data.get("networks") or {}).items():
            ndef = ndef or {}
            network_defs[name] = {
                "external": bool(ndef.get("external", False)),
                "driver": ndef.get("driver"),
                "opts": ndef.get("driver_opts") or {},
                "attachable": bool(ndef.get("attachable", False)),
                "enable_ipv6": bool(ndef.get("enable_ipv6", False)),
            }

        return {
            "binds": binds,
            "named_volumes": named_volumes,
            "volume_defs": volume_defs,
            "network_defs": network_defs,
            "has_build": has_build,
            "build_contexts": build_contexts,
        }

    # ==========================================================================
    # compose 重写 — bind 路径前缀映射
    # ==========================================================================
    @staticmethod
    def _rewrite_compose(compose_path, source_prefix, target_prefix):
        if not source_prefix or source_prefix == target_prefix:
            return False
        with open(compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return False
        changed = False
        for svc in (data.get("services") or {}).values():
            vols = svc.get("volumes")
            if not isinstance(vols, list):
                continue
            for i, v in enumerate(vols):
                if isinstance(v, str):
                    parts = v.split(":")
                    if len(parts) >= 2 and parts[0].startswith(source_prefix):
                        parts[0] = target_prefix + parts[0][len(source_prefix):]
                        vols[i] = ":".join(parts)
                        changed = True
                elif isinstance(v, dict) and isinstance(v.get("source"), str) \
                        and v["source"].startswith(source_prefix):
                    v["source"] = target_prefix + v["source"][len(source_prefix):]
                    changed = True
        if changed:
            with open(compose_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False,
                               default_flow_style=False)
        return changed

    # ==========================================================================
    # rsync helpers
    # ==========================================================================
    def _rsync_env(self, remote_cfg):
        rsh_base = (
            f"ssh -p {remote_cfg['port']} "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            "-o ConnectTimeout=20"
        )
        env = os.environ.copy()
        if remote_cfg.get("password"):
            rsh = f"sshpass -e {rsh_base}"
            env["SSHPASS"] = remote_cfg["password"]
        else:
            rsh = rsh_base
        return rsh, env

    async def _rsync_pull(self, task_id, remote_cfg, remote_src, local_dst, label=None):
        """从远端 remote_src/ rsync 拉到本地 local_dst/。"""
        parent = os.path.dirname(local_dst.rstrip("/"))
        if parent:
            os.makedirs(parent, exist_ok=True)
        src = remote_src if remote_src.endswith("/") else remote_src + "/"
        dst = local_dst if local_dst.endswith("/") else local_dst + "/"
        rsh, env = self._rsync_env(remote_cfg)
        cmd = ["rsync", "-avX", "--numeric-ids", "-e", rsh,
               f"{remote_cfg['username']}@{remote_cfg['host']}:{src}", dst]
        await self._run_cmd(task_id, cmd, env=env, label=label)

    async def _rsync_push(self, task_id, remote_cfg, local_src, remote_dst, label=None):
        """从本机 local_src/ rsync 推到远端 remote_dst/。"""
        # 先在远端 mkdir -p（兼容密码 / 密钥两种认证）
        ssh_base = (
            f"ssh -p {remote_cfg['port']} "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            "-o ConnectTimeout=20"
        )
        if remote_cfg.get("password"):
            mdir_cmd = f"sshpass -e {ssh_base}"
            mdir_env = os.environ.copy()
            mdir_env["SSHPASS"] = remote_cfg["password"]
        else:
            mdir_cmd = ssh_base
            mdir_env = None
        mdir_cmd += (
            f" {remote_cfg['username']}@{remote_cfg['host']} "
            f"mkdir -p {shlex.quote(remote_dst)}"
        )
        mdir = await asyncio.create_subprocess_exec(
            "bash", "-lc", mdir_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=mdir_env,
        )
        _, err = await mdir.communicate()
        rc = mdir.returncode
        if rc != 0:
            detail = err.decode(errors="replace").strip().splitlines()[-1] if err else ""
            self._log(task_id,
                      f"警告：远端 mkdir {remote_dst} 失败 rc={rc} {detail}（后续 rsync 仍尝试继续）",
                      "stderr")

        src = local_src if local_src.endswith("/") else local_src + "/"
        dst = remote_dst if remote_dst.endswith("/") else remote_dst + "/"
        rsh, env = self._rsync_env(remote_cfg)
        cmd = ["rsync", "-avX", "--numeric-ids", "-e", rsh,
               src, f"{remote_cfg['username']}@{remote_cfg['host']}:{dst}"]
        await self._run_cmd(task_id, cmd, env=env, label=label)

    # ==========================================================================
    # 查找 compose 文件
    # ==========================================================================
    @staticmethod
    def _find_compose_file(proj_dir, preferred_name=None):
        candidates = [preferred_name] if preferred_name else []
        candidates += ["docker-compose.yml", "docker-compose.yaml",
                       "compose.yml", "compose.yaml"]
        for c in candidates:
            if c is None:
                continue
            p = os.path.join(proj_dir, c)
            if os.path.exists(p):
                return p, os.path.basename(p)
        return None, None

    # ==========================================================================
    # named volume 辅助 — 拿源/目标 volume 的真实 mountpoint
    # ==========================================================================
    async def _get_local_volume_mountpoint(self, task_id, vol_name):
        """本机 docker volume inspect，返回 Mountpoint。"""
        out = await self._run_cmd(
            task_id,
            ["docker", "volume", "inspect", "-f", "{{.Mountpoint}}", vol_name],
            warn_only=True,
        )
        if out and out.strip():
            return out.strip()
        return None

    async def _get_remote_volume_mountpoint(self, task_id, remote_cfg, vol_name):
        """远端 docker volume inspect。"""
        out = await self._run_remote_cmd(
            task_id, remote_cfg,
            ["docker", "volume", "inspect", "-f", "{{.Mountpoint}}", vol_name],
            warn_only=True,
        )
        if out and out.strip():
            return out.strip()
        return None

    async def _ensure_local_volume(self, task_id, vol_name, vdef):
        """本机 docker volume create（若不存在）。"""
        existing = await self._get_local_volume_mountpoint(task_id, vol_name)
        if existing:
            self._log(task_id, f"  本机 volume {vol_name} 已存在 ({existing})")
            return existing
        cmd = ["docker", "volume", "create"]
        driver = (vdef or {}).get("driver")
        if driver:
            cmd += ["--driver", driver]
        for k, v in ((vdef or {}).get("opts") or {}).items():
            cmd += ["--opt", f"{k}={v}"]
        cmd.append(vol_name)
        await self._run_cmd(task_id, cmd, label=f"docker volume create {vol_name}")
        return await self._get_local_volume_mountpoint(task_id, vol_name)

    async def _ensure_remote_volume(self, task_id, remote_cfg, vol_name, vdef):
        """远端 docker volume create（若不存在）。"""
        existing = await self._get_remote_volume_mountpoint(task_id, remote_cfg, vol_name)
        if existing:
            self._log(task_id, f"  远端 volume {vol_name} 已存在 ({existing})")
            return existing
        cmd = ["docker", "volume", "create"]
        driver = (vdef or {}).get("driver")
        if driver:
            cmd += ["--driver", driver]
        for k, v in ((vdef or {}).get("opts") or {}).items():
            cmd += ["--opt", f"{k}={v}"]
        cmd.append(vol_name)
        await self._run_remote_cmd(task_id, remote_cfg, cmd,
                                   label=f"远端 docker volume create {vol_name}")
        return await self._get_remote_volume_mountpoint(task_id, remote_cfg, vol_name)

    # ==========================================================================
    # network 辅助 — create（跳过 external）
    # ==========================================================================
    async def _ensure_local_network(self, task_id, net_name, ndef):
        """本机 docker network create（如不存在、非 external）。"""
        ndef = ndef or {}
        if ndef.get("external"):
            self._log(task_id, f"  网络 {net_name} 声明为 external，跳过创建")
            return
        cmd = ["docker", "network", "ls", "--filter", f"name=^{net_name}$", "-q"]
        out = await self._run_cmd(task_id, cmd, warn_only=True)
        if out and out.strip():
            self._log(task_id, f"  本机 network {net_name} 已存在")
            return
        ncmd = ["docker", "network", "create"]
        driver = ndef.get("driver")
        if driver:
            ncmd += ["--driver", driver]
        if ndef.get("attachable"):
            ncmd.append("--attachable")
        if ndef.get("enable_ipv6"):
            ncmd.append("--ipv6")
        for k, v in (ndef.get("opts") or {}).items():
            ncmd += ["--opt", f"{k}={v}"]
        ncmd.append(net_name)
        await self._run_cmd(task_id, ncmd, label=f"docker network create {net_name}")

    async def _ensure_remote_network(self, task_id, remote_cfg, net_name, ndef):
        """远端 docker network create。"""
        ndef = ndef or {}
        if ndef.get("external"):
            self._log(task_id, f"  网络 {net_name} 声明为 external，跳过创建")
            return
        cmd = ["docker", "network", "ls", "--filter", f"name=^{net_name}$", "-q"]
        out = await self._run_remote_cmd(task_id, remote_cfg, cmd, warn_only=True)
        if out and out.strip():
            self._log(task_id, f"  远端 network {net_name} 已存在")
            return
        ncmd = ["docker", "network", "create"]
        driver = ndef.get("driver")
        if driver:
            ncmd += ["--driver", driver]
        if ndef.get("attachable"):
            ncmd.append("--attachable")
        if ndef.get("enable_ipv6"):
            ncmd.append("--ipv6")
        for k, v in (ndef.get("opts") or {}).items():
            ncmd += ["--opt", f"{k}={v}"]
        ncmd.append(net_name)
        await self._run_remote_cmd(task_id, remote_cfg, ncmd,
                                   label=f"远端 docker network create {net_name}")

    # ==========================================================================
    # 本地 rsync（无 SSH，用于 local 模式 named volume 数据复制）
    # ==========================================================================
    async def _rsync_local(self, task_id, src, dst, label=None):
        """本机到本机的 rsync（无 SSH）。"""
        parent = os.path.dirname(dst.rstrip("/"))
        if parent:
            os.makedirs(parent, exist_ok=True)
        os.makedirs(dst, exist_ok=True)
        src = src if src.endswith("/") else src + "/"
        dst = dst if dst.endswith("/") else dst + "/"
        cmd = ["rsync", "-avX", "--numeric-ids", src, dst]
        await self._run_cmd(task_id, cmd, label=label)

    # ==========================================================================
    # build context 改写（local 模式：相对路径 → 绝对路径指向挂载点项目目录）
    # ==========================================================================
    @staticmethod
    def _rewrite_build_context(compose_path, project_dir):
        """将 compose 中相对路径的 build context 改写为绝对路径。

        local 模式下 staging 只拷贝了 compose 文件，build context（如 '.'/'./subdir'）
        需改为指向挂载点上的原始项目目录，否则 compose build 找不到 Dockerfile。
        """
        with open(compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return
        changed = False
        for svc in (data.get("services") or {}).values():
            build = svc.get("build")
            if not build:
                continue
            if isinstance(build, str):
                if not os.path.isabs(build):
                    svc["build"] = os.path.normpath(os.path.join(project_dir, build))
                    changed = True
            elif isinstance(build, dict):
                ctx = build.get("context", ".")
                if not os.path.isabs(ctx):
                    build["context"] = os.path.normpath(os.path.join(project_dir, ctx))
                    changed = True
        if changed:
            with open(compose_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False,
                               default_flow_style=False)

    # ==========================================================================
    # 统一入口
    # ==========================================================================
    async def run(self, task_id, direction, remote, local_docker_root, remote_docker_root,
                  projects, pull_images=True, start_containers=True,
                  source_prefix="", target_prefix="", source_docker_data=""):
        task = self.tasks[task_id]
        task["status"] = "running"

        prefix_active = bool(source_prefix) and source_prefix != target_prefix
        try:
            if direction == "pull":
                self._log(task_id, f"[拉模式] 开始迁移 {len(projects)} 个项目（从源 NAS → 本机）")
                await self._run_pull(task_id, remote, local_docker_root, projects,
                                     pull_images, start_containers,
                                     source_prefix, target_prefix, prefix_active)
            elif direction == "push":
                self._log(task_id, f"[推模式] 开始迁移 {len(projects)} 个项目（从本机 → 目标 NAS）")
                await self._run_push(task_id, remote, local_docker_root, remote_docker_root, projects,
                                     pull_images, start_containers,
                                     source_prefix, target_prefix, prefix_active)
            else:
                self._log(task_id, f"[本地映射模式] 开始迁移 {len(projects)} 个项目（源 NAS 硬盘已挂载到本机）")
                await self._run_local(task_id, local_docker_root, projects,
                                     pull_images, start_containers,
                                     source_prefix, target_prefix, prefix_active,
                                     source_docker_data)

            task["status"] = "done"
            self._log(task_id, "\n===== 全部迁移完成 =====")
        except Exception as e:
            task["status"] = "error"
            self._log(task_id, f"错误: {e}", "stderr")
            raise

    # ==========================================================================
    # 拉模式 — 目标 NAS 部署
    # ==========================================================================
    async def _run_pull(self, task_id, remote, local_root, projects,
                        pull_images, start_containers,
                        source_prefix, target_prefix, prefix_active):
        if prefix_active:
            self._log(task_id, f"路径前缀映射：{source_prefix} -> {target_prefix}")
        else:
            self._log(task_id,
                      "未启用路径前缀映射：要求源/目标 NAS 路径完全一致，否则外部 bind mount 会失效",
                      "stderr")

        remote_user = remote["username"]
        remote_host = remote["host"]

        for idx, proj in enumerate(projects, 1):
            name = proj["name"]
            remote_path = proj.get("remote_path") or f"(missing remote_path for {name})"
            compose_name = proj.get("compose_file", "docker-compose.yml")
            local_dst = os.path.join(local_root, name)

            self._log(task_id, f"\n{'='*10} [{idx}/{len(projects)}] {name} {'='*10}")
            self._log(task_id, f"源: {remote_user}@{remote_host}:{remote_path}")
            self._log(task_id, f"目标: {local_dst}")
            os.makedirs(local_root, exist_ok=True)

            # ---- 1) rsync 拉项目目录（含 compose、.env、build context）----
            await self._rsync_pull(task_id, remote, remote_path, local_dst,
                                   label=f"rsync 拉取项目 {name}")

            # ---- 2) 找 compose ----
            compose_file, compose_name = self._find_compose_file(local_dst, compose_name)
            if not compose_file:
                self._log(task_id,
                          f"⚠️ 警告：未找到 compose 文件，跳过 {name} 的镜像/容器/数据操作",
                          "stderr")
                continue

            # ---- 3) 解析 compose：binds + named_volumes + networks + build ----
            try:
                parsed = self._parse_compose(compose_file)
            except Exception as e:
                self._log(task_id, f"警告：解析 compose 失败: {e}", "stderr")
                parsed = {"binds": [], "named_volumes": [], "volume_defs": {},
                          "network_defs": {}, "has_build": False, "build_contexts": set()}

            binds = parsed["binds"]
            named_vols = parsed["named_volumes"]
            volume_defs = parsed["volume_defs"]
            network_defs = parsed["network_defs"]
            has_build = parsed["has_build"]
            build_contexts = parsed["build_contexts"]

            if build_contexts:
                self._log(task_id, f"  发现 build 指令，contexts: {sorted(build_contexts)}")

            # ---- 4) bind mount 同步（排除项目目录内部的）----
            rp_norm = os.path.normpath(remote_path)
            lp_norm = os.path.normpath(local_dst)
            for bp, svc in binds:
                bp_norm = os.path.normpath(bp)
                # 项目内部路径跳过（已经随项目 rsync 过来了）
                if bp_norm == rp_norm or bp_norm.startswith(rp_norm + "/"):
                    continue
                # 也可能是本地已有绝对路径但属于项目内部（build context 相对路径）
                # 前缀映射处理
                if prefix_active:
                    if bp.startswith(source_prefix):
                        local_target = target_prefix + bp[len(source_prefix):]
                    else:
                        self._log(task_id,
                                  f"跳过外部 bind mount {bp}：路径不以前缀 {source_prefix!r} 开头",
                                  "stderr")
                        continue
                else:
                    local_target = bp
                # 如果 local_target 在 lp_norm 下（本地项目目录内），也跳过
                lt_norm = os.path.normpath(local_target)
                if lt_norm == lp_norm or lt_norm.startswith(lp_norm + "/"):
                    continue
                self._log(task_id, f"  同步外部 bind mount [{svc}]: {bp} -> {local_target}")
                try:
                    await self._rsync_pull(task_id, remote, bp, local_target,
                                           label=f"rsync bind {bp}")
                except Exception as e:
                    self._log(task_id, f"⚠️ 警告：同步 {bp} 失败: {e}（继续）", "stderr")

            # ---- 5) named volume 同步 ----
            vol_names = set()
            for vn, svc in named_vols:
                vol_names.add(vn)
            for vn in vol_names:
                vdef = volume_defs.get(vn, {"external": False})
                if vdef.get("external"):
                    self._log(task_id, f"  volume {vn} 声明为 external，跳过")
                    continue
                self._log(task_id, f"  同步 named volume: {vn}")
                try:
                    # 源 NAS 的 mountpoint
                    remote_mp = await self._run_remote_cmd(
                        task_id, remote,
                        ["docker", "volume", "inspect", "-f", "{{.Mountpoint}}", vn],
                        warn_only=True,
                    )
                    if not remote_mp or not remote_mp.strip():
                        # 源 NAS volume 可能不存在（空 volume）
                        self._log(task_id,
                                  f"    源 NAS volume {vn} 不存在或为空，先在本机 create 一个空 volume")
                        await self._ensure_local_volume(task_id, vn, vdef)
                        continue
                    remote_mp = remote_mp.strip()
                    # 确保本机有同名 volume
                    local_mp = await self._ensure_local_volume(task_id, vn, vdef)
                    if not local_mp:
                        self._log(task_id, f"⚠️ 本机 create volume {vn} 失败，跳过数据同步", "stderr")
                        continue
                    # rsync 远端 _data 到本机 _data
                    await self._rsync_pull(task_id, remote, remote_mp, local_mp,
                                           label=f"rsync volume {vn} 数据")
                except Exception as e:
                    self._log(task_id, f"⚠️ named volume {vn} 同步失败: {e}（继续）", "stderr")

            # ---- 6) custom network 创建 ----
            for net_name, ndef in network_defs.items():
                try:
                    await self._ensure_local_network(task_id, net_name, ndef)
                except Exception as e:
                    self._log(task_id, f"⚠️ network {net_name} 创建失败: {e}（继续）", "stderr")

            # ---- 7) 重写 compose bind 路径 ----
            if prefix_active:
                try:
                    changed = self._rewrite_compose(compose_file, source_prefix, target_prefix)
                    if changed:
                        self._log(task_id, "  已重写 compose bind 路径")
                except Exception as e:
                    self._log(task_id, f"警告：重写 compose 失败: {e}", "stderr")

            # ---- 8) compose build / pull / up ----
            compose_ctx = ["docker", "compose", "-f", compose_file, "-p", name]
            if has_build:
                self._log(task_id, "  检测到 build 指令，先 compose build")
                try:
                    await self._run_cmd(task_id, compose_ctx + ["build"],
                                        label=f"docker compose build {name}")
                except Exception as e:
                    self._log(task_id, f"⚠️ compose build 失败: {e}（继续尝试 up）", "stderr")
            if pull_images and not has_build:
                # 有 build 的项目 pull 会失败（私有 image 名），跳过 pull 直接 up
                await self._run_cmd(task_id, compose_ctx + ["pull"],
                                    label=f"docker compose pull {name}",
                                    warn_only=True)
            if start_containers:
                await self._run_cmd(task_id, compose_ctx + ["up", "-d"],
                                    label=f"docker compose up {name}")

            self._log(task_id, f"✅ 项目 {name} 完成")

    # ==========================================================================
    # 推模式 — 源 NAS 部署
    # ==========================================================================
    async def _run_push(self, task_id, remote, local_root, remote_root, projects,
                        pull_images, start_containers,
                        source_prefix, target_prefix, prefix_active):
        if prefix_active:
            self._log(task_id,
                      f"路径前缀映射：{source_prefix} -> {target_prefix}（本机路径前缀 → 目标 NAS 路径前缀）")
        else:
            self._log(task_id,
                      "未启用路径前缀映射：要求源/目标 NAS 路径完全一致，否则外部 bind mount 会失效",
                      "stderr")

        tmp_workdir = tempfile.mkdtemp(prefix="migpush_")
        self._log(task_id, f"（临时 staging 目录：{tmp_workdir}）")
        try:
            for idx, proj in enumerate(projects, 1):
                name = proj["name"]
                local_src = proj.get("local_path")
                if not local_src:
                    self._log(task_id, f"跳过 {name}：缺少 local_path（请确认从本地扫描）", "stderr")
                    continue
                compose_name = proj.get("compose_file", "docker-compose.yml")
                remote_dst = os.path.join(remote_root, name)

                self._log(task_id, f"\n{'='*10} [{idx}/{len(projects)}] {name} {'='*10}")
                self._log(task_id, f"本机源: {local_src}")
                self._log(task_id, f"对端目标: {remote['username']}@{remote['host']}:{remote_dst}")

                # ---- 1) 拷贝到 staging（不动源目录）----
                staging_dir = os.path.join(tmp_workdir, name)
                if os.path.exists(staging_dir):
                    shutil.rmtree(staging_dir)
                shutil.copytree(local_src, staging_dir, symlinks=True)

                compose_file, compose_name = self._find_compose_file(staging_dir, compose_name)
                if not compose_file:
                    # 没 compose：只推送 staging 给用户留个拷贝，不做 create volume/network/up
                    self._log(task_id,
                              f"⚠️ 未找到 compose 文件，仅推送项目目录到远端（不启动容器）",
                              "stderr")
                    try:
                        await self._rsync_push(task_id, remote, staging_dir, remote_dst,
                                               label=f"rsync 推送 {name}")
                    except Exception as e:
                        self._log(task_id, f"⚠️ 推送 {name} 失败: {e}", "stderr")
                    else:
                        self._log(task_id, f"✅ 项目 {name} 目录已推送（无 compose，跳过启动）")
                    continue

                # ---- 2) 解析 compose ----
                try:
                    parsed = self._parse_compose(compose_file)
                except Exception as e:
                    self._log(task_id, f"警告：解析 compose 失败: {e}", "stderr")
                    parsed = {"binds": [], "named_volumes": [], "volume_defs": {},
                              "network_defs": {}, "has_build": False, "build_contexts": set()}

                binds = parsed["binds"]
                named_vols = parsed["named_volumes"]
                volume_defs = parsed["volume_defs"]
                network_defs = parsed["network_defs"]
                has_build = parsed["has_build"]
                build_contexts = parsed["build_contexts"]

                if build_contexts:
                    self._log(task_id, f"  发现 build 指令，contexts: {sorted(build_contexts)}（已随 staging 一起同步）")

                # ---- 3) 重写 staging compose（bind 路径）----
                if prefix_active:
                    try:
                        changed = self._rewrite_compose(compose_file, source_prefix, target_prefix)
                        if changed:
                            self._log(task_id, "  已重写 staging 内 compose 的 bind 路径")
                    except Exception as e:
                        self._log(task_id, f"警告：重写 compose 失败: {e}", "stderr")

                # ---- 4) 推送 staging（项目目录 + 已写好 compose）到对端 ----
                await self._rsync_push(task_id, remote, staging_dir, remote_dst,
                                       label=f"rsync 推送项目 {name}（含重写后 compose + build context）")

                # ---- 5) 推送外部 bind mount ----
                local_src_norm = os.path.normpath(local_src)
                for bp, svc in binds:
                    bp_norm = os.path.normpath(bp)
                    if bp_norm == local_src_norm or bp_norm.startswith(local_src_norm + "/"):
                        continue
                    # 远端目标路径
                    if prefix_active:
                        if bp.startswith(source_prefix):
                            remote_bind = target_prefix + bp[len(source_prefix):]
                        else:
                            self._log(task_id,
                                      f"跳过外部 bind mount {bp}：路径不以前缀 {source_prefix!r} 开头",
                                      "stderr")
                            continue
                    else:
                        remote_bind = bp
                    # 如果 remote_bind 指向 remote_dst 内部，跳过
                    rb_norm = os.path.normpath(remote_bind)
                    rd_norm = os.path.normpath(remote_dst)
                    if rb_norm == rd_norm or rb_norm.startswith(rd_norm + "/"):
                        continue
                    self._log(task_id, f"  同步外部 bind mount [{svc}]: {bp} -> {remote_bind}")
                    if not os.path.exists(bp):
                        self._log(task_id, f"⚠️ 本机 {bp} 不存在，跳过", "stderr")
                        continue
                    try:
                        await self._rsync_push(task_id, remote, bp, remote_bind,
                                               label=f"rsync bind {bp}")
                    except Exception as e:
                        self._log(task_id, f"⚠️ 推送 {bp} 失败: {e}（继续）", "stderr")

                # ---- 6) named volume 推送 ----
                vol_names = set()
                for vn, svc in named_vols:
                    vol_names.add(vn)
                for vn in vol_names:
                    vdef = volume_defs.get(vn, {"external": False})
                    if vdef.get("external"):
                        self._log(task_id, f"  volume {vn} 声明为 external，跳过")
                        continue
                    self._log(task_id, f"  同步 named volume: {vn}")
                    try:
                        # 本机 mountpoint
                        local_mp = await self._get_local_volume_mountpoint(task_id, vn)
                        if not local_mp or not os.path.exists(local_mp):
                            # 本机 volume 不存在（可能是空 volume 或用户未初始化）
                            self._log(task_id,
                                      f"    本机 volume {vn} 不存在或为空，先在远端 create 一个空 volume")
                            await self._ensure_remote_volume(task_id, remote, vn, vdef)
                            continue
                        # 远端 create 同名 volume
                        remote_mp = await self._ensure_remote_volume(task_id, remote, vn, vdef)
                        if not remote_mp:
                            self._log(task_id, f"⚠️ 远端 create volume {vn} 失败，跳过", "stderr")
                            continue
                        # rsync 本机 _data 到远端 _data
                        await self._rsync_push(task_id, remote, local_mp, remote_mp,
                                               label=f"rsync volume {vn} 数据")
                    except Exception as e:
                        self._log(task_id, f"⚠️ named volume {vn} 推送失败: {e}（继续）", "stderr")

                # ---- 7) 远端 custom network 创建 ----
                for net_name, ndef in network_defs.items():
                    try:
                        await self._ensure_remote_network(task_id, remote, net_name, ndef)
                    except Exception as e:
                        self._log(task_id, f"⚠️ network {net_name} 创建失败: {e}（继续）", "stderr")

                # ---- 8) 远端 compose build / pull / up ----
                remote_compose_path = os.path.join(remote_dst, compose_name)
                compose_ctx = ["docker", "compose", "-f", remote_compose_path, "-p", name]
                if has_build:
                    self._log(task_id, "  远端检测到 build 指令，先 compose build")
                    try:
                        await self._run_remote_cmd(task_id, remote, compose_ctx + ["build"],
                                                   label=f"远端 compose build {name}")
                    except Exception as e:
                        self._log(task_id, f"⚠️ 远端 compose build 失败: {e}（继续尝试 up）", "stderr")
                if pull_images and not has_build:
                    await self._run_remote_cmd(task_id, remote, compose_ctx + ["pull"],
                                               label=f"远端 compose pull {name}",
                                               warn_only=True)
                if start_containers:
                    await self._run_remote_cmd(task_id, remote, compose_ctx + ["up", "-d"],
                                               label=f"远端 compose up {name}")

                self._log(task_id, f"✅ 项目 {name} 完成")
        finally:
            shutil.rmtree(tmp_workdir, ignore_errors=True)

    # ==========================================================================
    # 本地映射模式 — 源 NAS 硬盘已挂载到本机（零数据传输）
    # ==========================================================================
    async def _run_local(self, task_id, local_root, projects,
                        pull_images, start_containers,
                        source_prefix, target_prefix, prefix_active,
                        source_docker_data=""):
        """源 NAS 存储已物理挂载到本机，数据直接可访问，无需 SSH/rsync 传输。

        source_prefix      = 源 NAS 上的原始路径前缀（如 /vol1/1000）
        target_prefix      = 在本机上的挂载路径前缀（如 /mnt/oldnas/vol1/1000 或 /vol1/1000）
        source_docker_data = 源 NAS 的 docker data-root 在本机上的路径
                             （如 /mnt/oldnas/var/lib/docker，用于 named volume 数据复制）
        """
        # ---- 0) 权限/可达性诊断 ----
        try:
            my_uid, my_gid = os.getuid(), os.getgid()
            self._log(task_id,
                      f"ℹ️ 当前进程 uid={my_uid} gid={my_gid}（容器应以 root 运行读取挂载点）")
        except Exception:
            pass
        checked_dirs = set()
        for proj in projects:
            p = proj.get("local_path") or proj.get("remote_path")
            if p:
                checked_dirs.add(p)
        if source_docker_data:
            checked_dirs.add(source_docker_data)
        for d in sorted(checked_dirs):
            if not os.path.exists(d):
                self._log(task_id,
                          f"❌ 路径不存在：{d}（挂载点是否透传到了容器？见容器日志启动诊断章节）",
                          "stderr")
                continue
            try:
                st = os.stat(d)
                mode_oct = oct(st.st_mode & 0o777)
                self._log(task_id,
                          f"ℹ️  {d}: uid={st.st_uid} gid={st.st_gid} mode={mode_oct} "
                          f"r_ok={os.access(d, os.R_OK)} x_ok={os.access(d, os.X_OK)}")
                if not os.access(d, os.R_OK) or not os.access(d, os.X_OK):
                    self._log(task_id,
                              "⚠️ 目录缺 r 或 x 位 → 读 compose 或递归复制 named volume 将失败，"
                              "请在主机侧 `chmod o+rX` 或以 root 挂载，"
                              "NFS 请在 /etc/exports 加 no_root_squash",
                              "stderr")
            except PermissionError as e:
                self._log(task_id, f"❌ stat({d}) 权限失败: {e}", "stderr")
            except OSError as e:
                self._log(task_id, f"❌ stat({d}) 失败: {e}", "stderr")

        if prefix_active:
            self._log(task_id, f"路径前缀映射：{source_prefix} -> {target_prefix}")
        else:
            self._log(task_id,
                      "路径前缀未映射：compose 中 bind 路径将原样使用（要求挂载路径与源 NAS 一致）")
        if source_docker_data:
            self._log(task_id,
                      f"源 docker data-root：{source_docker_data}（named volume 数据将从这里复制到本机 docker volumes）")
        else:
            self._log(task_id,
                      "⚠️ 未指定源 docker data-root，named volume 创建为空（如需复制数据请填源 NAS 的 "
                      "/var/lib/docker 在本机挂载点上的路径）",
                      "stderr")

        tmp_workdir = tempfile.mkdtemp(prefix="miglocal_")
        self._log(task_id, f"（临时 staging 目录：{tmp_workdir}）")
        try:
            for idx, proj in enumerate(projects, 1):
                name = proj["name"]
                source_path = proj.get("local_path") or proj.get("remote_path") or ""
                if not source_path or not os.path.isdir(source_path):
                    self._log(task_id, f"跳过 {name}：源路径不存在 {source_path}", "stderr")
                    continue
                compose_name = proj.get("compose_file", "docker-compose.yml")

                self._log(task_id, f"\n{'='*10} [{idx}/{len(projects)}] {name} {'='*10}")
                self._log(task_id, f"源路径（挂载点）：{source_path}")

                # ---- 1) 找 compose ----
                compose_src, compose_name = self._find_compose_file(source_path, compose_name)
                if not compose_src:
                    self._log(task_id, f"⚠️ 未找到 compose 文件，跳过 {name}", "stderr")
                    continue

                # ---- 2) staging：只拷贝 compose + .env（不拷贝数据，数据在挂载点直接可用）----
                staging_dir = os.path.join(tmp_workdir, name)
                os.makedirs(staging_dir, exist_ok=True)
                staging_compose = os.path.join(staging_dir, compose_name)
                shutil.copy2(compose_src, staging_compose)
                env_src = os.path.join(source_path, ".env")
                if os.path.exists(env_src):
                    shutil.copy2(env_src, os.path.join(staging_dir, ".env"))
                self._log(task_id, "  已拷贝 compose + .env 到 staging（不拷贝数据，数据在挂载点直接可用）")

                # ---- 3) 解析 compose ----
                try:
                    parsed = self._parse_compose(staging_compose)
                except Exception as e:
                    self._log(task_id, f"警告：解析 compose 失败: {e}", "stderr")
                    parsed = {"binds": [], "named_volumes": [], "volume_defs": {},
                              "network_defs": {}, "has_build": False, "build_contexts": set()}

                binds = parsed["binds"]
                named_vols = parsed["named_volumes"]
                volume_defs = parsed["volume_defs"]
                network_defs = parsed["network_defs"]
                has_build = parsed["has_build"]
                build_contexts = parsed["build_contexts"]

                if build_contexts:
                    self._log(task_id, f"  发现 build 指令，contexts: {sorted(build_contexts)}")

                # ---- 4) 重写 staging compose ----
                # a. bind 路径前缀映射
                if prefix_active:
                    try:
                        changed = self._rewrite_compose(staging_compose, source_prefix, target_prefix)
                        if changed:
                            self._log(task_id, "  已重写 bind 路径前缀")
                    except Exception as e:
                        self._log(task_id, f"警告：重写 compose 失败: {e}", "stderr")
                # b. build context 改为绝对路径（指向挂载点上的项目目录）
                try:
                    self._rewrite_build_context(staging_compose, source_path)
                except Exception as e:
                    self._log(task_id, f"警告：重写 build context 失败: {e}", "stderr")

                # ---- 5) named volumes ----
                vol_names = set(vn for vn, _ in named_vols)
                for vn in vol_names:
                    vdef = volume_defs.get(vn, {"external": False})
                    if vdef.get("external"):
                        self._log(task_id, f"  volume {vn} 声明为 external，跳过")
                        continue
                    self._log(task_id, f"  处理 named volume: {vn}")
                    try:
                        local_mp = await self._ensure_local_volume(task_id, vn, vdef)
                        if not local_mp:
                            self._log(task_id, f"⚠️ 本机 create volume {vn} 失败", "stderr")
                            continue
                        # 尝试从源 NAS docker data-root 复制数据
                        if source_docker_data:
                            src_vol_data = os.path.join(source_docker_data, "volumes", vn, "_data")
                            if os.path.isdir(src_vol_data):
                                self._log(task_id, f"    从 {src_vol_data} 复制 volume 数据到 {local_mp}")
                                await self._rsync_local(task_id, src_vol_data, local_mp,
                                                       label=f"rsync volume {vn} 数据（本地复制）")
                            else:
                                self._log(task_id,
                                          f"    源 docker data-root 中未找到 volume {vn} 的数据（{src_vol_data} 不存在），创建空 volume",
                                          "stderr")
                        else:
                            self._log(task_id,
                                      f"    未指定源 docker data-root 路径，volume {vn} 创建为空"
                                      f"（如需复制数据请在高级选项中填写源 docker data-root 路径）",
                                      "stderr")
                    except Exception as e:
                        self._log(task_id, f"⚠️ named volume {vn} 处理失败: {e}（继续）", "stderr")

                # ---- 6) custom networks ----
                for net_name, ndef in network_defs.items():
                    try:
                        await self._ensure_local_network(task_id, net_name, ndef)
                    except Exception as e:
                        self._log(task_id, f"⚠️ network {net_name} 创建失败: {e}（继续）", "stderr")

                # ---- 7) compose build / pull / up ----
                compose_ctx = ["docker", "compose", "-f", staging_compose, "-p", name]
                if has_build:
                    self._log(task_id, "  检测到 build 指令，先 compose build")
                    try:
                        await self._run_cmd(task_id, compose_ctx + ["build"],
                                            label=f"docker compose build {name}")
                    except Exception as e:
                        self._log(task_id, f"⚠️ compose build 失败: {e}（继续尝试 up）", "stderr")
                if pull_images and not has_build:
                    await self._run_cmd(task_id, compose_ctx + ["pull"],
                                        label=f"docker compose pull {name}",
                                        warn_only=True)
                if start_containers:
                    await self._run_cmd(task_id, compose_ctx + ["up", "-d"],
                                        label=f"docker compose up {name}")

                self._log(task_id, f"✅ 项目 {name} 完成")
        finally:
            shutil.rmtree(tmp_workdir, ignore_errors=True)
