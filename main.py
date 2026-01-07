from fastapi import FastAPI, UploadFile, File, HTTPException, Header, Response
from fastapi.responses import PlainTextResponse
import uuid, os, base64, time, secrets

app = FastAPI()
tasks = {}

UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")
ONE_TIME_TOKEN_TTL_SECONDS = int(os.getenv("ONE_TIME_TOKEN_TTL_SECONDS", "600"))  # 默认10分钟

# 内存里存一次性token：token -> expire_at
one_time_tokens: dict[str, float] = {}


def _cleanup_expired_one_time_tokens() -> None:
    """清理过期的一次性token（简单做法：每次调用顺手清一下）"""
    now = time.time()
    expired = [t for t, exp in one_time_tokens.items() if exp <= now]
    for t in expired:
        del one_time_tokens[t]


def require_token(x_upload_token: str | None):
    """
    兼容两种鉴权：
    1) 长期 UPLOAD_TOKEN（原有逻辑）
    2) 一次性 token（用一次即删除）
    """
    if not UPLOAD_TOKEN:
        raise RuntimeError("UPLOAD_TOKEN not set")

    if not x_upload_token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 1) 先兼容原有：长期token
    if x_upload_token == UPLOAD_TOKEN:
        return

    # 2) 再判断一次性token
    _cleanup_expired_one_time_tokens()
    exp = one_time_tokens.get(x_upload_token)
    if not exp:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 存在但过期（理论上 cleanup 会清掉，这里再兜底一次）
    if exp <= time.time():
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
    获取一次性上传token：
    - 必须用长期 UPLOAD_TOKEN 来调用（防止被滥发）
    - 返回纯文本 token
    """
    if not UPLOAD_TOKEN:
        raise RuntimeError("UPLOAD_TOKEN not set")

    # 这里必须是长期token才允许签发一次性token
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
    # 上传需要鉴权（长期token 或 一次性token）
    require_token(x_upload_token)

    task_id = str(uuid.uuid4())
    file_path = f"/tmp/{task_id}.bin"

    with open(file_path, "wb") as f:
        f.write(await file.read())

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
