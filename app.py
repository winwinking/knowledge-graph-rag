import hashlib
import json
import logging
import mimetypes
import os
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

# Windows 的 mimetypes 会从注册表读，常把 .js 判成 text/plain，
# 导致 <script type="module"> 被浏览器拒绝执行 —— 强制指定一下。
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 打包后的前端静态文件目录（frontend 里 npm run build 生成）
_DIST_DIR = os.path.join(_BASE_DIR, "frontend", "dist")
_ASSETS_DIR = os.path.join(_DIST_DIR, "assets")

# 先配好日志，再 import workflow（workflow 里的 logger 会挂到这个 root handler 上）
_LOG_FILE = os.path.join(_BASE_DIR, "app.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        # 通过 start_app.bat 隐藏窗口启动时看不到控制台，日志同时写到文件
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("kg_rag.app")
log.info("日志文件：%s", _LOG_FILE)

import api_key_store  # noqa: E402
import kb_store  # noqa: E402
import chat_store  # noqa: E402
from workflow import run_query, run_query_stream, get_stats  # noqa: E402

# 首次启动：把旧的扁平 knowledge_graph.db / faiss_index 迁移成默认知识库「AI面试题」
try:
    kb_store.ensure_default_kb()
except Exception:
    log.exception("ensure_default_kb 失败（不影响已迁移的情况）")


# ==================== Redis 查询缓存 ====================
# 只缓存非流式 /api/chat 的结果，key = md5(query + kb_id)，TTL 1 小时。
# Redis 连不上（没装/没启动）时 _redis 保持 None，缓存整体失效但服务照常跑。
# 连接地址从环境变量读：REDIS_HOST（默认 localhost）/ REDIS_PORT（默认 6379），
# docker-compose 里传 REDIS_HOST=redis 走内部网络。

try:
    import redis as _redis_lib  # noqa: E402
except ImportError:
    _redis_lib = None
    log.warning("未安装 redis 库（pip install redis），查询缓存已禁用")

_CACHE_TTL = 3600
_CACHE_PREFIX = "kgrag:chat:"      # 完整 key：kgrag:chat:<kb_id>:<md5>
_REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
_REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
_redis = None
_cache_inval_seen = set()          # 已处理过「入库完成清缓存」的 job_id


def _init_redis():
    global _redis
    if _redis_lib is None:
        return
    try:
        client = _redis_lib.Redis(
            host=_REDIS_HOST, port=_REDIS_PORT, db=0,
            socket_connect_timeout=1, socket_timeout=2,
            decode_responses=True,
        )
        client.ping()
        _redis = client
        log.info(
            "已连接 Redis（%s:%d），/api/chat 查询缓存启用（TTL=%ds）",
            _REDIS_HOST, _REDIS_PORT, _CACHE_TTL,
        )
    except Exception as e:
        _redis = None
        log.warning(
            "连接 Redis 失败（%s:%d：%s），查询缓存已禁用，服务正常运行",
            _REDIS_HOST, _REDIS_PORT, e,
        )


_init_redis()


def _cache_key(query: str, kb_id: str) -> str:
    digest = hashlib.md5(f"{query}\x00{kb_id}".encode("utf-8")).hexdigest()
    return f"{_CACHE_PREFIX}{kb_id}:{digest}"


def _cache_get(query: str, kb_id: str):
    if _redis is None:
        return None
    try:
        raw = _redis.get(_cache_key(query, kb_id))
        if raw:
            return json.loads(raw)
    except Exception:
        log.exception("读查询缓存失败（忽略，照常走 workflow）")
    return None


def _cache_set(query: str, kb_id: str, result: dict):
    if _redis is None:
        return
    try:
        _redis.setex(_cache_key(query, kb_id), _CACHE_TTL, json.dumps(result, ensure_ascii=False))
    except Exception:
        log.exception("写查询缓存失败（忽略）")


def _cache_clear(kb_id: Optional[str] = None) -> int:
    """清缓存：给了 kb_id 就只清那个库的，否则清全部。返回删除条数。"""
    if _redis is None:
        return 0
    pattern = f"{_CACHE_PREFIX}{kb_id}:*" if kb_id else f"{_CACHE_PREFIX}*"
    removed = 0
    try:
        batch = []
        for key in _redis.scan_iter(match=pattern, count=500):
            batch.append(key)
            if len(batch) >= 500:
                removed += _redis.delete(*batch)
                batch = []
        if batch:
            removed += _redis.delete(*batch)
    except Exception:
        log.exception("清查询缓存失败")
    return removed


def _clear_cache_when_job_done(job_id: str, kb_id: str):
    """上传是后台任务，等它真正入库完成后再清这个库的缓存（不依赖前端轮询）。"""
    if _redis is None:
        return

    def _watch():
        for _ in range(2400):        # 最多盯 20 分钟
            job = kb_store.get_job(job_id)
            if job is None:
                return
            status = job.get("status")
            if status in ("done", "error"):
                if status == "done" and job_id not in _cache_inval_seen:
                    _cache_inval_seen.add(job_id)
                    n = _cache_clear(kb_id)
                    log.info("文档入库完成，清除知识库 %s 的查询缓存 %d 条", kb_id, n)
                return
            time.sleep(0.5)

    threading.Thread(target=_watch, daemon=True, name=f"cache-inval-{job_id[:8]}").start()


# ==================== 请求 / 响应模型 ====================

class ChatRequest(BaseModel):
    query: Optional[str] = None
    message: Optional[str] = None            # 兼容旧字段名
    kb_id: Optional[str] = None
    chat_id: Optional[str] = None


class CreateKbRequest(BaseModel):
    name: Optional[str] = None


class SetApiKeyRequest(BaseModel):
    api_key: Optional[str] = None


class ApiKeyStatusResponse(BaseModel):
    configured: bool
    source: str                # user / env / none
    last4: Optional[str] = None


class ChatResponse(BaseModel):
    # 回答里还带 vector_chunks 等动态字段，允许透传
    model_config = ConfigDict(extra="allow")
    answer: str = ""
    intent: str = ""
    vector_results: List[Any] = []
    graph_results: List[Any] = []
    graph_nodes: List[Any] = []
    suggestions: List[Any] = []
    verified: bool = False
    kb_id: Optional[str] = None
    skill: Optional[str] = None            # 当前激活的 Skill id，未加载则 None
    question_type: Optional[str] = None    # Skill 判定的细粒度问题类型（concept/comparison/...）
    error: Optional[str] = None


class ErrorResponse(BaseModel):
    error: str


class StatusResponse(BaseModel):
    status: str


class CacheClearResponse(BaseModel):
    status: str            # cleared / disabled
    cleared: int           # 删除的缓存条数
    kb_id: Optional[str] = None


class ClearContextResponse(BaseModel):
    status: str
    divider: Optional[Dict[str, Any]] = None


class StatsResponse(BaseModel):
    """知识库统计，字段随实现变化，直接透传。"""
    model_config = ConfigDict(extra="allow")


class KbSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    name: str


class KbListResponse(BaseModel):
    kbs: List[KbSummary]
    default: str


class KbDetailResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    stats: Dict[str, Any] = {}


class UploadResponse(BaseModel):
    job_id: str
    filename: str


class JobResponse(BaseModel):
    """后台建库任务状态，字段较多，直接透传。"""
    model_config = ConfigDict(extra="allow")


class ChatListItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    title: str


class ChatListResponse(BaseModel):
    chats: List[ChatListItem]


class ChatRecordResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    messages: List[Any] = []


# ==================== 应用 ====================

app = FastAPI(title="Knowledge Graph RAG", version="2.0.0", description="LangGraph 知识图谱 RAG 后端")

# 同源部署本不需要 CORS；这里放开是方便直接用 `npm run dev`(5173) 打后端调试。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- 统一错误格式：前端只认 body 里的 {"error": ...} ----

@app.exception_handler(StarletteHTTPException)
async def _on_http_exc(request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.exception_handler(RequestValidationError)
async def _on_validation_exc(request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"error": f"请求参数有误: {exc.errors()}"})


@app.exception_handler(Exception)
async def _on_any_exc(request, exc: Exception):
    log.error("未捕获异常：%s\n%s", exc, traceback.format_exc())
    return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})


