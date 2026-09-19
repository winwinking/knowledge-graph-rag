"""Knowledge Graph RAG —— MCP Server（只做检索，不生成回答）。

给 Claude Desktop / Cursor 等支持 MCP 协议的客户端，把本项目的知识库检索能力
（FAISS 向量检索 + SQLite 知识图谱检索）暴露成工具。生成回答这一步完全交给
连接的 AI 客户端自己做——这个进程不调用 DeepSeek，也不 import workflow.py /
LangGraph，只依赖 kb_store.py 里已有的路径 / embedding 工具函数。

复用的现有逻辑（写这个文件之前读过 kb_store.py 确认过）：
    - knowledge_bases/<kb_id>/graph.db        SQLite：entities(name,type,source_file) /
                                               relations(entity1,relation,entity2,source_file)
    - knowledge_bases/<kb_id>/faiss_index/     FAISS 索引目录，FAISS.load_local() 加载
    - kb_store.get_embeddings()                all-MiniLM-L6-v2（HuggingFaceEmbeddings），
                                                跟 app.py / workflow.py 共用同一份模型
    - kb_store.list_kbs() / get_kb() / kb_faiss_dir() / kb_db_path()

注意：这个文件要用单独的 mcp_venv（Python 3.11+）运行，因为 fastmcp / mcp SDK
不支持项目主环境的 Python 3.9。运行方式和依赖装法见 README_MCP.md。

启动：
    mcp_venv\\Scripts\\python.exe mcp_server.py       # stdio，Claude Desktop 会这么起
调试：
    mcp_venv\\Scripts\\fastmcp.exe dev mcp_server.py      # MCP Inspector（网页界面，需要 Node/npx）
    mcp_venv\\Scripts\\fastmcp.exe inspect mcp_server.py  # 纯命令行，打印工具列表摘要，不需要 Node
"""

import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

# mcp_server.py 和 kb_store.py 同级，直接 import 就行；kb_store 顶层只 import
# 标准库，langchain 系列都是函数内部 lazy import，所以这个 import 很轻量。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb_store  # noqa: E402

mcp = FastMCP(
    "knowledge-graph-rag",
    instructions=(
        "这是一个知识库检索服务，底层是 FAISS 向量索引 + SQLite 知识图谱，"
        "支持多个独立知识库。它只负责检索，不生成回答——请你（AI 客户端）先调用 "
        "list_knowledge_bases 看有哪些库，再根据用户问题选择合适的工具检索，"
        "最后基于检索到的内容自己组织语言回答用户。"
    ),
)

# ==================== 内部：FAISS / SQLite 访问（自成一套，不依赖 workflow.py）====================

# 按 kb_id 缓存 FAISS store：(store, index.faiss 的 mtime)。
# 索引被 app.py 那边的上传/删除接口重建后，mtime 会变，这里会自动重新加载，不用重启本进程。
_vector_stores: Dict[str, tuple] = {}


def _get_vector_store(kb_id: str):
    """加载（并缓存）某个知识库的 FAISS store；库还没有索引时返回 None。"""
    from langchain_community.vectorstores import FAISS

    faiss_dir = kb_store.kb_faiss_dir(kb_id)
    idx_file = os.path.join(faiss_dir, "index.faiss")
    if not os.path.isfile(idx_file):
        return None
    mtime = os.path.getmtime(idx_file)
    cached = _vector_stores.get(kb_id)
    if cached and cached[1] == mtime:
        return cached[0]
    store = FAISS.load_local(faiss_dir, kb_store.get_embeddings(), allow_dangerous_deserialization=True)
    _vector_stores[kb_id] = (store, mtime)
    return store


def _db_connect(kb_id: str) -> sqlite3.Connection:
    conn = sqlite3.connect(kb_store.kb_db_path(kb_id))
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    return conn


def _require_kb(kb_id: str) -> str:
    """校验 kb_id：MCP 工具面向外部客户端，无效 id 应该报错让客户端改用
    list_knowledge_bases() 的结果重试，而不是像 workflow.py 那样静默回退默认库。"""
    kb_id = (kb_id or "").strip()
    if not kb_id:
        raise ValueError("kb_id 不能为空，请先调用 list_knowledge_bases() 获取可用的知识库 id")
    if not kb_store.get_kb(kb_id):
        available = ", ".join(k["id"] for k in kb_store.list_kbs()) or "（当前没有任何知识库）"
        raise ValueError(f"知识库 {kb_id!r} 不存在。可用的 kb_id：{available}")
    return kb_id


_MAX_TOP_K = 20
_MAX_GRAPH_RESULTS = 50


def _search_knowledge_impl(query: str, kb_id: str, top_k: int) -> List[Dict[str, Any]]:
    kb_id = _require_kb(kb_id)
    query = (query or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    top_k = max(1, min(int(top_k or 5), _MAX_TOP_K))

    store = _get_vector_store(kb_id)
    if store is None:
        return []
    docs = store.similarity_search(query, k=top_k)
    return [{"content": d.page_content, "metadata": d.metadata} for d in docs]


def _query_graph_impl(entities: List[str], kb_id: str) -> List[Dict[str, str]]:
    kb_id = _require_kb(kb_id)
    ents = [str(e).strip() for e in (entities or []) if str(e).strip()]
    if not ents:
        raise ValueError("entities 不能为空，至少传一个从用户问题里识别出的关键实体名")

    if not os.path.isfile(kb_store.kb_db_path(kb_id)):
        return []

    conn = _db_connect(kb_id)
    try:
        cur = conn.cursor()
        seen = set()
        out: List[Dict[str, str]] = []
        for ent in ents:
            cur.execute(
                "SELECT entity1, relation, entity2 FROM relations "
                "WHERE entity1 LIKE ? OR entity2 LIKE ?",
                (f"%{ent}%", f"%{ent}%"),
            )
            for e1, rel, e2 in cur.fetchall():
                key = (e1, rel, e2)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"entity1": e1, "relation": rel, "entity2": e2})
                if len(out) >= _MAX_GRAPH_RESULTS:
                    return out
        return out
    finally:
        conn.close()


