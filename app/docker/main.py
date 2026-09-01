"""飞牛 Docker 迁移工具后端（通用版：拉模式 / 推模式 / 本地映射模式）。

- 拉模式（pull）：本应用部署在「目标 NAS」，SSH 连接源 NAS 扫描并拉取。
- 推模式（push）：本应用部署在「源 NAS」，扫描本机项目并推送到对端。
- 本地映射模式（local）：源 NAS 硬盘阵列已物理挂载到本机，直接扫描挂载点上的项目，
  无需 SSH / rsync 网络传输，仅拷贝 compose + .env 到 staging 后在本地 build/pull/up。

安全：Basic Auth 保护所有 /api/* 路由；路径遍历校验；SSH host/port 格式校验。
"""
import os
import json
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel

from ssh_client import SSHClient
from migrator import Migrator
from auth import (
    validate_abs_path, validate_host, validate_port, validate_proj_name,
    check_basic_auth, mask_secrets_in_text,
)

app = FastAPI(title="飞牛 Docker 迁移工具（通用版）")

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

migrator = Migrator()
# state: {'remote': {host/port/...}, 'mode': 'pull'|'push'|'local',
#         'scan_root': str,  # 「扫描出项目」的根目录（拉=远端，推=本机）
#         'remote_root': str # 「对端」docker root（推送时写 compose up 用）
state = {"remote": None, "mode": "pull", "scan_root": "", "remote_root": ""}


# ---------------- Basic Auth 中间件 ----------------
@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    """所有 /api/* 路由要求 Basic Auth；静态资源和首页放行。"""
    path = request.url.path
    if path.startswith("/api/"):
        if not check_basic_auth(request):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Docker Migrator"'},
                content='{"detail":"未认证，请使用 Basic Auth（默认 admin/admin）"}',
                media_type="application/json",
            )
    return await call_next(request)


# ---------------- 默认值（来自安装向导注入的 LOCAL_DOCKER_ROOT）----------------
@app.get("/api/defaults")
async def defaults():
    return {
        "local_docker_root": os.getenv("LOCAL_DOCKER_ROOT", "/vol1/1000/docker"),
    }


# ---------------- 模型 ----------------
class ConnectionConfig(BaseModel):
    host: str
    port: int = 22
    username: str = "root"
    password: str | None = None
    key_path: str | None = None
    # 拉模式 scan_local=False：扫描「远端」docker_root 字段（源 NAS）
    # 推模式 scan_local=True：扫描「本机」local_docker_root（源 NAS），再验证到远端可连
    scan_local: bool = False
    local_docker_root: str = "/vol1/1000/docker"
    remote_docker_root: str = "/vol1/1000/docker"
    # 兼容旧版单字段（传了它 = 传 scan_local=False 并把这个当远端 root）
    docker_root: str | None = None


class ProjectSpec(BaseModel):
    name: str
    remote_path: str | None = None   # pull 模式：源 NAS 上路径
    local_path: str | None = None    # push 模式：本机路径
    compose_file: str = "docker-compose.yml"
    size: str = ""


class MigrateRequest(BaseModel):
    direction: str = "pull"             # 'pull' | 'push' | 'local'
    local_docker_root: str = "/vol1/1000/docker"
    remote_docker_root: str = "/vol1/1000/docker"
    projects: list[ProjectSpec]
    pull_images: bool = True
    start_containers: bool = True
    # 路径前缀映射：源 NAS 与目标 NAS 卷号/用户号不同时，
    # 把 compose 里以 source_prefix 开头的 bind mount 路径改写到 target_prefix
    source_prefix: str = ""
    target_prefix: str = ""
    # 本地映射模式专用：源 NAS 的 docker data-root 在本机上的路径
    # （如 /mnt/oldnas/var/lib/docker，用于 named volume 数据复制）
    source_docker_data: str = ""


