from fastapi import FastAPI, UploadFile, File, HTTPException
import uuid
import os

app = FastAPI()

# 存储记录：{ task_id: {"key": "...", "file_path": "..."} }
tasks = {}

@app.post("/upload")
async def upload_file(key: str, file: UploadFile = File(...)):
    task_id = str(uuid.uuid4())
    file_path = f"/tmp/{task_id}.bin"
    
    # 保存加密文件
    with open(file_path, "wb") as f:
        f.write(await file.read())
    
    tasks[task_id] = {"key": key, "file_path": file_path}
    return {"task_id": task_id}

@app.get("/download/{task_id}")
async def download(task_id: str):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="ID无效或已被使用")
    
    task = tasks[task_id]
    
    # 读取内容
    with open(task["file_path"], "rb") as f:
        content = f.read()
    
    # --- 核心逻辑：阅后即焚 ---
    key = task["key"]
    os.remove(task["file_path"]) # 删除物理文件
    del tasks[task_id]           # 删除内存记录
    
    return {"encrypted_data_b64": content, "aes_key": key}