from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.responses import PlainTextResponse
import uuid, os, base64

app = FastAPI()
tasks = {}

UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")

def require_token(x_upload_token: str | None):
    if not UPLOAD_TOKEN:
        raise RuntimeError("UPLOAD_TOKEN not set")
    if x_upload_token != UPLOAD_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.post("/upload", response_class=PlainTextResponse)
async def upload_file(
    key: str,
    file: UploadFile = File(...),
    x_upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
):
    # 上传需要鉴权
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
