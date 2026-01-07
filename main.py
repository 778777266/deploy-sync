from fastapi import (
    FastAPI,
    UploadFile,
    File,
    HTTPException,
    Header,
    BackgroundTasks,
)
from fastapi.responses import PlainTextResponse, FileResponse
import uuid
import os
import base64
import time
import secrets

app = FastAPI()

# task_id -> {"key": str, "file_path": str, "burn_on_download": bool, "created_at": float}
tasks: dict[str, dict[str, object]] = {}

UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")
ONE_TIME_TOKEN_TTL_SECONDS = int(os.getenv("ONE_TIME_TOKEN_TTL_SECONDS", "600"))  # 默认10分钟

# 一次性 token：token -> expire_at（epoch seconds）
one_time_tokens: dict[str, float] = {}

# 记录“当前活跃的一次性上传任务”（用于下一次一次性上传时删除旧文件）
active_one_time_task_id: str | None = None


def _cleanup_expired_one_time_tokens() -> None:
    now = time.time()
    expired = [t for t, exp in one_time_tokens.items() if exp <= now]
    for t in expired:
        del one_time_tokens[t]


def _delete_task(task_id: str) -> None:
    """删除某个任务的文件与记录（尽量不抛异常）"""
    task = tasks.get(task_id)
    if not task:
        return
    file_path = task.get("file_path")
    try:
        if isinstance(file_path, str) and os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass
    try:
        del tasks[task_id]
    except Exception:
        pass

    global active_one_time_task_id
    if active_one_time_task_id == task_id:
        active_one_time_task_id = None


def authorize_token(x_upload_token: str | None) -> str:
    """
    返回鉴权类型：
    - "long": 长期 UPLOAD_TOKEN
    - "one_time": 一次性 token（用一次即删除）
    """
    if not UPLOAD_TOKEN:
        raise RuntimeError("UPLOAD_TOKEN not set")

    if not x_upload_token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 1) 长期 token
    if x_upload_token == UPLOAD_TOKEN:
        return "long"

    # 2) 一次性 token
    _cleanup_expired_one_time_tokens()
    exp = one_time_tokens.get(x_upload_token)
    if not exp:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if exp <= time.time():
        del one_time_tokens[x_upload_token]
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 用一次即焚：鉴权通过就删除
    del one_time_tokens[x_upload_token]
    return "one_time"


@app.post("/upload-token", response_class=PlainTextResponse)
async def issue_one_time_upload_token(
    x_upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
):
    """
    获取一次性上传 token：
    - 必须用长期 UPLOAD_TOKEN 调用（防止被滥发）
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
    key: str,
    file: UploadFile = File(...),
    x_upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
):
    global active_one_time_task_id

    auth_type = authorize_token(x_upload_token)

    # 一次性 token 上传：先删除旧文件（旧的一次性上传）
    if auth_type == "one_time" and active_one_time_task_id:
        _delete_task(active_one_time_task_id)
        active_one_time_task_id = None

    task_id = str(uuid.uuid4())
    file_path = f"/tmp/{task_id}.bin"

    # 分块写盘，避免大文件 OOM；中断时清理残留
    try:
        with open(file_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)  # 1MB
                if not chunk:
                    break
                f.write(chunk)
    except Exception:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
        raise

    burn_on_download = (auth_type == "long")

    tasks[task_id] = {
        "key": key,
        "file_path": file_path,
        "burn_on_download": burn_on_download,
        "created_at": time.time(),
    }

    # 记录最新一次性上传（下一次一次性上传会删除它）
    if auth_type == "one_time":
        active_one_time_task_id = task_id

    return task_id


@app.get("/download/{task_id}")
async def download(task_id: str):
    """
    兼容原接口：返回 JSON（base64 + key）
    注意：大文件会占用大量内存，不推荐用于 GB 级文件。
    """
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="ID无效或已被使用")

    file_path = task["file_path"]
    if not isinstance(file_path, str) or not os.path.exists(file_path):
        _delete_task(task_id)
        raise HTTPException(status_code=404, detail="ID无效或已被使用")

    with open(file_path, "rb") as f:
        content = f.read()

    # 长期 token 上传：下载后删除；一次性 token 上传：下载不删除
    if bool(task.get("burn_on_download", True)):
        _delete_task(task_id)

    content_b64 = base64.b64encode(content).decode("utf-8")
    return {"encrypted_data_b64": content_b64, "aes_key": task["key"]}


@app.get("/download-file/{task_id}")
async def download_file(task_id: str, background_tasks: BackgroundTasks):
    """
    新增：流式下载接口（适合大文件）
    - 直接返回二进制文件（不 base64）
    - 删除策略与 /download 保持一致：
      * 长期 token 上传：传完后删除
      * 一次性 token 上传：不删除（直到下次一次性上传替换掉）
    """
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="ID无效或已被使用")

    file_path = task["file_path"]
    if not isinstance(file_path, str) or not os.path.exists(file_path):
        _delete_task(task_id)
        raise HTTPException(status_code=404, detail="ID无效或已被使用")

    # 下载完成后再删除（避免边传边删）
    if bool(task.get("burn_on_download", True)):
        background_tasks.add_task(_delete_task, task_id)

    # 给个友好的文件名
    filename = f"{task_id}.bin"
    return FileResponse(
        path=file_path,
        media_type="application/octet-stream",
        filename=filename,
    )
