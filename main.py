from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.responses import PlainTextResponse
import uuid
import os
import base64
import time
import secrets

app = FastAPI()
tasks: dict[str, dict[str, str]] = {}

UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")
ONE_TIME_TOKEN_TTL_SECONDS = int(os.getenv("ONE_TIME_TOKEN_TTL_SECONDS", "600"))  # 默认10分钟

# 一次性 token：token -> expire_at（epoch seconds）
one_time_tokens: dict[str, float] = {}


def _cleanup_expired_one_time_tokens() -> None:
    """清理过期的一次性 token"""
    now = time.time()
    expired = [t for t, exp in one_time_tokens.items() if exp <= now]
    for t in expired:
        del one_time_tokens[t]


def require_token(x_upload_token: str | None) -> None:
    """
    兼容两种鉴权：
    1) 长期 UPLOAD_TOKEN（原有逻辑）
    2) 一次性 token（用一次即删除）
    """
    if not UPLOAD_TOKEN:
        raise RuntimeError("UPLOAD_TOKEN not set")

    if not x_upload_token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 1) 长期 token 直接放行
    if x_upload_token == UPLOAD_TOKEN:
        return

    # 2) 一次性 token 校验
    _cleanup_expired_one_time_tokens()
    exp = one_time_tokens.get(x_upload_token)
    if not exp:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if exp <= time.time():
        # 过期就删掉
        del one_time_tokens[x_upload_token]
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 用一次即焚：鉴权通过就删除
    del one_time_tokens[x_upload_token]
    return


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

    # 只允许长期 token 签发
    if x_upload_token != UPLOAD_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    _cleanup_expired_one_time_tokens()

    token = secrets.token_hex(32)  # 64 hex chars
    one_time_tokens[token] = time.time() + ONE_TIME_TOKEN_TTL_SECONDS
    return token


@app.post("/upload", response_class=PlainTextResponse)
async def upload_file(
    key: str,
    file: UploadFile = File(...),
    x_upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
):
    # 上传需要鉴权（长期 token 或 一次性 token）
    require_token(x_upload_token)

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
        # 客户端取消/网络异常/写盘异常：尽量删除残留文件
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
        raise

    tasks[task_id] = {"key": key, "file_path": file_path}

    # 直接返回纯文本 task_id（不是 JSON）
    return task_id


@app.get("/download/{task_id}")
async def download(task_id: str):
    # 下载不需要鉴权
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="ID无效或已被使用")

    task = tasks[task_id]

    with open(task["file_path"], "rb") as f:
        content = f.read()

    # 阅后即焚
    os.remove(task["file_path"])
    del tasks[task_id]

    content_b64 = base64.b64encode(content).decode("utf-8")
    return {"encrypted_data_b64": content_b64, "aes_key": task["key"]}
