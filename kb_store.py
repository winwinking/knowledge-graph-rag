"""多知识库存储与管理。

目录结构（都在项目根的 knowledge_bases/ 下）：
    knowledge_bases/
    ├─ registry.json                # 所有知识库的注册表：库名 / 创建时间 / 文档列表
    └─ <kb_id>/
       ├─ graph.db                  # 该库独立的 SQLite（entities / relations，带 source_file）
       ├─ faiss_index/              # 该库独立的 FAISS 索引
       └─ docs/                     # 上传的原始文件副本

首次导入时 ensure_default_kb() 把旧的扁平产物（项目根的 knowledge_graph.db /
faiss_index/ + 源 md 目录）迁移成一个默认知识库「AI面试题」。

切块 / 抽实体关系 / 建库的逻辑集中在这里，graph_builder.py 和 app.py 都调用它。
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime

import api_key_store

logger = logging.getLogger("kg_rag.kb_store")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.join(BASE_DIR, "knowledge_bases")
REGISTRY_PATH = os.path.join(KB_ROOT, "registry.json")

DEFAULT_KB_ID = "ai_interview"
DEFAULT_KB_NAME = "AI面试题"

# 旧的扁平产物 + 源文档目录，仅用于首次迁移
_LEGACY_DB = os.path.join(BASE_DIR, "knowledge_graph.db")
_LEGACY_FAISS = os.path.join(BASE_DIR, "faiss_index")
_LEGACY_DOCS = r"E:\ailearning\ai-agent-interview-guide-main\ai-agent-interview-guide-main\docs\01-面试八股文"

MAX_CHUNK_CHARS = 1500
SUPPORTED_EXT = (".md", ".markdown", ".txt", ".pdf")

# DeepSeek（跟 workflow.py 一致；key 从 api_key_store 取，不再硬编码）
_DEEPSEEK_BASE = "https://api.deepseek.com"

_EXTRACT_PROMPT = """你是一个信息提取助手，从以下文本中提取实体和关系，以JSON格式输出。
JSON里要有entities和relations两个列表。
entities格式：{"name": "实体名", "type": "实体类型"}
relations格式：{"entity1": "实体1", "relation": "关系", "entity2": "实体2"}
只提取原文中明确提到的关系，不要推理或补充文档中没有的信息。
只输出JSON，不要任何其他文字。

文本：
"""

# registry 读写 + 建库全程串行化，避免并发上传把索引写坏
_lock = threading.RLock()

# 后台任务表：job_id -> dict（上传较慢，前端轮询这个）
_jobs = {}
_JOBS_MAX = 50


# ==================== 基础路径 / registry ====================

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def kb_dir(kb_id):
    return os.path.join(KB_ROOT, kb_id)


def kb_db_path(kb_id):
    return os.path.join(kb_dir(kb_id), "graph.db")


def kb_faiss_dir(kb_id):
    return os.path.join(kb_dir(kb_id), "faiss_index")


def kb_docs_dir(kb_id):
    return os.path.join(kb_dir(kb_id), "docs")


def _load_registry():
    if not os.path.isfile(REGISTRY_PATH):
        return {"kbs": []}
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            reg = json.load(f)
        if not isinstance(reg, dict) or "kbs" not in reg:
            return {"kbs": []}
        return reg
    except Exception:
        logger.exception("registry.json 读取失败，按空处理")
        return {"kbs": []}


def _save_registry(reg):
    os.makedirs(KB_ROOT, exist_ok=True)
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REGISTRY_PATH)  # 原子替换


def _safe_filename(name):
    name = os.path.basename(str(name or "")).replace("\\", "").strip()
    if not name or name in (".", ".."):
        raise ValueError("非法文件名")
    return name


def _new_kb_id(name, existing_ids):
    s = re.sub(r"[^a-zA-Z0-9]+", "_", str(name or "")).strip("_").lower()
    if len(s) < 3 or not re.match(r"^[a-z]", s):
        s = "kb"
    kb_id = s
    i = 2
    while kb_id in existing_ids or os.path.exists(kb_dir(kb_id)):
        kb_id = f"{s}_{i}"
        i += 1
    return kb_id


# ==================== SQLite ====================

def _init_db(db_path):
    """建表 + 给旧库补 source_file 列。返回打开的连接。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS entities ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, type TEXT, source_file TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS relations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, entity1 TEXT, relation TEXT, entity2 TEXT, source_file TEXT)"
    )
    for tbl in ("entities", "relations"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
        if "source_file" not in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN source_file TEXT")
    conn.commit()
    return conn


def _insert_extractions(conn, entities, relations, source_file):
    """写入某个文档抽出来的实体/关系。
    去重只在「同一 source_file 内」做（按小写名），不同文档间允许同名实体各存一份，
    这样按 source_file 删文档时能干净移除。"""
    cur = conn.cursor()
    seen = set()
    for (n,) in cur.execute("SELECT name FROM entities WHERE source_file=?", (source_file,)):
        if n:
            seen.add(n.strip().lower())
    for e in entities:
        name = (e.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cur.execute(
            "INSERT INTO entities (name, type, source_file) VALUES (?, ?, ?)",
            (name, (e.get("type") or "未知"), source_file),
        )
    for r in relations:
        if r.get("entity1") and r.get("relation") and r.get("entity2"):
            cur.execute(
                "INSERT INTO relations (entity1, relation, entity2, source_file) VALUES (?, ?, ?, ?)",
                (r["entity1"], r["relation"], r["entity2"], source_file),
            )
    conn.commit()


# ==================== 切块 ====================

def _chunk_markdown(text, source_file):
    from langchain_text_splitters import (
        MarkdownHeaderTextSplitter,
        RecursiveCharacterTextSplitter,
    )

    md_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")]
    )
    sub_splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=150)

    out = []
    for d in md_splitter.split_text(text):
        headers = [d.metadata[k] for k in ("h1", "h2", "h3") if d.metadata.get(k)]
        header_str = " > ".join(headers)
        d.metadata = {"source_file": source_file, "header": header_str}
        if header_str:
            d.page_content = f"{header_str}\n\n{d.page_content}"
        if len(d.page_content) > MAX_CHUNK_CHARS:
            for sub in sub_splitter.split_documents([d]):
                sub.metadata = {"source_file": source_file, "header": header_str}
                out.append(sub)
        else:
            out.append(d)
    return out