# ---------------- 路由 ----------------
@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.post("/api/connect")
async def connect(cfg: ConnectionConfig):
    # 输入校验（防 SSH 命令注入 + 路径遍历）
    cfg.host = validate_host(cfg.host)
    cfg.port = validate_port(cfg.port)
    cfg.username = (cfg.username or "root").strip()[:32]
    # username 只允许字母数字下划线横线点
    import re
    if not re.match(r"^[A-Za-z0-9_.\-]+$", cfg.username):
        raise HTTPException(status_code=400, detail=f"用户名格式不合法: {cfg.username!r}")
    if cfg.local_docker_root:
        cfg.local_docker_root = validate_abs_path(cfg.local_docker_root, "本机 Docker 根目录")
    if cfg.remote_docker_root:
        cfg.remote_docker_root = validate_abs_path(cfg.remote_docker_root, "对端 Docker 根目录")

    # 兼容旧单字段 docker_root
    remote_root = cfg.remote_docker_root
    if cfg.docker_root and not remote_root:
        remote_root = cfg.docker_root
    if cfg.docker_root and not cfg.local_docker_root:
        local_root = cfg.docker_root
    else:
        local_root = cfg.local_docker_root

    mode = "push" if cfg.scan_local else "pull"

    # 远端 SSH 验证（两种模式都要能连上对端）
    client = SSHClient(cfg.host, cfg.port, cfg.username, cfg.password, cfg.key_path)
    result = client.test_connection()
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])

    state["remote"] = {
        "host": cfg.host, "port": cfg.port, "username": cfg.username,
        "password": cfg.password, "key_path": cfg.key_path,
    }
    state["mode"] = mode
    if mode == "pull":
        state["scan_root"] = remote_root
        state["remote_root"] = remote_root
    else:
        state["scan_root"] = local_root
        state["remote_root"] = remote_root

    return {"ok": True, "info": result["info"], "mode": mode}


@app.get("/api/projects")
async def list_projects():
    if not state["remote"]:
        raise HTTPException(status_code=400, detail="请先连接对端 NAS")
    r = state["remote"]
    root = state["scan_root"]

    if state["mode"] == "pull":
        # 远程扫描源 NAS
        client = SSHClient(r["host"], r["port"], r["username"], r["password"], r.get("key_path"))
        try:
            projects = client.list_projects(root)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        # 扫描本机（源 NAS）
        projects = SSHClient.list_local_projects(root)

    return {"projects": projects, "docker_root": root, "mode": state["mode"]}


# ---------------- 本地映射模式：扫描挂载点（无需 SSH） ----------------
# 权限错误 → 中文修复建议（覆盖容器挂载/uid-gid/NFS/目录权限）
# 结构：[(关键词列表, 修复建议), ...]  — 一目了然，不再交替排列
_PERMISSION_HINTS = [
    (["容器内不可见挂载点", "no such file or directory", "not found", "不存在"],
     "💡 修复建议：路径在容器里不存在 → 该挂载路径未透传到 Docker 容器。"
     "在飞牛「停止应用」后编辑 docker-compose.yaml 的 volumes，"
     "或重新安装/配置挂载向导字段「源 NAS 挂载根路径」，然后重启应用。"
     "可在容器日志「启动诊断」章节查看已挂载清单。"),
    (["EACCES", "Permission denied", "permission denied", "权限不够"],
     "💡 修复建议：Permission denied（EACCES）常见原因：① 源 NAS 硬盘挂载在本机使用了 UID/GID 映射（NFS all_squash / root_squash）"
     "导致 root(容器内 uid=0) 无权读；② ext4/btrfs 挂载目录的 POSIX 权限不给 root 可读；"
     "建议：① 在飞牛主机侧执行 `ls -ld 路径` + `id`，对比容器日志启动诊断的 uid/gid；"
     "② NFS 改 /etc/exports 加 no_root_squash 或挂载时 anonuid=0; ③ U 盘/移动硬盘确保挂载时给用户/组可读权限。"),
    (["EROFS", "read-only", "Read-only", "只读"],
     "💡 修复建议：文件系统只读（EROFS）。检查 mount 是否带 ro；USB 硬盘写保护开关；NTFS 可能只读需要 ntfs-3g。"),
]


def _diagnose_permission_error(err_str: str):
    """从错误字符串匹配权限类提示，返回首条命中的修复建议。"""
    if not err_str:
        return None
    import re
    e = err_str.lower()
    for kws, hint in _PERMISSION_HINTS:
        for kw in kws:
            if re.search(kw.lower(), e):
                return hint
    return None


