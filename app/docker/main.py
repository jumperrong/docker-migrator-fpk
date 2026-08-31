"""飞牛 Docker 迁移工具后端（通用版：拉模式 / 推模式）。

- 拉模式（pull）：本应用部署在「目标 NAS」，SSH 连接源 NAS 扫描并拉取。
- 推模式（push）：本应用部署在「源 NAS」，扫描本机项目并推送到对端。
"""
import os
import json
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ssh_client import SSHClient
from migrator import Migrator

app = FastAPI(title="飞牛 Docker 迁移工具（通用版）")

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

migrator = Migrator()
# state: {'remote': {host/port/...}, 'mode': 'pull'|'push',
#         'scan_root': str,  # 「扫描出项目」的根目录（拉=远端，推=本机）
#         'remote_root': str # 「对端」docker root（推送时写 compose up 用）
state = {"remote": None, "mode": "pull", "scan_root": "", "remote_root": ""}


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
    direction: str = "pull"             # 'pull' | 'push'
    local_docker_root: str = "/vol1/1000/docker"
    remote_docker_root: str = "/vol1/1000/docker"
    projects: list[ProjectSpec]
    pull_images: bool = True
    start_containers: bool = True
    # 路径前缀映射：源 NAS 与目标 NAS 卷号/用户号不同时，
    # 把 compose 里以 source_prefix 开头的 bind mount 路径改写到 target_prefix
    source_prefix: str = ""
    target_prefix: str = ""


# ---------------- 路由 ----------------
@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.post("/api/connect")
async def connect(cfg: ConnectionConfig):
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


@app.post("/api/migrate")
async def migrate(req: MigrateRequest):
    if not state["remote"]:
        raise HTTPException(status_code=400, detail="请先连接对端 NAS")
    if not req.projects:
        raise HTTPException(status_code=400, detail="未选择任何项目")
    if req.direction not in ("pull", "push"):
        raise HTTPException(status_code=400, detail="direction 必须是 pull 或 push")

    import uuid
    task_id = uuid.uuid4().hex
    migrator.create_task(task_id)
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
        )
    )

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
    return {"status": t["status"], "log_len": len(t["log"])}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