def _chunk_pdf(path, source_file):
    from langchain_core.documents import Document
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError("处理 PDF 需要 pypdf，请先运行：pip install pypdf")

    reader = PdfReader(path)
    text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if not text:
        return []
    sub_splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=150)
    doc = Document(page_content=text, metadata={"source_file": source_file, "header": ""})
    subs = sub_splitter.split_documents([doc])
    for s in subs:
        s.metadata = {"source_file": source_file, "header": ""}
    return subs


def _chunk_file(path):
    name = os.path.basename(path)
    if name.lower().endswith(".pdf"):
        return _chunk_pdf(path, name)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return _chunk_markdown(f.read(), name)


# ==================== 抽实体关系 / 向量化 ====================

_embeddings = None


def get_embeddings():
    """all-MiniLM-L6-v2，全进程共用一份（workflow.py 也走这里，避免重复加载）。"""
    global _embeddings
    if _embeddings is None:
        from langchain_huggingface import HuggingFaceEmbeddings

        _embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
    return _embeddings


def _extract(chunks, progress=None):
    """逐块调 DeepSeek 提取实体/关系。progress(done, total) 可选，用于上报进度。
    没有配置 API key 时直接跳过抽取（文档仍会正常切块 + 向量化，只是没有实体/关系），
    不阻塞上传流程——向量检索本来就不需要 DeepSeek。"""
    api_key = api_key_store.get_key()
    if not api_key:
        logger.warning("没有配置 DeepSeek API key，跳过实体/关系抽取（%d 块）", len(chunks))
        if progress:
            try:
                progress(len(chunks), len(chunks))
            except Exception:
                pass
        return [], []

    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model="deepseek-chat",
        api_key=api_key,
        base_url=_DEEPSEEK_BASE,
        timeout=60,
        max_retries=1,
    )
    entities, relations = [], []
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        if progress:
            try:
                progress(i, total)
            except Exception:
                pass
        try:
            response = llm.invoke(_EXTRACT_PROMPT + chunk.page_content)
            raw = response.content.replace("```json", "").replace("```", "").strip()
            data = json.loads(raw)
            entities.extend(data.get("entities", []) or [])
            relations.extend(data.get("relations", []) or [])
        except Exception as e:
            logger.warning("第 %d/%d 块抽取失败：%s", i + 1, total, e)
    if progress:
        try:
            progress(total, total)
        except Exception:
            pass
    return entities, relations


def _faiss_add(faiss_dir, chunks):
    """把 chunks 追加进已有 FAISS 索引，没有就新建。"""
    from langchain_community.vectorstores import FAISS

    emb = get_embeddings()
    idx_file = os.path.join(faiss_dir, "index.faiss")
    if os.path.isfile(idx_file):
        vs = FAISS.load_local(faiss_dir, emb, allow_dangerous_deserialization=True)
        vs.add_documents(chunks)
    else:
        vs = FAISS.from_documents(chunks, emb)
    vs.save_local(faiss_dir)