# ==================== 后端接口（统一挂在 /api 下） ====================

@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    t0 = time.time()
    query = (req.query or req.message or "").strip()
    kb_id = (req.kb_id or "").strip() or None
    chat_id = (req.chat_id or "").strip() or None
    log.info("收到 /chat 请求：%r（kb=%s，chat=%s）", query, kb_id, chat_id)

    if not query:
        log.warning("/chat 缺少 query 字段")
        raise HTTPException(status_code=400, detail="query 不能为空")

    history = chat_store.get_context(chat_id) if chat_id else None
    resolved_kb = kb_store.resolve_kb_id(kb_id)
    # 有对话上下文时，答案依赖前文，不能只按 query+kb_id 命中缓存
    use_cache = not history

    if use_cache:
        cached = _cache_get(query, resolved_kb)
        if cached is not None:
            if isinstance(cached, dict):
                cached["cached"] = True
            log.info("/chat 命中缓存，跳过 workflow（%.3fs）", time.time() - t0)
            # 命中缓存也要把这轮写进聊天历史 / 上下文
            if chat_id and isinstance(cached, dict) and cached.get("answer") and not cached.get("error"):
                try:
                    chat_store.append_turn(chat_id, kb_id, query, cached["answer"], cached.get("verified"))
                    chat_store.record_context(chat_id, query, cached["answer"])
                except Exception:
                    log.exception("/chat 写历史失败（缓存命中）chat=%s", chat_id)
            return cached

    try:
        result = run_query(query, kb_id, history)
    except Exception as e:
        log.error("/chat 处理异常：%s\n%s", e, traceback.format_exc())
        return JSONResponse(status_code=500, content={
            "error": f"{type(e).__name__}: {e}",
            "answer": "",
            "intent": "",
            "vector_results": [],
            "graph_results": [],
            "graph_nodes": [],
        })

    # 再保险一层：确认结果能被 JSON 序列化
    try:
        json.dumps(result, ensure_ascii=False)
    except Exception as e:
        log.error("/chat 结果无法 JSON 序列化：%s\n%s", e, traceback.format_exc())
        return JSONResponse(status_code=500, content={
            "error": f"响应序列化失败: {e}",
            "answer": result.get("answer", "") if isinstance(result, dict) else "",
            "intent": "",
            "vector_results": [],
            "graph_results": [],
            "graph_nodes": [],
        })

    if use_cache and result.get("answer") and not result.get("error"):
        _cache_set(query, resolved_kb, result)

    if chat_id and result.get("answer") and not result.get("error"):
        try:
            chat_store.append_turn(chat_id, kb_id, query, result["answer"], result.get("verified"))
            chat_store.record_context(chat_id, query, result["answer"])
        except Exception:
            log.exception("/chat 写历史失败 chat=%s", chat_id)

    log.info("/chat 返回成功，耗时 %.1fs", time.time() - t0)
    return result


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest):
    """SSE 流式版 /chat。事件：meta / token / verifying / revise / done / error。"""
    query = (req.query or req.message or "").strip()
    kb_id = (req.kb_id or "").strip() or None
    chat_id = (req.chat_id or "").strip() or None
    log.info("收到 /chat/stream 请求：%r（kb=%s，chat=%s）", query, kb_id, chat_id)
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")

    history = chat_store.get_context(chat_id) if chat_id else None

    def gen():
        t0 = time.time()
        answer_buf = ""
        verified = None
        errored = False
        try:
            for kind, payload in run_query_stream(query, kb_id, history):
                if kind == "token":
                    answer_buf += payload
                elif kind == "revise":
                    answer_buf = ""   # 后端要重新流式，前端也会清空
                elif kind == "done":
                    verified = payload.get("verified")
                elif kind == "error":
                    errored = True
                yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception as e:
            errored = True
            log.error("/chat/stream 异常：%s\n%s", e, traceback.format_exc())
            yield f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            if chat_id and answer_buf.strip() and not errored:
                try:
                    chat_store.append_turn(chat_id, kb_id, query, answer_buf, verified)
                    chat_store.record_context(chat_id, query, answer_buf)
                except Exception:
                    log.exception("/chat/stream 写历史失败 chat=%s", chat_id)
            log.info("/chat/stream 结束，耗时 %.1fs", time.time() - t0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 关掉可能的反代缓冲
            "Connection": "keep-alive",
        },
    )


