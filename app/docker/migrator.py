"""迁移逻辑（通用版：pull + push 两种模式）。

拉模式 (pull)：rsync 从远端拉项目 -> 同步外部 bind mount -> 重写 compose 路径（如启用映射）
                -> 本机 docker compose pull / up。
推模式 (push)：本机 compose 先解析 bind -> 必要时重写 compose（拷贝到临时 dir 再推）
                -> rsync 项目目录到远端 -> rsync 外部 bind mount 到远端
                -> 远端（目标 NAS）执行 docker compose pull / up。
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

    # ---------- Docker 常见错误 → 中文修复建议 ----------
    # 每条规则: (关键词列表全部命中 or 任意1命中, 提示语)
    DOCKER_ERROR_HINTS = [
        # 网络超时 / 出网不可达
        (["i/o timeout"],
         "💡 修复建议：镜像仓库 443 出网超时（i/o timeout）。请检查：① 该 NAS 到镜像仓库的出网是否畅通；"
         "② 如在国内可在 Docker daemon.json 配置 registry-mirrors（国内镜像加速器）；"
         "③ 或到仓库链路走 HTTP/HTTPS 代理（在 Docker 启动环境变量里加 HTTP_PROXY/HTTPS_PROXY）。"),
        (["context deadline exceeded"],
         "💡 修复建议：Docker 拉取触发 context deadline exceeded。典型原因为对端 registry 网络太慢或不可达。"
         "建议：① 在对应 NAS 上配 registry-mirrors 或 HTTP 代理；"
         "② 临时改用『迁移后不拉镜像』，手动 docker load 离线 tarball 再 up。"),
        (["TLS handshake timeout"],
         "💡 修复建议：registry TLS 握手超时 = HTTPS 链路慢/被墙。通常和上两条一样，配镜像加速器/代理即可；"
         "如为自建 registry 请确认证书 CN 匹配域名且未过期。"),
        # DNS
        (["no such host"],
         "💡 修复建议：DNS 解析失败（no such host）。检查该 NAS 的 /etc/resolv.conf 是否能解析镜像仓库域名。"
         "常见：私有仓内网 DNS 未配置、或对端 NAS 使用了只能在本局域网解析的域名。"),
        (["lookup .* on .*: no such host"],
         "💡 修复建议：DNS 解析失败（no such host）。检查该 NAS 的 /etc/resolv.conf 是否能解析镜像仓库域名。"
         "常见：私有仓内网 DNS 未配置、或对端 NAS 使用了只能在本局域网解析的域名。"),
        # 鉴权 / 私有仓
        (["pull access denied"],
         "💡 修复建议：pull access denied = 对该镜像无权访问。通常原因：① 私有镜像仓库未登录（需 docker login <registry>）；"
         "② 镜像名/namespace 拼写错误；③ 公网镜像因限流被拒，尝试稍后或配置 registry-mirrors。"),
        (["repository does not exist"],
         "💡 修复建议：repository does not exist。请核对 compose 中 image 字段（拼写、tag、registry 前缀）是否存在；"
         "如为私有仓请先 docker login。"),
        (["unauthorized", "authentication required"],
         "💡 修复建议：镜像仓库 401 Unauthorized/鉴权失败。请在对应 NAS 上先执行 docker login <你的镜像仓域名>。"),
        (["denied: requested access to the resource is denied"],
         "💡 修复建议：镜像仓 denied = 当前登录账号无拉取权限。请换成有权限的 docker login 账号，或联系镜像仓管理员授权。"),
        # manifest / tag
        (["manifest unknown"],
         "💡 修复建议：manifest unknown = tag 不存在（可能 compose 里写了错的 tag、或 latest 被清）。"
         "请核对源 NAS 上 docker images 该镜像的实际 tag，修改 compose 后重试。"),
        (["manifest for .* not found"],
         "💡 修复建议：镜像 tag 未找到。核对 compose 中 image:tag 与源 NAS docker images 输出是否一致；"
         "如果源是本地 build 出来的且没 push，拉镜像选项无意义，请关掉『拉取镜像』后手动 docker save/load。"),
        # 连接被重置 / 断流
        (["connection reset by peer", "EOF"],
         "💡 修复建议：registry 连接被重置 / EOF，多为出口网络不稳或镜像仓限流。"
         "建议：① 配置 registry-mirrors；② 改用离线 docker save/load；③ 在网络闲时重试。"),
        # Daemon / CLI
        (["Cannot connect to the Docker daemon"],
         "💡 修复建议：无法连接 Docker daemon。请检查：① 该 NAS 上是否已安装并启动 Docker 应用；"
         "② /var/run/docker.sock 是否可被 dockermigrator 用户访问（一般由飞牛 docker-project 资源自动授权）。"),
        (["docker: command not found", "\"docker\": executable file not found"],
         "💡 修复建议：docker 命令不存在。"
         "如错误发生在『远端』（推模式目标 NAS）：请先在目标 NAS 安装 Docker 应用，并确保 docker compose v2 插件可用；"
         "如错误发生在『本机』：请确认 dockermigrator 容器内 /app/docker/bin/docker 已正确挂载到 PATH。"),
        (["compose is not a docker command"],
         "💡 修复建议：docker compose 子命令不存在 = 缺 compose v2 插件。"
         "如在推模式目标 NAS 上报错：请升级 Docker 版本或手动安装 docker-compose 插件（docker-compose-plugin 包）。"),
        # 端口绑定冲突
        (["port is already allocated"],
         "💡 修复建议：宿主机端口被占用。检查目标 NAS 上 compose 映射的 host 端口是否已被其他容器占用；"
         "用 docker ps 查看后停掉冲突容器，或改 compose 里的 host 端口再重试。"),
        # 磁盘
        (["no space left on device"],
         "💡 修复建议：目标 NAS 磁盘空间不足。清理无用镜像(docker system prune -a)、日志或迁移到大容量卷再重试。"),
    ]

    @classmethod
    def _diagnose_docker_error(cls, all_lines):
        """扫描最近的输出行（stdout+stderr），命中则按顺序返回一条最贴切的修复建议。
        规则：一条规则的所有 keyword（正则）都命中 blob 则触发；数组顺序即优先级。
        """
        if not all_lines:
            return None
        import re
        blob = "\n".join(all_lines).lower()
        for kws, hint in cls.DOCKER_ERROR_HINTS:
            # 本规则里多个关键词之间是 AND 关系；每个关键词是正则（大小写不敏感，已 lower）
            all_hit = True
            for kw in kws:
                if not re.search(kw.lower(), blob):
                    all_hit = False
                    break
            if all_hit:
                return hint
        return None

    # ---------- 任务状态 ----------
    def create_task(self, task_id):
        self.tasks[task_id] = {"status": "pending", "log": []}

    def _log(self, task_id, line, stream="stdout"):
        task = self.tasks.get(task_id)
        if task is None:
            return
        task["log"].append({"line": line, "stream": stream})

    # ---------- 执行本机子进程 ----------
    async def _run_cmd(self, task_id, cmd, env=None, label=None):
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
            # 仅在 docker 相关命令失败时给出针对性修复建议
            if cmd and (cmd[0].endswith("docker") or "docker" in cmd[0]):
                hint = self._diagnose_docker_error(all_lines)
                if hint:
                    self._log(task_id, hint, "stderr")
                else:
                    self._log(
                        task_id,
                        "💡 通用排查：① 对应 NAS 上 docker info 是否正常；"
                        "② 镜像名/tag 是否存在(docker images)；③ 检查磁盘空间(df -h)和网络。",
                        "stderr",
                    )
            raise RuntimeError(f"命令失败 (exit={rc})")

    # ---------- 执行远端命令（push 模式：docker compose pull/up） ----------
    async def _run_remote_cmd(self, task_id, remote_cfg, cmd, label=None):
        """用 SSHClient.exec_stream 远程执行命令，输出落到任务日志。"""
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
            # 远端 docker 命令失败 → 同样输出诊断
            is_docker = any("docker" in str(c).lower() for c in cmd)
            if is_docker:
                hint = self._diagnose_docker_error(lines)
                if hint:
                    self._log(task_id, hint + "（以上提示针对目标 NAS 远端环境）", "stderr")
                else:
                    self._log(
                        task_id,
                        "💡 远端通用排查：① 目标 NAS 上 docker info 正常吗？② compose 中镜像/tag 是否存在？"
                        "③ 目标 NAS 磁盘和出网是否通畅？",
                        "stderr",
                    )
            raise RuntimeError(f"远端命令失败 {tail}")

    # ---------- compose 解析 ----------
    @staticmethod
    def _extract_bind_mounts(compose_path):
        with open(compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        paths, seen = [], set()
        for svc in (data.get("services") or {}).values():
            vols = svc.get("volumes")
            if not isinstance(vols, list):
                continue
            for v in vols:
                src = None
                if isinstance(v, str):
                    parts = v.split(":")
                    if len(parts) >= 2 and parts[0].startswith("/"):
                        src = parts[0]
                elif isinstance(v, dict):
                    s = v.get("source")
                    if isinstance(s, str) and s.startswith("/"):
                        src = s
                if src and src not in seen:
                    seen.add(src)
                    paths.append(src)
        return paths

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

    # ---------- rsync helper ----------
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
        cmd = ["rsync", "-av", "--numeric-ids", "-e", rsh,
               f"{remote_cfg['username']}@{remote_cfg['host']}:{src}", dst]
        await self._run_cmd(task_id, cmd, env=env, label=label)

    async def _rsync_push(self, task_id, remote_cfg, local_src, remote_dst, label=None):
        """从本机 local_src/ rsync 推到远端 remote_dst/。"""
        # 先在远端 mkdir -p（按密码/密钥 拼对应的 ssh 命令）
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
            self._log(
                task_id,
                f"警告：远端 mkdir {remote_dst} 失败 rc={rc} {detail}（后续 rsync 仍尝试继续）",
                "stderr",
            )

        src = local_src if local_src.endswith("/") else local_src + "/"
        dst = remote_dst if remote_dst.endswith("/") else remote_dst + "/"
        rsh, env = self._rsync_env(remote_cfg)
        cmd = ["rsync", "-av", "--numeric-ids", "-e", rsh,
               src, f"{remote_cfg['username']}@{remote_cfg['host']}:{dst}"]
        await self._run_cmd(task_id, cmd, env=env, label=label)

    # ---------- 查找 compose 文件 ----------
    @staticmethod
    def _find_compose_file(proj_dir, preferred_name=None):
        candidates = [preferred_name] if preferred_name else []
        candidates += ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]
        for c in candidates:
            if c is None:
                continue
            p = os.path.join(proj_dir, c)
            if os.path.exists(p):
                return p, os.path.basename(p)
        return None, None

    # -------------------------------------------------------------------------
    # 统一入口
    # -------------------------------------------------------------------------
    async def run(self, task_id, direction, remote, local_docker_root, remote_docker_root,
                  projects, pull_images=True, start_containers=True,
                  source_prefix="", target_prefix=""):
        task = self.tasks[task_id]
        task["status"] = "running"

        prefix_active = bool(source_prefix) and source_prefix != target_prefix
        try:
            if direction == "pull":
                self._log(task_id, f"[拉模式] 开始迁移 {len(projects)} 个项目（从源 NAS → 本机）")
                await self._run_pull(task_id, remote, local_docker_root, projects,
                                     pull_images, start_containers,
                                     source_prefix, target_prefix, prefix_active)
            else:
                self._log(task_id, f"[推模式] 开始迁移 {len(projects)} 个项目（从本机 → 目标 NAS）")
                await self._run_push(task_id, remote, local_docker_root, remote_docker_root, projects,
                                     pull_images, start_containers,
                                     source_prefix, target_prefix, prefix_active)

            task["status"] = "done"
            self._log(task_id, "\n===== 全部迁移完成 =====")
        except Exception as e:
            task["status"] = "error"
            self._log(task_id, f"错误: {e}", "stderr")
            raise

    # -------------------------------------------------------------------------
    # 拉模式（原有逻辑，略重构）
    # -------------------------------------------------------------------------
    async def _run_pull(self, task_id, remote, local_root, projects,
                        pull_images, start_containers,
                        source_prefix, target_prefix, prefix_active):
        if prefix_active:
            self._log(task_id,
                      f"路径前缀映射：{source_prefix} -> {target_prefix}")
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

            self._log(task_id, f"\n========== [{idx}/{len(projects)}] {name} ==========")
            self._log(task_id, f"源: {remote_user}@{remote_host}:{remote_path}")
            self._log(task_id, f"目标: {local_dst}")
            os.makedirs(local_root, exist_ok=True)

            # 1) 拉取项目目录
            await self._rsync_pull(task_id, remote, remote_path, local_dst,
                                   label=f"rsync 拉取项目 {name}")

            # 2) 找 compose
            compose_file, compose_name = self._find_compose_file(local_dst, compose_name)
            if not compose_file:
                self._log(task_id,
                          f"警告：未找到 compose 文件，跳过 {name} 的镜像/容器操作", "stderr")
                continue

            # 3) bind 同步
            try:
                bind_paths = self._extract_bind_mounts(compose_file)
            except Exception as e:
                self._log(task_id, f"警告：解析 compose 失败: {e}", "stderr")
                bind_paths = []

            rp_norm = os.path.normpath(remote_path)
            for bp in bind_paths:
                bp_norm = os.path.normpath(bp)
                if bp_norm == rp_norm or bp_norm.startswith(rp_norm + "/"):
                    continue
                if prefix_active:
                    if bp.startswith(source_prefix):
                        local_target = target_prefix + bp[len(source_prefix):]
                    else:
                        self._log(
                            task_id,
                            f"跳过外部 bind mount {bp}：已启用前缀映射但路径不以前缀"
                            f" {source_prefix!r} 开头，不明确目标落点请手动同步",
                            "stderr",
                        )
                        continue
                else:
                    local_target = bp
                self._log(task_id, f"同步外部 bind mount: {bp} -> {local_target}")
                try:
                    await self._rsync_pull(task_id, remote, bp, local_target,
                                           label=f"rsync bind {bp}")
                except Exception as e:
                    self._log(task_id, f"警告：同步 {bp} 失败: {e}（继续）", "stderr")

            # 4) 重写 compose 路径
            if prefix_active:
                try:
                    changed = self._rewrite_compose(compose_file, source_prefix, target_prefix)
                    if changed:
                        self._log(task_id, "已重写 compose bind 路径")
                except Exception as e:
                    self._log(task_id, f"警告：重写 compose 失败: {e}", "stderr")

            # 5) 本机 compose pull/up
            compose_ctx = ["docker", "compose", "-f", compose_file, "-p", name]
            if pull_images:
                await self._run_cmd(task_id, compose_ctx + ["pull"],
                                   label=f"docker compose pull {name}")
            if start_containers:
                await self._run_cmd(task_id, compose_ctx + ["up", "-d"],
                                   label=f"docker compose up {name}")

            self._log(task_id, f"项目 {name} 完成")

    # -------------------------------------------------------------------------
    # 推模式（新）
    # -------------------------------------------------------------------------
    async def _run_push(self, task_id, remote, local_root, remote_root, projects,
                        pull_images, start_containers,
                        source_prefix, target_prefix, prefix_active):
        if prefix_active:
            self._log(task_id,
                      f"路径前缀映射：{source_prefix} -> {target_prefix}（本机路径前缀 -> 目标 NAS 路径前缀）")
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

                self._log(task_id, f"\n========== [{idx}/{len(projects)}] {name} ==========")
                self._log(task_id, f"本机源: {local_src}")
                self._log(task_id, f"对端目标: {remote['username']}@{remote['host']}:{remote_dst}")

                # 1) 如果启用了路径前缀重写，把 compose 先拷到 staging 再重写（不动源目录）
                staging_dir = os.path.join(tmp_workdir, name)
                # 拷贝整个项目目录到 staging
                if os.path.exists(staging_dir):
                    shutil.rmtree(staging_dir)
                shutil.copytree(local_src, staging_dir, symlinks=True)

                compose_file, compose_name = self._find_compose_file(staging_dir, compose_name)
                if not compose_file:
                    self._log(task_id,
                              f"警告：未找到 compose 文件，跳过 {name} 的 compose 解析与远端 up",
                              "stderr")
                    # 但仍然只推送 staging 的文件（给用户留一个拷贝）
                    continue

                # 2) 提取 bind（解析 staging 的 compose，源路径是本机原路径）
                try:
                    bind_paths = self._extract_bind_mounts(compose_file)
                except Exception as e:
                    self._log(task_id, f"警告：解析 compose 失败: {e}", "stderr")
                    bind_paths = []

                # 3) 前缀映射下重写 staging 的 compose（只有 bind source 前缀会变）
                if prefix_active:
                    try:
                        changed = self._rewrite_compose(compose_file, source_prefix, target_prefix)
                        if changed:
                            self._log(task_id, "已重写 staging 内 compose 的 bind 路径")
                    except Exception as e:
                        self._log(task_id, f"警告：重写 compose 失败: {e}", "stderr")

                # 4) 推送 staging（项目目录 + 已写好的 compose）到对端
                await self._rsync_push(task_id, remote, staging_dir, remote_dst,
                                       label=f"rsync 推送项目 {name}（含重写后 compose）")

                # 5) 推送外部 bind mount（排除项目目录本身）
                local_src_norm = os.path.normpath(local_src)
                for bp in bind_paths:
                    bp_norm = os.path.normpath(bp)
                    if bp_norm == local_src_norm or bp_norm.startswith(local_src_norm + "/"):
                        continue
                    # 远端目标路径（按前缀映射）
                    if prefix_active:
                        if bp.startswith(source_prefix):
                            remote_bind = target_prefix + bp[len(source_prefix):]
                        else:
                            self._log(
                                task_id,
                                f"跳过外部 bind mount {bp}：已启用前缀映射但路径不以前缀"
                                f" {source_prefix!r} 开头，不明确对端落点请手动同步",
                                "stderr",
                            )
                            continue
                    else:
                        remote_bind = bp
                    self._log(task_id, f"同步外部 bind mount: {bp} -> {remote_bind}")
                    if not os.path.exists(bp):
                        self._log(task_id, f"警告：本机 {bp} 不存在，跳过", "stderr")
                        continue
                    try:
                        await self._rsync_push(task_id, remote, bp, remote_bind,
                                               label=f"rsync bind {bp}")
                    except Exception as e:
                        self._log(task_id, f"警告：推送 {bp} 失败: {e}（继续）", "stderr")

                # 6) 远端 docker compose pull / up
                remote_compose_path = os.path.join(remote_dst, compose_name)
                compose_ctx = [
                    "docker", "compose",
                    "-f", remote_compose_path,
                    "-p", name,
                ]
                if pull_images:
                    await self._run_remote_cmd(task_id, remote, compose_ctx + ["pull"],
                                               label=f"docker compose pull {name}")
                if start_containers:
                    await self._run_remote_cmd(task_id, remote, compose_ctx + ["up", "-d"],
                                               label=f"docker compose up {name}")

                self._log(task_id, f"项目 {name} 完成")
        finally:
            shutil.rmtree(tmp_workdir, ignore_errors=True)