def _faiss_rebuild(faiss_dir, all_chunks):
    """整体重建 FAISS（删文档后用：FAISS 删不掉单条向量，只能拿剩下的全量重建）。"""
    from langchain_community.vectorstores import FAISS

    if os.path.isdir(faiss_dir):
        shutil.rmtree(faiss_dir)
    if not all_chunks:
        return  # 空库：不建索引，workflow 检测不到 index.faiss 会走空结果
    emb = get_embeddings()
    vs = FAISS.from_documents(all_chunks, emb)
    vs.save_local(faiss_dir)


def _rechunk_all_docs(kb_id):
    docs_dir = kb_docs_dir(kb_id)
    all_chunks = []
    if os.path.isdir(docs_dir):
        for fn in sorted(os.listdir(docs_dir)):
            p = os.path.join(docs_dir, fn)
            if not os.path.isfile(p):
                continue
            try:
                all_chunks.extend(_chunk_file(p))
            except Exception as e:
                logger.warning("重建索引：跳过 %s（%s）", fn, e)
    return all_chunks


# ==================== 对外：查询 ====================

def list_kbs():
    reg = _load_registry()
    out = []
    for kb in reg.get("kbs", []):
        out.append({
            "id": kb["id"],
            "name": kb["name"],
            "created_at": kb.get("created_at", ""),
            "doc_count": len(kb.get("docs", [])),
            "docs": kb.get("docs", []),
        })
    return out


def get_kb(kb_id):
    for kb in _load_registry().get("kbs", []):
        if kb["id"] == kb_id:
            return kb
    return None


def resolve_kb_id(kb_id):
    """None / 不存在的 id 一律回退到默认库。"""
    if kb_id and get_kb(kb_id):
        return kb_id
    return DEFAULT_KB_ID


def kb_stats(kb_id):
    """entities / relations / documents / top_types，供侧边栏和管理页用。"""
    kb = get_kb(kb_id)
    db_path = kb_db_path(kb_id)
    entities = relations = 0
    top_types = []
    if os.path.isfile(db_path):
        conn = sqlite3.connect(db_path)
        conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
        try:
            entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            relations = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
            top_types = conn.execute(
                "SELECT type, COUNT(*) c FROM entities GROUP BY type ORDER BY c DESC LIMIT 8"
            ).fetchall()
        except Exception:
            logger.exception("kb_stats 查询失败：%s", kb_id)
        finally:
            conn.close()
    return {
        "entities": entities,
        "relations": relations,
        "documents": len(kb.get("docs", [])) if kb else 0,
        "top_types": [{"type": t or "未知", "count": c} for t, c in top_types],
    }


# ==================== 对外：建库 / 增删文档 ====================

def create_kb(name):
    with _lock:
        name = (name or "").strip()
        if not name:
            raise ValueError("知识库名称不能为空")
        if len(name) > 40:
            raise ValueError("知识库名称太长（最多 40 字）")
        reg = _load_registry()
        if any(kb["name"] == name for kb in reg["kbs"]):
            raise ValueError("已存在同名知识库")
        kb_id = _new_kb_id(name, {kb["id"] for kb in reg["kbs"]})
        os.makedirs(kb_docs_dir(kb_id), exist_ok=True)
        _init_db(kb_db_path(kb_id)).close()
        entry = {"id": kb_id, "name": name, "created_at": _now(), "docs": []}
        reg["kbs"].append(entry)
        _save_registry(reg)
        logger.info("新建知识库 %s（%s）", name, kb_id)
        return entry


def _register_doc(kb_id, filename, chunks, entities, relations):
    reg = _load_registry()
    for kb in reg["kbs"]:
        if kb["id"] == kb_id:
            kb.setdefault("docs", [])
            kb["docs"] = [d for d in kb["docs"] if d["filename"] != filename]
            kb["docs"].append({
                "filename": filename,
                "uploaded_at": _now(),
                "chunks": len(chunks),
                "entities": len(entities),
                "relations": len(relations),
            })
    _save_registry(reg)


