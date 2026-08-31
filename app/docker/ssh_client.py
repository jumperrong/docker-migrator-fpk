"""SSH 客户端：通用版（拉模式 + 推模式）。

拉模式：连接源 NAS，扫描远程 compose 并由 migrator 拉取。
推模式：连接目标 NAS，推送本机 compose 后由 migrator 远程 compose up。
"""
import os
import shlex
import subprocess
import paramiko


class SSHClient:
    def __init__(self, host, port=22, username="root", password=None, key_path=None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_path = key_path

    # ---------- 连接 ----------
    def _connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(
            hostname=self.host,
            port=self.port,
            username=self.username,
            timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        if self.key_path:
            kwargs["key_filename"] = self.key_path
        elif self.password:
            kwargs["password"] = self.password
        else:
            # 既没有密码也没有密钥，回退到默认 agent/已知主机
            kwargs["allow_agent"] = True
            kwargs["look_for_keys"] = True
        client.connect(**kwargs)
        return client

    def test_connection(self):
        """尝试连接并执行 uname，返回主机信息或失败。"""
        try:
            client = self._connect()
            try:
                _, stdout, _ = client.exec_command("uname -a; echo '---'; hostname")
                out = stdout.read().decode(errors="replace").strip()
                return {"ok": True, "info": out}
            finally:
                client.close()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 扫描 compose ----------
    def list_projects(self, docker_root):
        """扫描远程 docker_root 下（最多 2 层）的 compose 文件，返回项目列表。
        项目列表里每条含 name / remote_path / compose_file / size。
        """
        client = self._connect()
        try:
            root = shlex.quote(docker_root)
            cmd = (
                f"find {root} -maxdepth 2 "
                r"\( -name 'docker-compose.yml' -o -name 'docker-compose.yaml' "
                r"-o -name 'compose.yml' -o -name 'compose.yaml' \) 2>/dev/null"
            )
            _, stdout, _ = client.exec_command(cmd)
            files = [line.strip() for line in stdout if line.strip()]

            projects = []
            for f in files:
                proj_dir = os.path.dirname(f)
                name = os.path.basename(proj_dir)
                if not name:
                    continue
                _, so, _ = client.exec_command(
                    f"du -sh {shlex.quote(proj_dir)} 2>/dev/null | cut -f1"
                )
                size = so.read().decode(errors="replace").strip() or "未知"
                projects.append(
                    {
                        "name": name,
                        "remote_path": proj_dir,
                        "compose_file": os.path.basename(f),
                        "size": size,
                    }
                )
            # 去重（同名取第一个）
            seen, dedup = set(), []
            for p in projects:
                if p["name"] in seen:
                    continue
                seen.add(p["name"])
                dedup.append(p)
            return dedup
        finally:
            client.close()

    @staticmethod
    def list_local_projects(docker_root):
        """扫描本机 docker_root（push 模式：本应用装在源 NAS 上）。
        字段对齐 list_projects 但用 local_path。
        """
        try:
            root = shlex.quote(docker_root)
            find_cmd = (
                f"find {root} -maxdepth 2 "
                r"\( -name 'docker-compose.yml' -o -name 'docker-compose.yaml' "
                r"-o -name 'compose.yml' -o -name 'compose.yaml' \) 2>/dev/null"
            )
            out = subprocess.check_output(find_cmd, shell=True, text=True, stderr=subprocess.DEVNULL)
            files = [line.strip() for line in out.splitlines() if line.strip()]
        except Exception:
            files = []
        projects = []
        seen = set()
        for f in files:
            proj_dir = os.path.dirname(f)
            name = os.path.basename(proj_dir)
            if not name or name in seen:
                continue
            seen.add(name)
            try:
                sz = subprocess.check_output(
                    f"du -sh {shlex.quote(proj_dir)} 2>/dev/null | cut -f1",
                    shell=True, text=True, stderr=subprocess.DEVNULL,
                ).strip() or "未知"
            except Exception:
                sz = "未知"
            projects.append(
                {
                    "name": name,
                    "local_path": proj_dir,
                    "compose_file": os.path.basename(f),
                    "size": sz,
                }
            )
        return projects

    # ---------- 远程执行命令（逐行回调，用于推送模式：docker compose pull / up 输出回传） ----------
    def exec_stream(self, cmd, on_line):
        """远程执行 cmd（列表形式），stdout/stderr 逐行调用 on_line(line, stream='stdout'|'stderr')。
        返回 (ok, combined_tail_text) 元组。
        """
        shell_cmd = " ".join(shlex.quote(c) for c in cmd)
        client = self._connect()
        try:
            _, stdout, stderr = client.exec_command(shell_cmd, get_pty=False)

            import threading

            def _drain(stream, stream_name):
                for raw in stream:
                    text = raw.decode(errors="replace").rstrip("\r\n")
                    if text:
                        on_line(text, stream_name)

            t_out = threading.Thread(target=_drain, args=(stdout, "stdout"), daemon=True)
            t_err = threading.Thread(target=_drain, args=(stderr, "stderr"), daemon=True)
            t_out.start(); t_err.start()
            t_out.join(); t_err.join()

            rc = stdout.channel.recv_exit_status()
            return rc == 0, f"(rc={rc})"
        finally:
            client.close()