def _probe_local_path(root: str) -> str:
    """针对本地映射模式做权限探测，返回可读空字符串或拼接好的中文诊断信息。

    探测项：
      1) 路径存在？
      2) 目录可读 + 可遍历（r+x）？
      3) 能列出目录内容（find 会报错 Permission denied 吗）？
      4) 随机挑一个 compose 文件能读吗？
      5) uid/gid 与源目录不一致的风险提示
    """
    import subprocess
    import shlex
    msgs = []
    # 1) 路径存在
    if not os.path.exists(root):
        hint = _diagnose_permission_error("no such file or directory") or ""
        return f"❌ 路径不存在：{root}\n{hint}"
    if not os.path.isdir(root):
        return f"❌ 路径存在但不是目录：{root}"
    # 2) r+x
    if not os.access(root, os.R_OK):
        msgs.append("❌ 目录不可读（R_OK 失败），可能是 POSIX 权限或 ACL 限制")
    if not os.access(root, os.X_OK):
        msgs.append("❌ 目录不可遍历（X_OK 失败，无法 cd 进入子目录）")
    # 3) 能否列出第一层（用 os.listdir，失败会抛 PermissionError）
    try:
        items = os.listdir(root)
        if len(items) == 0:
            msgs.append("⚠️ 路径可读但是空目录，请确认挂载路径是否正确")
    except PermissionError as e:
        msgs.append(f"❌ 列出目录内容失败（PermissionError）: {e}")
    except OSError as e:
        msgs.append(f"❌ 列出目录内容失败: {e}")
    # 4) 试 find -maxdepth 4，看是否有 EACCES 子目录
    try:
        out = subprocess.check_output(
            f"find {shlex.quote(root)} -maxdepth 4 "
            r"\( -name 'docker-compose.yml' -o -name 'docker-compose.yaml' "
            r"-o -name 'compose.yml' -o -name 'compose.yaml' \) "
            "2>&1 | head -20",
            shell=True, text=True, stderr=subprocess.STDOUT,
        )
        if "Permission denied" in out or "权限不够" in out:
            msgs.append("⚠️ find 扫描过程中出现子目录 Permission denied，可能某些项目目录将被跳过")
    except subprocess.CalledProcessError as e:
        msgs.append(f"❌ find 扫描返回错误 exit={e.returncode}: {e.output[:400]}")
    # 5) 读取 uid/gid 样本，判断与容器内 root 是否一致
    try:
        st = os.stat(root)
        uid, gid = st.st_uid, st.st_gid
        import pwd, grp
        try:
            owner = pwd.getpwuid(uid).pw_name
        except Exception:
            owner = f"uid={uid}"
        try:
            group = grp.getgrgid(gid).gr_name
        except Exception:
            group = f"gid={gid}"
        mode_bits = oct(st.st_mode & 0o777)
        msgs.append(f"ℹ️ 目录属主: {owner}:{group} (uid={uid},gid={gid})，权限位 {mode_bits}，"
                    f"当前容器进程 uid={os.getuid()} gid={os.getgid()}")
        if os.getuid() != 0 and os.getuid() != uid:
            msgs.append("⚠️ 容器进程非 root 且与目录属主 uid 不同，可能无法读取——建议 compose.yaml 配置 user: '0:0'")
    except Exception:
        pass

    if msgs:
        return "\n".join(msgs)
    return ""


@app.get("/api/scan_local")
async def scan_local(root: str = ""):
    """本地映射模式：扫描源 NAS 硬盘挂载到本机的路径下的 compose 项目。

    扫描前先做权限探测；如有权限风险会给出中文修复建议。
    不需要 SSH 连接，直接用 list_local_projects 扫描本机路径。
    """
    root = (root or "").strip()
    root = validate_abs_path(root, "源 NAS 挂载路径")
    # 权限探测
    diag = _probe_local_path(root)
    if diag and any(l.startswith("❌") for l in diag.splitlines()):
        hint = _diagnose_permission_error(diag)
        msg = "容器内无法访问源 NAS 挂载点，请按以下说明修复：\n\n" + diag
        if hint:
            msg += "\n\n" + hint
        raise HTTPException(status_code=400, detail=msg)
    try:
        projects = SSHClient.list_local_projects(root)
    except Exception as e:
        err_str = str(e)
        hint = _diagnose_permission_error(err_str)
        msg = f"扫描失败：{err_str}"
        if diag:
            msg += "\n\n权限探测：\n" + diag
        if hint:
            msg += "\n\n" + hint
        raise HTTPException(status_code=500, detail=msg)
    # 记录到 state，便于 /api/migrate 校验（local 模式 remote 可为 None）
    state["mode"] = "local"
    state["scan_root"] = root
    state["remote"] = None
    result = {"projects": projects, "docker_root": root, "mode": "local"}
    if diag:
        # 有⚠️类提示但无❌，作为警告附加返回（UI 可在控制台显示）
        result["warnings"] = diag
    return result