def add_document(kb_id, filename, data, progress=None, on_phase=None):
    """保存文件 -> 切块 -> 抽实体关系存该库 SQLite -> 向量化进该库 FAISS。
    同名文档视为替换（先清旧记录 + 重建 FAISS）。返回统计 dict。"""
    def _phase(p):
        if on_phase:
            try:
                on_phase(p)
            except Exception:
                pass

    with _lock:
        if not get_kb(kb_id):
            raise ValueError("知识库不存在")
        filename = _safe_filename(filename)
        if not filename.lower().endswith(SUPPORTED_EXT):
            raise ValueError("只支持 .md / .pdf 文件")

        os.makedirs(kb_docs_dir(kb_id), exist_ok=True)
        dest = os.path.join(kb_docs_dir(kb_id), filename)
        replacing = os.path.isfile(dest) or any(
            d["filename"] == filename for d in get_kb(kb_id).get("docs", [])
        )
        with open(dest, "wb") as f:
            f.write(data)

        try:
            chunks = _chunk_file(dest)
        except Exception as e:
            if not replacing and os.path.isfile(dest):
                os.remove(dest)
            raise ValueError(f"解析文件失败：{e}")
        if not chunks:
            if not replacing and os.path.isfile(dest):
                os.remove(dest)
            raise ValueError("没从文件里提取到文本（空文件？扫描件 / 图片型 PDF？）")

        conn = _init_db(kb_db_path(kb_id))
        try:
            if replacing:
                conn.execute("DELETE FROM entities WHERE source_file=?", (filename,))
                conn.execute("DELETE FROM relations WHERE source_file=?", (filename,))
                conn.commit()
            _phase("提取实体关系")
            entities, relations = _extract(chunks, progress=progress)
            _insert_extractions(conn, entities, relations, filename)
        finally:
            conn.close()

        _phase("建立向量索引")
        if replacing:
            _faiss_rebuild(kb_faiss_dir(kb_id), _rechunk_all_docs(kb_id))
        else:
            _faiss_add(kb_faiss_dir(kb_id), chunks)

        _register_doc(kb_id, filename, chunks, entities, relations)
        logger.info(
            "知识库 %s 收录 %s：%d 块 / %d 实体 / %d 关系%s",
            kb_id, filename, len(chunks), len(entities), len(relations),
            "（替换）" if replacing else "",
        )
        return {
            "filename": filename,
            "chunks": len(chunks),
            "entities": len(entities),
            "relations": len(relations),
            "replaced": replacing,
        }


def delete_document(kb_id, filename):
    """从库里删一个文档：清 SQLite 里该 source_file 的记录 + 删原始文件 + 用剩余文档重建 FAISS。
    删空了也保留空库。"""
    with _lock:
        if not get_kb(kb_id):
            raise ValueError("知识库不存在")
        filename = _safe_filename(filename)

        conn = _init_db(kb_db_path(kb_id))
        try:
            conn.execute("DELETE FROM entities WHERE source_file=?", (filename,))
            conn.execute("DELETE FROM relations WHERE source_file=?", (filename,))
            conn.commit()
        finally:
            conn.close()

        p = os.path.join(kb_docs_dir(kb_id), filename)
        if os.path.isfile(p):
            os.remove(p)

        _faiss_rebuild(kb_faiss_dir(kb_id), _rechunk_all_docs(kb_id))

        reg = _load_registry()
        for kb in reg["kbs"]:
            if kb["id"] == kb_id:
                kb["docs"] = [d for d in kb.get("docs", []) if d["filename"] != filename]
        _save_registry(reg)
        logger.info("知识库 %s 删除文档 %s，已重建索引", kb_id, filename)


def rebuild_kb_from_folder(kb_id, folder, name=None, verbose=False):
    """把一个文件夹里的所有 .md/.pdf 灌进指定知识库（先清空该库）。graph_builder.py 用。"""
    with _lock:
        reg = _load_registry()
        entry = next((kb for kb in reg["kbs"] if kb["id"] == kb_id), None)
        if entry is None:
            entry = {"id": kb_id, "name": name or kb_id, "created_at": _now(), "docs": []}
            reg["kbs"].insert(0, entry)
        elif name:
            entry["name"] = name
        entry["docs"] = []
        _save_registry(reg)

        # 清空
        d = kb_dir(kb_id)
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(kb_docs_dir(kb_id), exist_ok=True)

        files = sorted(
            f for f in os.listdir(folder)
            if f.lower().endswith(SUPPORTED_EXT) and os.path.isfile(os.path.join(folder, f))
        )
        if verbose:
            print(f"共 {len(files)} 个文件：{files}")

        conn = _init_db(kb_db_path(kb_id))
        all_chunks = []
        try:
            for fi, fn in enumerate(files):
                shutil.copy2(os.path.join(folder, fn), os.path.join(kb_docs_dir(kb_id), fn))
                try:
                    chunks = _chunk_file(os.path.join(kb_docs_dir(kb_id), fn))
                except Exception as e:
                    print(f"  跳过 {fn}：{e}")
                    continue
                if not chunks:
                    print(f"  跳过 {fn}：没提取到文本")
                    continue
                all_chunks.extend(chunks)

                def _p(done, total, _fn=fn, _fi=fi):
                    if verbose and done % 10 == 0:
                        print(f"  [{_fi + 1}/{len(files)}] {_fn}: 抽取 {done}/{total}")

                entities, relations = _extract(chunks, progress=_p)
                _insert_extractions(conn, entities, relations, fn)
                _register_doc(kb_id, fn, chunks, entities, relations)
                if verbose:
                    print(f"  {fn}: {len(chunks)} 块 / {len(entities)} 实体 / {len(relations)} 关系")
        finally:
            conn.close()

        if verbose:
            print(f"向量化 {len(all_chunks)} 块...")
        _faiss_rebuild(kb_faiss_dir(kb_id), all_chunks)
        if verbose:
            print("完成。", kb_stats(kb_id))