# ==================== 对外：MCP 工具 ====================

@mcp.tool()
def list_knowledge_bases() -> List[Dict[str, Any]]:
    """列出当前所有可用的知识库。

    适用场景：每次对话开始、或用户没指定要查哪个知识库时，先调用这个工具看看
    有哪些知识库可选，再把用户选中（或猜测最相关）的 kb_id 传给
    search_knowledge / query_graph / search_all。

    Returns:
        一个列表，每项是一个知识库的概况：
        - kb_id (str): 知识库 id，后面调用其它工具时要传这个
        - name (str): 知识库的显示名称（人类可读，比如「AI面试题」）
        - doc_count (int): 库里有多少篇文档
        - created_at (str): 创建时间
    """
    return [
        {
            "kb_id": kb["id"],
            "name": kb["name"],
            "doc_count": kb["doc_count"],
            "created_at": kb.get("created_at", ""),
        }
        for kb in kb_store.list_kbs()
    ]


@mcp.tool()
def search_knowledge(query: str, kb_id: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """在指定知识库里做向量语义检索，返回最相关的原文片段。

    适用场景：概念解释类问题（"什么是 XXX"、"解释一下 XXX 的原理"）——用这个工具
    去知识库原文里找语义相关的段落。如果问题同时涉及"XXX 和 YYY 是什么关系"这种
    实体间关系，应该额外调用 query_graph（或者直接用 search_all 一次性拿两种结果）。

    Args:
        query: 用户的问题或检索关键词，直接传自然语言就行，不需要预处理。
        kb_id: 要查询的知识库 id，从 list_knowledge_bases() 的结果里选一个。
        top_k: 返回最相关的多少条片段，默认 5，最多 20。

    Returns:
        一个列表，每项：
        - content (str): 检索到的原文片段
        - metadata (dict): 片段的元信息，包含 source_file（来源文件名）、
          header（在原文档里的标题路径，比如 "RAG 技术 > 1. RAG 基础 > 1.1 定义与原理"）
    """
    return _search_knowledge_impl(query, kb_id, top_k)


@mcp.tool()
def query_graph(entities: List[str], kb_id: str) -> List[Dict[str, str]]:
    """在指定知识库的知识图谱（SQLite 三元组）里查询实体之间的关联关系。

    适用场景：适合查询"实体之间的关系"这类问题，比如"A 和 B 是什么关系""A 用到了
    哪些技术"。entities 应该是你（AI 客户端）从用户问题里提取出的关键实体名——
    这个工具自己不做实体提取、不调用任何外部 LLM，完全依赖调用方传入的实体列表。
    传入的实体名做的是模糊匹配（LIKE %entity%），所以不需要跟原文完全一致，
    大致的名字/缩写也能匹配上。

    Args:
        entities: 从用户问题里识别出的关键实体名列表（比如 ["RAG", "向量数据库"]）。
            之所以是列表而不是单个实体，是因为一个问题往往涉及多个实体，
            要把它们都传进来才能查到它们之间、以及各自相关的关系。
        kb_id: 要查询的知识库 id，从 list_knowledge_bases() 的结果里选一个。

    Returns:
        一个列表，每项是一条三元组关系：
        - entity1 (str), relation (str), entity2 (str)
        最多返回 50 条，按查询到的顺序去重。
    """
    return _query_graph_impl(entities, kb_id)


@mcp.tool()
def search_all(query: str, entities: List[str], kb_id: str, top_k: int = 5) -> Dict[str, Any]:
    """同时做向量检索 + 知识图谱查询，一次性拿到两种检索结果，适合大多数问题。

    适用场景：不确定问题是"概念解释"还是"实体关系"、或者两者都要时，直接用这个
    工具最省事——它会同时跑 search_knowledge 和 query_graph 并把结果合并返回。

    **重要**：entities 参数需要你（AI 客户端）自己先从用户的问题里识别出涉及的
    关键实体（人名、技术名词、产品名等），再作为参数传进来——这个 MCP server
    本身不依赖任何外部 LLM 做实体提取，识别实体的工作必须由调用方完成。如果
    问题里确实没有明确的实体（纯概念解释类问题），entities 传空列表 [] 即可，
    这时只会返回向量检索的结果。

    Args:
        query: 用户的问题，用于向量检索；传空字符串则跳过向量检索。
        entities: 从用户问题里提取出的关键实体名列表；传空列表则跳过图谱查询。
        kb_id: 要查询的知识库 id，从 list_knowledge_bases() 的结果里选一个。
        top_k: 向量检索返回的片段数，默认 5，最多 20。

    Returns:
        {
          "vector_results": [...同 search_knowledge 的返回...],
          "graph_results":  [...同 query_graph 的返回...]
        }
    """
    kb_id = _require_kb(kb_id)
    query = (query or "").strip()
    vector_results = _search_knowledge_impl(query, kb_id, top_k) if query else []
    graph_results = _query_graph_impl(entities, kb_id) if entities else []
    return {"vector_results": vector_results, "graph_results": graph_results}


if __name__ == "__main__":
    # stdio 传输：Claude Desktop / Cursor 都是把这个进程当子进程拉起，走 stdin/stdout 通信。
    mcp.run(transport="stdio")
