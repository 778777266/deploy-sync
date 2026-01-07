# file: main.py
from fastapi import FastAPI, UploadFile, File, HTTPException, Header, BackgroundTasks, Query
from fastapi.responses import PlainTextResponse, FileResponse
import os
import time
import uuid
import secrets

app = FastAPI()

# ------------------------
# Config
# ------------------------
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "").strip()
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/tmp").strip() or "/tmp"

# DLTOKEN 有效期（秒）
DOWNLOAD_TOKEN_TTL_SECONDS = int(os.getenv("DOWNLOAD_TOKEN_TTL_SECONDS", "180"))  # 默认 3 分钟

# 上传文件保存多久自动清理（秒）
TASK_TTL_SECONDS = int(os.getenv("TASK_TTL_SECONDS", "3600"))  # 默认 1 小时

# 上传大小限制（字节），nginx 也会有限制，这里再加一层
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(60 * 1024 * 1024)))  # 默认 60MB


# ------------------------
# In-memory state (restart will reset)
# ------------------------
# task_id -> {"file_path": str, "created_at": float, "burn_on_download": bool}
tasks: dict[str, dict[str, object]] = {}

# download_token -> {"task_id": str, "expire_at": float}
download_tokens: dict[str, dict[str, object]] = {}


# ------------------------
# Helpers
# ------------------------
def _ensure_upload_token(x_upload_token: str | None) -> None:
    if not UPLOAD_TOKEN:
        raise RuntimeError("UPLOAD_TOKEN not set")
    if not x_upload_token or x_upload_token != UPLOAD_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _delete_file_quiet(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _cleanup_expired_tasks() -> None:
    now = time.time()
    expired_ids: list[str] = []
    for task_id, info in tasks.items():
        created_at = float(info.get("created_at", 0) or 0)
        if created_at and created_at + TASK_TTL_SECONDS <= now:
            expired_ids.append(task_id)

    for task_id in expired_ids:
        info = tasks.get(task_id)
        if not info:
            continue
        fp = info.get("file_path")
        if isinstance(fp, str):
            _delete_file_quiet(fp)
        try:
            del tasks[task_id]
        except Exception:
            pass


def _cleanup_expired_download_tokens() -> None:
    now = time.time()
    expired = [t for t, info in download_tokens.items() if float(info.get("expire_at", 0) or 0) <= now]
    for t in expired:
        del download_tokens[t]


async def _save_uploadfile_to_disk(upload_file: UploadFile, dst_path: str) -> None:
    # 分块写盘避免 OOM
    with open(dst_path, "wb") as f:
        while True:
            chunk = await upload_file.read(1024 * 1024)  # 1MB
            if not chunk:
                break
            f.write(chunk)
    try:
        await upload_file.close()
    except Exception:
        pass


def _issue_download_token(task_id: str) -> str:
    _cleanup_expired_download_tokens()
    token = secrets.token_hex(32)
    download_tokens[token] = {
        "task_id": task_id,
        "expire_at": time.time() + DOWNLOAD_TOKEN_TTL_SECONDS,
    }
    return token


def _authorize_download_token_for_task(x_download_token: str | None, task_id: str | None = None) -> str:
    """
    校验一次性下载 token。
    - 如果 task_id 提供，则要求 token 绑定的 task_id 必须匹配
    - 校验通过后：token 用一次即焚
    返回：token 对应的 task_id
    """
    if not x_download_token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    _cleanup_expired_download_tokens()
    info = download_tokens.get(x_download_token)
    if not info:
        raise HTTPException(status_code=401, detail="Unauthorized")

    exp = float(info.get("expire_at", 0) or 0)
    if exp <= time.time():
        del download_tokens[x_download_token]
        raise HTTPException(status_code=401, detail="Unauthorized")

    bound_task_id = str(info.get("task_id") or "")
    if not bound_task_id:
        del download_tokens[x_download_token]
        raise HTTPException(status_code=401, detail="Unauthorized")

    if task_id is not None and bound_task_id != task_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # ✅ 用一次即焚
    del download_tokens[x_download_token]
    return bound_task_id


def _delete_task(task_id: str) -> None:
    info = tasks.get(task_id)
    if not info:
        return
    fp = info.get("file_path")
    if isinstance(fp, str):
        _delete_file_quiet(fp)
    try:
        del tasks[task_id]
    except Exception:
        pass


# ------------------------
# APIs
# ------------------------
@app.post("/upload", response_class=PlainTextResponse)
async def upload_file(
    file: UploadFile = File(...),
    x_upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
    content_length: int | None = Header(default=None, alias="Content-Length"),
):
    """
    上传密文二进制：
    - 鉴权：X-Upload-Token（长期 token）
    - 保存到 /tmp/<task_id>.bin
    - 返回：task_id|download_token（download_token 一次性，用一次即焚）
    """
    _ensure_upload_token(x_upload_token)

    _cleanup_expired_tasks()
    _cleanup_expired_download_tokens()

    if content_length is not None and content_length > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    task_id = str(uuid.uuid4())
    _ensure_parent_dir(UPLOAD_DIR)
    file_path = os.path.join(UPLOAD_DIR, f"{task_id}.bin")

    try:
        await _save_uploadfile_to_disk(file, file_path)
    except Exception:
        _delete_file_quiet(file_path)
        raise

    tasks[task_id] = {
        "file_path": file_path,
        "created_at": time.time(),
        "burn_on_download": True,
    }

    dl_token = _issue_download_token(task_id)
    return f"{task_id}|{dl_token}"


@app.post("/download-token", response_class=PlainTextResponse)
async def reissue_download_token(
    task_id: str = Query(...),
    x_upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
):
    """
    备用：如果你丢了 download_token，可以用长期 UPLOAD_TOKEN 重新签发一次性下载 token
    """
    _ensure_upload_token(x_upload_token)

    _cleanup_expired_tasks()

    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Not found")

    fp = task.get("file_path")
    if not isinstance(fp, str) or not os.path.exists(fp):
        _delete_task(task_id)
        raise HTTPException(status_code=404, detail="Not found")

    return _issue_download_token(task_id)


@app.get("/download-file")
async def download_file_fixed_url(
    background_tasks: BackgroundTasks,
    x_download_token: str | None = Header(default=None, alias="X-Download-Token"),
):
    """
    ✅ 推荐：固定 URL 下载
    - URL 永远是 /download-file
    - 只靠 X-Download-Token 找到对应 task_id
    - token 用一次即焚
    - 传完后删除密文文件
    """
    task_id = _authorize_download_token_for_task(x_download_token, task_id=None)

    _cleanup_expired_tasks()

    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Not found")

    file_path = task.get("file_path")
    if not isinstance(file_path, str) or not os.path.exists(file_path):
        _delete_task(task_id)
        raise HTTPException(status_code=404, detail="Not found")

    if bool(task.get("burn_on_download", True)):
        background_tasks.add_task(_delete_task, task_id)

    return FileResponse(
        path=file_path,
        media_type="application/octet-stream",
        filename=f"{task_id}.bin",
    )


@app.get("/download-file/{task_id}")
async def download_file_with_task_id(
    task_id: str,
    background_tasks: BackgroundTasks,
    x_download_token: str | None = Header(default=None, alias="X-Download-Token"),
):
    """
    兼容：带 task_id 的下载
    - 仍然要求 X-Download-Token 且必须绑定同一个 task_id
    - token 用一次即焚
    - 传完后删除密文文件
    """
    _authorize_download_token_for_task(x_download_token, task_id=task_id)

    _cleanup_expired_tasks()

    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Not found")

    file_path = task.get("file_path")
    if not isinstance(file_path, str) or not os.path.exists(file_path):
        _delete_task(task_id)
        raise HTTPException(status_code=404, detail="Not found")

    if bool(task.get("burn_on_download", True)):
        background_tasks.add_task(_delete_task, task_id)

    return FileResponse(
        path=file_path,
        media_type="application/octet-stream",
        filename=f"{task_id}.bin",
    )


@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "ok"