# ==================== 首次迁移 ====================

def ensure_default_kb():
    """把旧的扁平 knowledge_graph.db / faiss_index/ + 源 md 目录迁移成默认知识库。
    幂等：已迁移过就直接返回。不删除旧文件（留作备份）。"""
    with _lock:
        os.makedirs(KB_ROOT, exist_ok=True)
        reg = _load_registry()
        if any(kb["id"] == DEFAULT_KB_ID for kb in reg["kbs"]):
            return

        os.makedirs(kb_docs_dir(DEFAULT_KB_ID), exist_ok=True)

        if os.path.isfile(_LEGACY_DB) and not os.path.isfile(kb_db_path(DEFAULT_KB_ID)):
            shutil.copy2(_LEGACY_DB, kb_db_path(DEFAULT_KB_ID))
        _init_db(kb_db_path(DEFAULT_KB_ID)).close()  # 补 source_file 列

        if os.path.isdir(_LEGACY_FAISS) and not os.path.isdir(kb_faiss_dir(DEFAULT_KB_ID)):
            shutil.copytree(_LEGACY_FAISS, kb_faiss_dir(DEFAULT_KB_ID))

        docs = []
        if os.path.isdir(_LEGACY_DOCS):
            for fn in sorted(os.listdir(_LEGACY_DOCS)):
                if not fn.lower().endswith(SUPPORTED_EXT):
                    continue
                src = os.path.join(_LEGACY_DOCS, fn)
                if not os.path.isfile(src):
                    continue
                shutil.copy2(src, os.path.join(kb_docs_dir(DEFAULT_KB_ID), fn))
                docs.append({"filename": fn, "uploaded_at": _now(), "chunks": None})

        reg["kbs"].insert(0, {
            "id": DEFAULT_KB_ID,
            "name": DEFAULT_KB_NAME,
            "created_at": _now(),
            "docs": docs,
            "legacy": True,  # 旧库：entities/relations 没有 source_file，按文档删只能删掉向量
        })
        _save_registry(reg)
        logger.info("已迁移旧知识库 -> %s（%d 篇文档）", DEFAULT_KB_ID, len(docs))


# ==================== 后台任务（上传较慢，前端轮询） ====================

def _prune_jobs():
    if len(_jobs) <= _JOBS_MAX:
        return
    done = [k for k, v in _jobs.items() if v.get("status") in ("done", "error")]
    done.sort(key=lambda k: _jobs[k].get("finished_at", 0))
    for k in done[: len(_jobs) - _JOBS_MAX]:
        _jobs.pop(k, None)


def add_document_async(kb_id, filename, data):
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {
        "status": "running", "phase": "解析文件", "done": 0, "total": 0,
        "kb_id": kb_id, "filename": filename, "started_at": time.time(),
    }
    _prune_jobs()

    def work():
        job = _jobs[job_id]
        try:
            def prog(done, total):
                job.update(done=done, total=total)

            def phase(p):
                job.update(phase=p)

            result = add_document(kb_id, filename, data, progress=prog, on_phase=phase)
            job.update(status="done", phase="完成", result=result, finished_at=time.time())
        except Exception as e:
            logger.exception("后台建库失败 job=%s", job_id)
            job.update(status="error", error=str(e), finished_at=time.time())

    threading.Thread(target=work, daemon=True, name=f"ingest-{job_id[:8]}").start()
    return job_id


def get_job(job_id):
    return _jobs.get(job_id)
