from fastapi import FastAPI, UploadFile, File, HTTPException, Header
import uuid, os, base64

app = FastAPI()
tasks = {}

UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")

def require_token(x_upload_token: str | None):
    if not UPLOAD_TOKEN:
        raise RuntimeError("UPLOAD_TOKEN not set")
    if x_upload_token != UPLOAD_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.post("/upload")
async def upload_file(
    key: str,
    file: UploadFile = File(...),
    x_upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
):
    require_token(x_upload_token)

    task_id = str(uuid.uuid4())
    file_path = f"/tmp/{task_id}.bin"
    with open(file_path, "wb") as f:
        f.write(await file.read())

    tasks[task_id] = {"key": key, "file_path": file_path}
    return {"task_id": task_id}

@app.get("/download/{task_id}")
async def download(
    task_id: str,
    x_upload_token: str | None = Header(default=None, alias="X-Upload-Token"),
):
    require_token(x_upload_token)

    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="ID无效或已被使用")

    task = tasks[task_id]
    with open(task["file_path"], "rb") as f:
        content = f.read()

    os.remove(task["file_path"])
    del tasks[task_id]

    # bytes -> base64 (JSON 里必须是字符串)
    content_b64 = base64.b64encode(content).decode("utf-8")
    return {"encrypted_data_b64": content_b64, "aes_key": task["key"]}