@app.post("/api/migrate")
async def migrate(req: MigrateRequest):
    if req.direction not in ("pull", "push", "local"):
        raise HTTPException(status_code=400, detail="direction 必须是 pull / push / local")
    if not req.projects:
        raise HTTPException(status_code=400, detail="未选择任何项目")
    # local 模式不需要 SSH，其余模式需要已连接对端
    if req.direction != "local" and not state["remote"]:
        raise HTTPException(status_code=400, detail="请先连接对端 NAS")

    # 路径校验（防遍历/注入）
    req.local_docker_root = validate_abs_path(req.local_docker_root, "本机 Docker 根目录")
    if req.remote_docker_root:
        req.remote_docker_root = validate_abs_path(req.remote_docker_root, "对端 Docker 根目录")
    if req.source_prefix:
        req.source_prefix = validate_abs_path(req.source_prefix, "源 NAS 前缀")
    if req.target_prefix:
        req.target_prefix = validate_abs_path(req.target_prefix, "目标 NAS 前缀")
    if req.direction == "local" and req.source_docker_data:
        req.source_docker_data = validate_abs_path(req.source_docker_data, "源 docker data-root")
    # 项目名校验（防 compose project name 命令注入）
    for p in req.projects:
        validate_proj_name(p.name)

    import uuid
    task_id = uuid.uuid4().hex
    migrator.create_task(task_id, direction=req.direction, project_count=len(req.projects))
    remote = state["remote"]
    projects = [p.model_dump() for p in req.projects]

    t = asyncio.create_task(
        migrator.run(
            task_id,
            direction=req.direction,
            remote=remote,
            local_docker_root=req.local_docker_root,
            remote_docker_root=req.remote_docker_root,
            projects=projects,
            pull_images=req.pull_images,
            start_containers=req.start_containers,
            source_prefix=req.source_prefix,
            target_prefix=req.target_prefix,
            source_docker_data=req.source_docker_data,
        )
    )
    migrator._running_futures[task_id] = t  # 供 cancel_task 取消

    def _on_done(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc and task_id in migrator.tasks:
            migrator.tasks[task_id]["status"] = "error"
            migrator._log(task_id, f"任务异常退出: {exc}", "stderr")

    t.add_done_callback(_on_done)
    return {"task_id": task_id}


@app.get("/api/tasks/{task_id}/stream")
async def stream(task_id: str):
    if task_id not in migrator.tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_gen():
        last = 0
        idle = 0.0
        while True:
            task = migrator.tasks[task_id]
            log = task["log"]
            if len(log) > last:
                for entry in log[last:]:
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                last = len(log)
                idle = 0.0
            if task["status"] in ("done", "error"):
                yield f"data: {json.dumps({'status': task['status']}, ensure_ascii=False)}\n\n"
                break
            # 心跳：连续 15s 无新日志时发 SSE 注释行，防止反代/浏览器超时断连
            idle += 0.4
            if idle >= 15.0:
                yield ": ping\n\n"
                idle = 0.0
            await asyncio.sleep(0.4)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    if task_id not in migrator.tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    t = migrator.tasks[task_id]
    return {
        "status": t["status"],
        "log_len": len(t["log"]),
        "started_at": t.get("started_at", ""),
        "direction": t.get("direction", ""),
        "project_count": t.get("project_count", 0),
        "historical": t.get("historical", False),
    }


@app.get("/api/tasks")
async def list_tasks():
    """列出所有历史任务（含进行中）。"""
    return {"tasks": migrator.list_tasks()}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消正在运行的迁移任务。"""
    if task_id not in migrator.tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    ok = migrator.cancel_task(task_id)
    return {"ok": ok, "task_id": task_id}


@app.get("/api/tasks/{task_id}/log")
async def download_log(task_id: str):
    """下载任务日志为 txt 文件。"""
    if task_id not in migrator.tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    t = migrator.tasks[task_id]
    lines = [f"[{e.get('stream','stdout')}] {e.get('line','')}" for e in t.get("log", [])]
    content = f"=== Docker 迁移工具任务日志 ===\n任务ID: {task_id}\n状态: {t['status']}\n方向: {t.get('direction','')}\n开始时间: {t.get('started_at','')}\n\n" + "\n".join(lines)
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename=migrator_{task_id[:8]}.log"}
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
