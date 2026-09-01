"""WebUI 安全：Basic Auth + 路径校验 + 输入格式校验。

设计原则：
- 密码通过环境变量 MIGRATOR_WEB_PASSWORD 注入（由飞牛安装向导 / compose env 设置）
- 未设置密码时默认 'admin'，首次访问提示修改（飞牛内网环境，弱认证即可）
- 所有用户输入的路径、主机名做严格格式校验，防注入与遍历
"""
import os
import re
import secrets
from functools import wraps

from fastapi import Request, HTTPException
from fastapi.responses import Response

# ---------------- 配置 ----------------
WEB_USERNAME = os.getenv("MIGRATOR_WEB_USER", "admin")
WEB_PASSWORD = os.getenv("MIGRATOR_WEB_PASSWORD", "admin")
# 飞牛内网场景，默认弱口令；如需强认证在 compose env 覆盖

# 路径格式：必须是绝对路径，不含 .. 遍历
_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9_.\-][A-Za-z0-9_./\-]*$")
# 主机名/IP：域名或 IPv4
_HOST_RE = re.compile(
    r"^(?:(?:\d{1,3}\.){3}\d{1,3})|"  # IPv4
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$"
)
# 项目名：禁止 . / \ 和绝对路径
_PROJ_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


def validate_abs_path(path: str, field_name: str = "路径") -> str:
    """校验绝对路径格式，拒绝 .. 遍历和相对路径。"""
    if not path:
        return path
    path = path.strip()
    if not _ABS_PATH_RE.match(path):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name}格式不合法：必须是绝对路径（以 / 开头），不含 .. 或特殊字符，当前值: {path!r}"
        )
    if ".." in path.split("/"):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name}不允许包含 .. 路径遍历: {path!r}"
        )
    return path


def validate_host(host: str) -> str:
    """校验主机名/IP 格式，防 SSH 命令注入。"""
    host = (host or "").strip()
    if not host:
        raise HTTPException(status_code=400, detail="主机 IP/域名不能为空")
    if not _HOST_RE.match(host) or len(host) > 255:
        raise HTTPException(
            status_code=400,
            detail=f"主机格式不合法（须为 IPv4 或域名）: {host!r}"
        )
    return host


def validate_port(port: int) -> int:
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise HTTPException(status_code=400, detail=f"端口须为 1-65535，当前: {port}")
    return port


def validate_proj_name(name: str) -> str:
    """校验 compose project name，防路径遍历和命令注入。"""
    if not name or not _PROJ_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"项目名不合法（字母/数字/下划线开头，不含 / \\ ..）: {name!r}"
        )
    return name


def mask_secrets_in_text(text: str, secrets_list=None) -> str:
    """在日志文本中打码密码等敏感字段。

    secrets_list = [password1, password2, ...] 要打码的明文。
    """
    if not text or not secrets_list:
        return text
    masked = text
    for s in secrets_list:
        if s and len(s) >= 3:
            masked = masked.replace(s, s[:2] + "***")
    return masked


def check_basic_auth(request: Request) -> bool:
    """检查 Basic Auth。返回是否通过。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    import base64
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        user, _, pwd = decoded.partition(":")
        # 用 secrets.compare_bytes 防时序攻击
        u_ok = secrets.compare_digest(user.encode(), WEB_USERNAME.encode())
        p_ok = secrets.compare_digest(pwd.encode(), WEB_PASSWORD.encode())
        return u_ok and p_ok
    except Exception:
        return False


def require_auth(request: Request):
    """FastAPI 依赖：要求 Basic Auth 通过。"""
    if check_basic_auth(request):
        return
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Docker Migrator"'},
    )
