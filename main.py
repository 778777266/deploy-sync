from fastapi import (
    FastAPI,
    UploadFile,
    File,
    HTTPException,
    Header,
    BackgroundTasks,
    Query,
)
from fastapi.responses import PlainTextResponse, FileResponse
import uuid
import os
import time
import secrets

app = FastAPI()

# ------------------------
# Config
# ------------------------
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")
ONE_TIME_TOKEN_TTL_SECONDS = int(os.getenv("ONE_TIME_TOKEN_TTL_SECONDS", "600"))  # 默认10分钟
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/tmp")

# 任务文件自动清理（避免异常情况下积累）
TASK_TTL_SECONDS = int(os.getenv("TASK_TTL_SECONDS", "3600"))  # 默认1小时

# 额外保险：应用层上传大小限制（nginx 已有限制，但多一层更稳）
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(60 * 1024 * 1024)))  # 默认 60MB

# ------------------------
# In-memory state (will reset on restart)
# ------------------------
# task_id -> {"file_path": str, "burn_on_download": bool, "created_at": float}
tasks: dict[str, dict[str, object]] = {}

# 一次性上传 token：token -> expire_at（epoch seconds）
one_time_tokens: dict[str, float] = {}

# 一次性下载 token：token -> {"task_id": str, "expire_at": float}
download_tokens: dict[str, dict[str, object]] = {}


# ------------------------
# Cleanup helpers
# ------------------------
def _delete_file_quiet(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _cleanup_expired_one_time_tokens() -> None:
    now = time.time()
    expired = [t for t, exp in one_time_tokens.items() if exp <= now]
    for t in expired:
        del one_time_tokens[t]


def _cleanup_expired_download_tokens() -> None:
    now = time.time()
    expired = [t for t, info in download_tokens.items() if float(info.get("expire_at", 0)) <= now]
    for t in expired:
        del download_tokens[t]


def _cleanup_expired_tasks() -> None:
    now = time.time()
    expired_ids = []
    for task_id, info in tasks.items():
        created_at = float(info.get("created_at", 0))
        if created_at and (created_at + TASK_TTL_SECONDS) <= now:
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


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


async def _save_uploadfile_to_disk(upload_file: UploadFile, dst_path: str) -> None:
    """分块保存 UploadFile 到磁盘，避免 OOM"""
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


def authorize_upload_token(x_upload_token: str | None) -> str:
    """
    返回鉴权类型：
    - "long": 长期 UPLOAD_TOKEN
    - "one_time": 一次性 token（用一次即删除）
    """
    if not UPLOAD_TOKEN:
        raise RuntimeError("UPLOAD_TOKEN not set")

    if not x_upload_token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if x_upload_token == UPLOAD_TOKEN:
        return "long"

    _cleanup_expired_one_time_tokens()
    exp = one_time_tokens.get(x_upload_token)
    if not exp:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if exp <= time.time():
        del one_time_tokens[x_upload_token]
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 一次性 token 用一次即焚
    del one_time_tokens[x_upload_token]
    return "one_time"


def _issue_download_token(task_id: str) -> str:
    _cleanup_expired_download_tokens()
    token = secrets.token_hex(32)
    download_tokens[token] = {"task_id": task_id, "expire_at": time.time() + ONE_TIME_TOKEN_TTL_SECONDS}
    return token


def _authorize_download_token(x_download_token: str | None, task_id: str) -> None:
    if not x_download_token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    _cleanup_expired_download_tokens()
    info = download_tokens.get(x_download_token)
    if not info or info.get("task_id") != task_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    exp = float(info.get("expire_at", 0))
    if exp <= time.time():
        del download_tokens[x_download_token]
        raise HTTPException(status_code=401, detail="Unauthorized")

    # ✅ 用一次即焚
    del download_tokens[x_download_token]


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
@app.post("/upload-token", response_class=PlainTextResponse)
async def issue_one_time_upload_token(
    x_upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
):
    """
    获取一次性上传 token：
    - 必须用长期 UPLOAD_TOKEN 调用
    - 返回纯文本 token
    """
    if not UPLOAD_TOKEN:
        raise RuntimeError("UPLOAD_TOKEN not set")

    if x_upload_token != UPLOAD_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    _cleanup_expired_one_time_tokens()
    token = secrets.token_hex(32)
    one_time_tokens[token] = time.time() + ONE_TIME_TOKEN_TTL_SECONDS
    return token


@app.post("/upload", response_class=PlainTextResponse)
async def upload_file(
    file: UploadFile = File(...),
    x_upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
    content_length: int | None = Header(default=None, alias="Content-Length"),
):
    """
    上传入口（密文二进制）：
    - 鉴权：长期 token 或一次性 token
    - 保存到 UPLOAD_DIR/<task_id>.bin
    - 返回：task_id|download_token（download_token 一次性，用一次即焚，TTL=ONE_TIME_TOKEN_TTL_SECONDS）
    """
    authorize_upload_token(x_upload_token)

    # 清理过期任务/下载 token（避免堆积）
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
        "burn_on_download": True,
        "created_at": time.time(),
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
    if not UPLOAD_TOKEN:
        raise RuntimeError("UPLOAD_TOKEN not set")

    if x_upload_token != UPLOAD_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    _cleanup_expired_tasks()

    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Not found")

    fp = task.get("file_path")
    if not isinstance(fp, str) or not os.path.exists(fp):
        _delete_task(task_id)
        raise HTTPException(status_code=404, detail="Not found")

    return _issue_download_token(task_id)


@app.get("/download-file/{task_id}")
async def download_file(
    task_id: str,
    background_tasks: BackgroundTasks,
    x_download_token: str | None = Header(default=None, alias="X-Download-Token"),
):
    """
    流式下载密文（FileResponse）：
    - 必须带一次性 X-Download-Token（用一次即焚）
    - 传完后删除密文文件（burn_on_download=True）
    """
    _authorize_download_token(x_download_token, task_id)

    _cleanup_expired_tasks()

    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="ID无效或已被使用")

    file_path = task.get("file_path")
    if not isinstance(file_path, str) or not os.path.exists(file_path):
        _delete_task(task_id)
        raise HTTPException(status_code=404, detail="ID无效或已被使用")

    if bool(task.get("burn_on_download", True)):
        background_tasks.add_task(_delete_task, task_id)

    return FileResponse(
        path=file_path,
        media_type="application/octet-stream",
        filename=f"{task_id}.bin",
    )
