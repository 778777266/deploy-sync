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
import base64
import time
import secrets
import re

app = FastAPI()

# ------------------------
# Config
# ------------------------
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")
ONE_TIME_TOKEN_TTL_SECONDS = int(os.getenv("ONE_TIME_TOKEN_TTL_SECONDS", "600"))  # 默认10分钟

# 存储目录（长期 token 上传的 task 文件）
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/tmp")

# 一次性 latest 槽位（一次性 token 上传会覆盖这里）
ONE_TIME_LATEST_PATH = os.getenv("ONE_TIME_LATEST_PATH", "/tmp/deploy-sync-latest.tar")
ONE_TIME_LATEST_META = os.getenv("ONE_TIME_LATEST_META", "/tmp/deploy-sync-latest.meta")  # 保存 key 等元信息

# ------------------------
# In-memory state (will reset on restart)
# ------------------------
# task_id -> {"key": str, "file_path": str, "burn_on_download": bool, "created_at": float}
tasks: dict[str, dict[str, object]] = {}

# 一次性 token：token -> expire_at（epoch seconds）
one_time_tokens: dict[str, float] = {}

# UUID 形式校验（防目录穿越）
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _cleanup_expired_one_time_tokens() -> None:
    now = time.time()
    expired = [t for t, exp in one_time_tokens.items() if exp <= now]
    for t in expired:
        del one_time_tokens[t]


def _delete_task_file_only(file_path: str) -> None:
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass


def _delete_task(task_id: str) -> None:
    """删除某个任务的文件与记录（尽量不抛异常）"""
    task = tasks.get(task_id)
    if not task:
        return
    file_path = task.get("file_path")
    if isinstance(file_path, str):
        _delete_task_file_only(file_path)
    try:
        del tasks[task_id]
    except Exception:
        pass


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


def _write_stream_to_path(upload_file: UploadFile, dst_path: str) -> None:
    """
    分块写盘（同步函数但在 async 里调用也可用；此处用 await file.read 分块）
    注意：由调用方负责 try/except 清理
    """
    # 这个函数只负责路径写入，调用方循环 await read
    raise NotImplementedError


async def _save_uploadfile_to_disk(upload_file: UploadFile, dst_path: str) -> None:
    """分块保存 UploadFile 到磁盘，避免 OOM"""
    with open(dst_path, "wb") as f:
        while True:
            chunk = await upload_file.read(1024 * 1024)  # 1MB
            if not chunk:
                break
            f.write(chunk)


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


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
    key: str = Query(...),
    file: UploadFile = File(...),
    x_upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
):
    """
    上传入口（兼容两种鉴权）：
    - 长期 token：生成 task_id，保存到 /tmp/<task_id>.bin，下载后删除
    - 一次性 token：写入 latest 槽位文件（覆盖旧文件），下载不删除（直到下次覆盖）
    """
    auth_type = authorize_token(x_upload_token)

    # 一次性 token：写 latest 槽位（覆盖旧）
    if auth_type == "one_time":
        _ensure_parent_dir(ONE_TIME_LATEST_PATH)
        _ensure_parent_dir(ONE_TIME_LATEST_META)

        tmp_path = ONE_TIME_LATEST_PATH + ".part"

        try:
            # 先写入临时文件，成功后原子替换，避免写一半留下坏文件
            await _save_uploadfile_to_disk(file, tmp_path)
            os.replace(tmp_path, ONE_TIME_LATEST_PATH)

            # 写 meta（保存 key 等信息）
            meta_tmp = ONE_TIME_LATEST_META + ".part"
            with open(meta_tmp, "w", encoding="utf-8") as mf:
                mf.write(f"key={key}\n")
                mf.write(f"updated_at={time.time()}\n")
            os.replace(meta_tmp, ONE_TIME_LATEST_META)

        except Exception:
            # 清理临时文件
            _delete_task_file_only(tmp_path)
            raise

        # 返回一个固定标识，告诉客户端用 latest 下载
        return "latest"

    # 长期 token：按 task_id 保存
    task_id = str(uuid.uuid4())
    _ensure_parent_dir(UPLOAD_DIR)
    file_path = os.path.join(UPLOAD_DIR, f"{task_id}.bin")

    try:
        await _save_uploadfile_to_disk(file, file_path)
    except Exception:
        _delete_task_file_only(file_path)
        raise

    tasks[task_id] = {
        "key": key,
        "file_path": file_path,
        "burn_on_download": True,  # 长期 token 上传：下载后删除
        "created_at": time.time(),
    }
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

    # 长期 token：阅后即焚
    if bool(task.get("burn_on_download", True)):
        _delete_task(task_id)

    content_b64 = base64.b64encode(content).decode("utf-8")
    return {"encrypted_data_b64": content_b64, "aes_key": task["key"]}


@app.get("/download-file/{task_id}")
async def download_file(task_id: str, background_tasks: BackgroundTasks):
    """
    流式下载：
    - task_id 下载：仅支持当前进程内存 tasks 中存在的 task（长期 token 上传产生的）
    - latest 下载：用 /download-file/latest
    """
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="ID无效或已被使用")

    file_path = task["file_path"]
    if not isinstance(file_path, str) or not os.path.exists(file_path):
        _delete_task(task_id)
        raise HTTPException(status_code=404, detail="ID无效或已被使用")

    # 传完再删
    if bool(task.get("burn_on_download", True)):
        background_tasks.add_task(_delete_task, task_id)

    return FileResponse(
        path=file_path,
        media_type="application/octet-stream",
        filename=f"{task_id}.bin",
    )