@app.post("/api/reset", response_model=StatusResponse)
def reset():
    """保留的旧端点。新建对话现在由前端换 chat_id 完成，历史后端自动存。"""
    log.info("收到 /reset")
    return {"status": "reset"}


# ==================== 聊天历史 ====================

@app.get("/api/chats", response_model=ChatListResponse)
def chats_list():
    return {"chats": chat_store.list_chats()}


@app.get("/api/chats/{chat_id}", response_model=ChatRecordResponse)
def chat_get(chat_id: str):
    chat = chat_store.load_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="对话不存在")
    return chat


@app.delete("/api/chats/{chat_id}", response_model=StatusResponse)
def chat_delete(chat_id: str):
    try:
        chat_store.delete_chat(chat_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error("/chats 删除失败：%s\n%s", e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "deleted"}


@app.post("/api/chats/{chat_id}/clear_context", response_model=ClearContextResponse)
def chat_clear_context(chat_id: str):
    """清空这个对话的上下文（后端记忆），历史消息保留，插一条分隔线。"""
    div = chat_store.clear_context(chat_id)
    log.info("对话 %s 清空上下文", chat_id)
    return {"status": "cleared", "divider": div}


@app.get("/api/stats", response_model=StatsResponse)
def stats(kb_id: Optional[str] = None):
    kb_id = (kb_id or "").strip() or None
    try:
        return get_stats(kb_id)
    except Exception as e:
        log.error("/stats 失败：%s\n%s", e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 知识库管理 ====================

@app.get("/api/kb", response_model=KbListResponse)
def kb_list():
    return {"kbs": kb_store.list_kbs(), "default": kb_store.DEFAULT_KB_ID}


@app.post("/api/kb/create", response_model=KbSummary)
def kb_create(req: CreateKbRequest):
    try:
        entry = kb_store.create_kb(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error("/kb/create 失败：%s\n%s", e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    return entry


@app.get("/api/kb/{kb_id}", response_model=KbDetailResponse)
def kb_detail(kb_id: str):
    kb = kb_store.get_kb(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    out = dict(kb)
    try:
        out["stats"] = kb_store.kb_stats(kb_id)
    except Exception:
        log.exception("/kb/%s stats 失败", kb_id)
        out["stats"] = {}
    return out


@app.post("/api/kb/{kb_id}/upload", response_model=UploadResponse)
async def kb_upload(kb_id: str, file: UploadFile = File(...)):
    """上传 .md / .pdf。建库要逐块调 DeepSeek，很慢，这里丢后台线程，
    返回 job_id，前端轮询 /api/kb/job/<job_id> 看进度。"""
    if not kb_store.get_kb(kb_id):
        raise HTTPException(status_code=404, detail="知识库不存在")
    if file is None or not file.filename:
        raise HTTPException(status_code=400, detail="没有收到文件")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件是空的")
    if not file.filename.lower().endswith(kb_store.SUPPORTED_EXT):
        raise HTTPException(status_code=400, detail="只支持 .md / .pdf 文件")
    job_id = kb_store.add_document_async(kb_id, file.filename, data)
    log.info("/kb/%s/upload 收到 %s（%d 字节），job=%s", kb_id, file.filename, len(data), job_id)
    # 入库是后台任务，等它跑完再清这个库的查询缓存（库内容变了，旧缓存不准）
    _clear_cache_when_job_done(job_id, kb_id)
    return {"job_id": job_id, "filename": file.filename}


@app.get("/api/kb/job/{job_id}", response_model=JobResponse)
def kb_job(job_id: str):
    job = kb_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job 不存在或已过期")
    return job


@app.delete("/api/kb/{kb_id}/doc/{filename:path}", response_model=StatusResponse)
def kb_delete_doc(kb_id: str, filename: str):
    if not kb_store.get_kb(kb_id):
        raise HTTPException(status_code=404, detail="知识库不存在")
    try:
        kb_store.delete_document(kb_id, filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error("/kb/%s/doc 删除失败：%s\n%s", kb_id, e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    # 库内容变了，清掉这个库的查询缓存
    n = _cache_clear(kb_id)
    if n:
        log.info("删文档后清除知识库 %s 的查询缓存 %d 条", kb_id, n)
    return {"status": "deleted"}


@app.delete("/api/cache", response_model=CacheClearResponse)
def cache_clear(kb_id: Optional[str] = None):
    """清空 /api/chat 查询缓存。带 ?kb_id=<id> 只清该库的，不带则清全部。"""
    kb_id = (kb_id or "").strip() or None
    if _redis is None:
        return {"status": "disabled", "cleared": 0, "kb_id": kb_id}
    n = _cache_clear(kb_id)
    log.info("清除查询缓存：kb=%s，删除 %d 条", kb_id or "(全部)", n)
    return {"status": "cleared", "cleared": n, "kb_id": kb_id}


@app.get("/health", response_model=StatusResponse)
@app.get("/api/health", response_model=StatusResponse)
def health():
    return {"status": "ok"}


# ==================== API Key 设置 ====================
# 只存进程内存，不写文件不写数据库——重启后端需要重新输入，这是刻意的取舍，
# 避免 key 落盘。MCP server（mcp_server.py）走独立进程，不调 DeepSeek，不需要这个 key。

@app.post("/api/settings/apikey", response_model=StatusResponse)
def set_apikey(req: SetApiKeyRequest):
    key = (req.api_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key 不能为空")
    api_key_store.set_user_key(key)
    log.info("已通过网页设置 DeepSeek API key（仅存内存，尾号 %s）", key[-4:])
    return {"status": "ok"}


@app.get("/api/settings/apikey/status", response_model=ApiKeyStatusResponse)
def apikey_status():
    return api_key_store.status()


# ==================== 前端静态文件（打包后的 dist） ====================

_NOT_BUILT_HTML = (
    "<h1>前端还没打包</h1>"
    "<p>在 <code>frontend</code> 目录执行 <code>npm run build</code> 后刷新本页。</p>"
    "<p>要调试前端可以另开一个终端跑 <code>npm run dev</code>（端口 5173）。</p>"
)

# Vite 打包后所有带 hash 的静态资源都在 dist/assets 下
if os.path.isdir(_ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")
else:
    log.warning("没找到 %s，前端页面会提示先 npm run build", _ASSETS_DIR)


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str):
    """前端单页应用：/ 和未匹配到的路径都回 index.html；/api/* 一律 404。"""
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")

    if full_path:
        candidate = os.path.normpath(os.path.join(_DIST_DIR, full_path))
        if candidate.startswith(_DIST_DIR) and os.path.isfile(candidate):
            return FileResponse(candidate)

    index_path = os.path.join(_DIST_DIR, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)
    return HTMLResponse(_NOT_BUILT_HTML, status_code=503)


if __name__ == "__main__":
    import uvicorn

    if os.path.isfile(os.path.join(_DIST_DIR, "index.html")):
        log.info("前端静态文件：%s", _DIST_DIR)
    else:
        log.warning("没找到 %s，前端页面会提示先 npm run build", os.path.join(_DIST_DIR, "index.html"))
    log.info("启动 FastAPI/uvicorn，页面 + 接口都在 http://127.0.0.1:5001 （接口在 /api 下，文档 /docs）")
    uvicorn.run(app, host="0.0.0.0", port=5001)
