# 知识图谱 RAG —— 单进程镜像
# FastAPI(uvicorn) 一个进程同时托管 /api 接口 和 已经 build 好的前端 frontend/dist。
# 前端不在容器里编译：镜像直接用仓库里现成的 frontend/dist。

FROM python:3.9-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app

# faiss / sentence-transformers 的少量编译期依赖
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# 先只拷依赖清单，命中 Docker 层缓存
COPY requirements.txt .

# torch 用 CPU 专用轮子（约 200MB，避免 PyPI 上带 CUDA 的 ~2GB 版本），
# 其余依赖走 PyPI。requirements.txt 里的 torch==2.8.0 之后会被判定为已满足。
RUN pip install --upgrade pip \
 && pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements.txt

# 把 embedding 模型（all-MiniLM-L6-v2，约 90MB）预下载进镜像，
# 这样容器首次查询不用联网、也不卡在下载上。
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# 拷项目代码（.dockerignore 已排除 node_modules / 日志 / 启动器等）
COPY . .

EXPOSE 5001

# app.py 内部 uvicorn.run(host="0.0.0.0", port=5001)
CMD ["python", "app.py"]
