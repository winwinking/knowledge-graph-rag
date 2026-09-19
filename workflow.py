from skills import load_skill, get_active_skill
load_skill("ai_tech_assistant")
from langchain_openai import ChatOpenAI
from langchain_community.vectorstores import FAISS
from langgraph.graph import StateGraph, END
from typing import TypedDict, List, Optional
import sqlite3
import time
import logging
import os
import json

import api_key_store
import kb_store

logger = logging.getLogger("kg_rag.workflow")

# 相对本文件的路径，保证从任何工作目录启动 Flask 都能找到资源
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# key 可能在进程启动后才通过网页设置提交，所以 llm 客户端延迟创建，
# 并按 key 是否变化决定要不要重新 new 一个（大多数请求直接命中缓存）。
# timeout：单次 DeepSeek 调用最多等 60s，否则一直挂着会拖垮整个请求，
# 前端/代理超时后拿到空响应就是 "Unexpected end of JSON input"。
# max_retries：网络抖动重试 1 次。
_llm_cache = {"key": None, "llm": None, "llm_classify": None}


def _get_llm():
    """返回 (llm, llm_classify)；没配置 key 时抛 RuntimeError，调用方已有 try/except 兜底。"""
    key = api_key_store.get_key()
    if not key:
        raise RuntimeError("DeepSeek API key 未配置，请在网页右下角「设置」里输入")
    if _llm_cache["key"] != key:
        new_llm = ChatOpenAI(
            model="deepseek-chat",
            api_key=key,
            base_url="https://api.deepseek.com",
            timeout=60,
            max_retries=1,
        )
        _llm_cache["key"] = key
        _llm_cache["llm"] = new_llm
        _llm_cache["llm_classify"] = new_llm.bind_tools(_CLASSIFY_TOOLS)
    return _llm_cache["llm"], _llm_cache["llm_classify"]

# classify 节点用 function calling 一次搞定「判断意图 + 抽实体」：
# 三个 tool 对应三条检索路径，模型选哪个 tool 就是哪个 intent，
# retrieve_graph / retrieve_both 的 entities 参数直接当作抽好的关键实体。
_CLASSIFY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "retrieve_vector",
            "description": (
                "概念解释类问题，只需要在文档原文里做语义检索即可回答，"
                "不涉及多个实体之间的关系（如「什么是CoT」「解释一下Prompt Engineering」）。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_graph",
            "description": (
                "实体关系类问题，问的是两个或多个事物之间的关系"
                "（如「RAG用了什么技术」「LangChain和向量搜索是什么关系」）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "问题中涉及的关键实体名称列表",
                    }
                },
                "required": ["entities"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_both",
            "description": (
                "混合类问题：既需要概念解释，又需要理清实体之间的关系。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "问题中涉及的关键实体名称列表",
                    }
                },
                "required": ["entities"],
            },
        },
    },
]

# tool 名 -> intent（保持 semantic/entity/hybrid，route 函数无需改动）
_TOOL_TO_INTENT = {
    "retrieve_vector": "semantic",
    "retrieve_graph": "entity",
    "retrieve_both": "hybrid",
}

def _db_connect(kb_id):
    """统一的 SQLite 连接：容忍数据库里可能存在的非法 UTF-8 字节，
    避免 jsonify 时抛 UnicodeEncodeError 导致响应体为空。"""
    conn = sqlite3.connect(kb_store.kb_db_path(kb_id))
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    return conn


# 每个知识库的 FAISS 索引缓存一份：(store, index.faiss 的 mtime)。
# 上传/删文档会改写索引文件，mtime 变了就重新加载，不用重启后端。
_vector_stores = {}


def get_vector_store(kb_id):
    """返回该知识库的 FAISS store；库还没有任何文档（没有 index.faiss）时返回 None。"""
    faiss_dir = kb_store.kb_faiss_dir(kb_id)
    idx_file = os.path.join(faiss_dir, "index.faiss")
    if not os.path.isfile(idx_file):
        return None
    mtime = os.path.getmtime(idx_file)
    cached = _vector_stores.get(kb_id)
    if cached and cached[1] == mtime:
        return cached[0]
    store = FAISS.load_local(
        faiss_dir, kb_store.get_embeddings(), allow_dangerous_deserialization=True
    )
    _vector_stores[kb_id] = (store, mtime)
    return store


# 定义状态：整个流程中传递的数据
# 除了原来的字符串字段，额外保留结构化结果供后端返回
class State(TypedDict):
    query: str
    kb_id: str                   # 查哪个知识库（空/无效时回退默认库）
    history: List[dict]           # 最近对话 [{"role","content"}]，供 generate 理解指代
    intent: str
    entities: List[str]          # classify 通过 function calling 抽出的关键实体，供图谱检索用
    vector_results: str          # 拼接后的文本，喂给生成节点
    graph_results: str           # 拼接后的文本，喂给生成节点
    vector_chunks: List[dict]     # 向量检索的原始 chunk 列表
    graph_relations: List[dict]   # 图谱检索的原始关系三元组列表
    graph_nodes: List[dict]       # 图谱检索涉及的实体（含 type、是否核心实体）
    answer: str
    suggestions: List[str]        # 基于本次回答生成的 2-3 个推荐追问
    verification_passed: Optional[bool]  # 幻觉检测结果，None=还没验证
    retry_count: int              # 因验证不通过已重新生成的次数
    hallucinated_parts: List[str]  # verify 标记为"超出原文范围"的句子


# 每个知识库的「实体名 -> type」映射缓存：(dict, graph.db 的 mtime)
_entity_type_caches = {}


def get_entity_types(kb_id) -> dict:
    db_path = kb_store.kb_db_path(kb_id)
    mtime = os.path.getmtime(db_path) if os.path.isfile(db_path) else 0
    cached = _entity_type_caches.get(kb_id)
    if cached and cached[1] == mtime:
        return cached[0]
    types = {}
    if os.path.isfile(db_path):
        conn = _db_connect(kb_id)
        try:
            for name, etype in conn.execute("SELECT name, type FROM entities"):
                if name:
                    types[name.strip().lower()] = etype or "未知"
        finally:
            conn.close()
    _entity_type_caches[kb_id] = (types, mtime)
    return types


def _lookup_type(name: str, kb_id: str) -> str:
    types = get_entity_types(kb_id)
    key = name.strip().lower()
    if key in types:
        return types[key]
    # 关系表里的实体名可能和 entities 表不完全一致，做一次包含匹配兜底
    for k, v in types.items():
        if key in k or k in key:
            return v
    return "未知"


# 节点1：分类器——用 function calling 一次完成「判断意图 + 抽取实体」
def classify(state: State) -> State:
    t0 = time.time()
    prompt = (
        "根据用户的问题，选择最合适的一条检索路径，并调用对应的工具：\n"
        "- retrieve_vector：概念解释类，只需搜原文；\n"
        "- retrieve_graph：实体关系类，问两个及以上事物之间的关系，需要给出关键实体；\n"
        "- retrieve_both：两者都需要，需要给出关键实体。\n"
        "只调用一个工具。\n\n"
        "问题：" + state["query"]
    )

    intent = "hybrid"          # fallback：模型没选任何 tool 时按 hybrid 处理
    entities: List[str] = []
    try:
        _, llm_classify = _get_llm()
        response = llm_classify.invoke(prompt)
        tool_calls = getattr(response, "tool_calls", None) or []
        if tool_calls:
            call = tool_calls[0]
            name = call.get("name")
            args = call.get("args") or {}
            if name in _TOOL_TO_INTENT:
                intent = _TOOL_TO_INTENT[name]
                raw = args.get("entities")
                if isinstance(raw, list):
                    entities = [str(e).strip() for e in raw if str(e).strip()]
            else:
                logger.warning("classify 返回未知 tool %r，回退 hybrid", name)
        else:
            logger.warning(
                "classify 没有返回 tool call（content=%r），回退 hybrid",
                (getattr(response, "content", "") or "")[:80],
            )
    except Exception:
        logger.exception("classify 节点失败，回退 hybrid")
        intent, entities = "hybrid", []

    logger.info(
        "节点 classify 完成：intent=%s，entities=%s (%.1fs)",
        intent, entities, time.time() - t0,
    )
    return {"intent": intent, "entities": entities}


def _kb(state) -> str:
    return kb_store.resolve_kb_id(state.get("kb_id"))


def _expand_query_for_vector(query: str) -> str:
    """把用户问题改写成关键词更丰富的检索 query，专给向量检索用。
    动机：同一主题下如果有很多具体变体/高级用法的小节（比如 "RAG" 底下一堆
    Self-RAG / Corrective RAG / Agentic RAG），原始短 query 语义太单薄，
    embedding 模型区分不出"基础定义"和这些变体，检索排名会被淹没。
    改写失败就原样返回原 query，不影响检索照常进行。"""
    prompt = (
        "把下面这个用户问题改写成更适合向量检索的查询：写出问题涉及的核心概念、"
        "全称/缩写、近义词和相关术语，用空格隔开拼成一行文本。目的是帮基于语义相似度的"
        "检索找到「基础定义/概念解释」类的内容，而不是被同一主题下更具体的变体、"
        "高级用法淹没。只输出改写后的查询本身，不要解释、不要标点、不要回答问题。\n\n"
        f"问题：{query}"
    )
    try:
        llm, _ = _get_llm()
        response = llm.invoke(prompt)
        expanded = (response.content or "").strip().strip('"').strip()
        return expanded if expanded else query
    except Exception:
        logger.exception("retrieve_vector 查询改写失败，回退用原始 query")
        return query


# 节点2：向量检索
def retrieve_vector(state: State) -> State:
    t0 = time.time()
    try:
        vector_store = get_vector_store(_kb(state))
        if vector_store is None:
            logger.info("retrieve_vector：知识库 %s 还没有向量索引", _kb(state))
            return {"vector_results": "", "vector_chunks": []}
        search_query = _expand_query_for_vector(state["query"])
        logger.info("retrieve_vector 查询改写：%r -> %r", state["query"], search_query)
        skill = get_active_skill()
        k = skill["get_k_value"](state["query"]) if skill else 5
        docs = vector_store.similarity_search(search_query, k=k)
        print("【检索结果】", [(doc.page_content[:100], doc.metadata) for doc in docs], flush=True)
        chunks = [
            {"content": doc.page_content, "metadata": doc.metadata}
            for doc in docs
        ]
        text = "\n".join([doc.page_content[:300] for doc in docs])
        logger.info(
            "节点 retrieve_vector 完成：%d 个 chunk (%.1fs)", len(chunks), time.time() - t0
        )
        return {"vector_results": text, "vector_chunks": chunks}
    except Exception:
        logger.exception("retrieve_vector 节点失败")
        return {"vector_results": "", "vector_chunks": []}


# 节点3：图谱检索——实体由 classify 节点（function calling）抽好，这里直接读 State
def retrieve_graph(state: State) -> State:
    t0 = time.time()
    entities = [str(e).strip() for e in (state.get("entities") or []) if str(e).strip()]

    kb_id = _kb(state)
    try:
        conn = _db_connect(kb_id)
        cursor = conn.cursor()
    except Exception:
        logger.exception("retrieve_graph 连接数据库失败")
        return {"graph_results": "", "graph_relations": [], "graph_nodes": []}
    relations = []
    try:
        seen = set()
        for entity in entities:
            cursor.execute(
                "SELECT entity1, relation, entity2 FROM relations "
                "WHERE entity1 LIKE ? OR entity2 LIKE ?",
                (f"%{entity}%", f"%{entity}%"))
            for r in cursor.fetchall():
                key = (r[0], r[1], r[2])
                if key in seen:
                    continue
                seen.add(key)
                relations.append(
                    {"entity1": r[0], "relation": r[1], "entity2": r[2]})
    except Exception:
        logger.exception("retrieve_graph 查询关系失败")
    finally:
        conn.close()

    # 从关系里汇总涉及的实体，补上 type，并标记哪些是本次问题的核心实体
    query_terms = [str(e).strip().lower() for e in entities if str(e).strip()]

    def _is_core(name: str) -> bool:
        low = name.strip().lower()
        return any(t in low or low in t for t in query_terms)

    node_names = []
    for r in relations:
        for name in (r["entity1"], r["entity2"]):
            if name not in node_names:
                node_names.append(name)

    try:
        graph_nodes = [
            {"name": name, "type": _lookup_type(name, kb_id), "core": _is_core(name)}
            for name in node_names
        ]
    except Exception:
        logger.exception("retrieve_graph 组装节点失败")
        graph_nodes = [{"name": n, "type": "未知", "core": _is_core(n)} for n in node_names]

    text = "\n".join(
        f"{r['entity1']} → {r['relation']} → {r['entity2']}" for r in relations
    )
    logger.info(
        "节点 retrieve_graph 完成：实体词=%s，关系 %d 条，节点 %d 个 (%.1fs)",
        entities, len(relations), len(graph_nodes), time.time() - t0,
    )
    return {
        "graph_results": text,
        "graph_relations": relations,
        "graph_nodes": graph_nodes,
    }


_SUGGEST_SEP = "===追问==="


def _history_block(history) -> str:
    """把最近对话拼成一小段，供模型理解「它」「上面那个」之类的指代。"""
    msgs = [m for m in (history or []) if m.get("content")]
    if not msgs:
        return ""
    lines = []
    for m in msgs[-8:]:
        who = "用户" if m.get("role") == "user" else "助手"
        c = str(m["content"]).strip()
        if len(c) > 500:
            c = c[:500] + "…"
        lines.append(f"{who}：{c}")
    return (
        "【最近对话】（仅用于理解这次提问里的指代，回答内容仍以下面的检索结果为准）\n"
        + "\n".join(lines)
        + "\n\n"
    )


_SUGGEST_INSTRUCTION = (
    f'\n\n回答结束后另起一行，单独写一行 "{_SUGGEST_SEP}"，'
    "再列出 2-3 个用户可能想继续追问的相关问题，\n"
    "每个问题独占一行，不要编号、不要符号、不要多余文字。"
)


def _generate_prompt(state: State) -> str:
    query = state["query"]
    is_retry = state.get("verification_passed") is False
    skill = get_active_skill()

    if skill:
        # Skill 已加载：context 把「最近对话」+ 向量/图谱检索结果拼成一段，
        # 交给 skill 自己的 build_generate_prompt 组装（回答规范/术语/引用规则都在 Skill 里）。
        context = (
            f'{_history_block(state.get("history"))}'
            f'【向量检索结果】\n{state.get("vector_results") or "无"}\n\n'
            f'【图谱检索结果】\n{state.get("graph_results") or "无"}'
        )
        prompt = skill["build_generate_prompt"](query, context, is_retry)
        # Skill 的 prompt 不知道「推荐追问」这个前端功能，追加同样的指令，
        # 保证 _split_answer_suggestions 还能正常解析出 suggestions。
        prompt += _SUGGEST_INSTRUCTION
        return prompt

    # Fallback：Skill 没加载，走原来手动拼 prompt 的逻辑
    prompt = f"""{_history_block(state.get("history"))}根据以下信息回答问题。

【向量检索结果】
{state.get("vector_results") or "无"}

【图谱检索结果】
{state.get("graph_results") or "无"}

问题：{query}

请综合以上信息直接回答问题（如果信息不足请说明），不要加"回答如下"之类的前缀。{_SUGGEST_INSTRUCTION}"""

    # 上一轮验证没通过：收紧要求，并点名要避开的内容
    if is_retry:
        parts = [str(p).strip() for p in (state.get("hallucinated_parts") or []) if str(p).strip()]
        joined = "；".join(parts) if parts else "（未指明具体句子）"
        prompt += (
            f"\n\n注意：严格只使用上面提供的检索内容回答，不要补充任何检索内容中没有的信息。"
            f"以下内容被标记为超出原文范围，请避免：{joined}"
        )
    return prompt


def _split_answer_suggestions(raw: str):
    """把模型输出拆成 (回答正文, [推荐追问])。"""
    raw = raw or ""
    if _SUGGEST_SEP in raw:
        body, _, tail = raw.partition(_SUGGEST_SEP)
        answer = body.strip()
        suggestions = []
        for line in tail.splitlines():
            q = line.strip().lstrip("-*·•0123456789.、) 　").strip()
            if len(q) >= 5:
                suggestions.append(q)
        return answer, suggestions[:3]
    return raw.strip(), []


# 节点4：生成回答（顺便让模型给出 2-3 个推荐追问，省一次调用）
def generate(state: State) -> State:
    t0 = time.time()
    # verification_passed 为 False 说明这是验证不通过后的重新生成
    is_retry = state.get("verification_passed") is False
    retry_count = state.get("retry_count", 0) + (1 if is_retry else 0)

    prompt = _generate_prompt(state)
    answer = "抱歉，生成回答时出错了（模型调用失败或超时，或 API key 未配置），请重试。"
    suggestions: List[str] = []
    try:
        llm, _ = _get_llm()
        response = llm.invoke(prompt)
        answer, suggestions = _split_answer_suggestions(response.content)
    except Exception:
        logger.exception("generate 节点 LLM 调用失败")
    logger.info(
        "节点 generate 完成：回答 %d 字，推荐追问 %d 个，retry=%d (%.1fs)",
        len(answer), len(suggestions), retry_count, time.time() - t0,
    )
    return {"answer": answer, "suggestions": suggestions, "retry_count": retry_count}


_HALLU_NOTE = "⚠️ 部分内容可能超出文档原文范围"


def _verify_answer(context: str, answer: str):
    """把检索原文 + 回答发给模型做幻觉检测。
    返回 (passed: bool, hallucinated_parts: List[str])。
    解析不出 JSON 一律按"通过"处理，避免误伤。"""
    prompt = (
        "以下是检索到的原文内容：\n" + (context or "无") +
        "\n\n以下是基于这些内容生成的回答：\n" + (answer or "") +
        "\n\n请判断这个回答里是否有【幻觉】。判断标准要宽松：不要逐句去原文里找对应表述，"
        "回答不需要是原文的摘抄。规则：\n"
        "1. 幻觉只有两种情况才成立：\n"
        "   a) 回答涉及的领域/话题是检索内容完全没有涉及的（答非所问、扯到检索内容之外的主题）；\n"
        "   b) 回答编造了具体的事实性细节——比如检索内容里没有的具体数据/数字、人名、"
        "论文名/书名、时间、机构名等看似确凿实则编造的东西。\n"
        "   只有命中 a 或 b 才判 passed: false，并在 hallucinated_parts 里列出有问题的句子。\n"
        "2. 基于检索内容做的总结、归纳、合理延伸——包括换一种说法、更通俗的转述、把分散信息"
        "整合表达、补充合理的背景解释或逻辑推论——都不算幻觉，判 passed: true。判断时看的是"
        "回答有没有编造具体事实，而不是回答有没有用检索内容里原本没有的词句表达。\n"
        "3. 如果回答的核心意思是「检索内容里没有足够信息回答这个问题」「无法根据现有内容作答」，"
        "这是正确且诚实的行为，不算幻觉，直接判 passed: true，hallucinated_parts 为空。\n"
        "4. 承认信息不足 ≠ 幻觉；不确定的时候优先判 passed: true，只有明确命中规则 1 才判 false。\n"
        "只回答 JSON 格式，不要任何其他文字："
        '{"passed": true/false, "hallucinated_parts": ["有问题的句子1", "有问题的句子2"]}'
    )
    try:
        llm, _ = _get_llm()
        response = llm.invoke(prompt)
        raw = response.content.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        passed = bool(data.get("passed", True))
        parts = data.get("hallucinated_parts", []) or []
        if not isinstance(parts, list):
            parts = []
        return passed, [str(p) for p in parts]
    except Exception:
        logger.exception("verify 解析失败，按通过处理")
        return True, []


# 节点5：验证——检查回答有没有跑出检索原文的范围
def verify(state: State) -> State:
    t0 = time.time()
    answer = state.get("answer", "") or ""
    context = "\n".join(
        s for s in (state.get("vector_results") or "", state.get("graph_results") or "") if s
    ).strip()

    # 没有任何检索原文时没法核对，直接放行
    if not context:
        logger.info("节点 verify 跳过：无检索上下文")
        return {"verification_passed": True, "hallucinated_parts": []}

    passed, parts = _verify_answer(context, answer)
    retry_count = state.get("retry_count", 0)
    out = {"verification_passed": passed, "hallucinated_parts": parts}

    # 已经重试到上限还不过：放行但在末尾加提示，避免死循环
    if not passed and retry_count >= 2 and _HALLU_NOTE not in answer:
        out["answer"] = answer.rstrip() + "\n\n" + _HALLU_NOTE

    logger.info(
        "节点 verify 完成：passed=%s，可疑 %d 处，retry=%d (%.1fs)",
        passed, len(parts), retry_count, time.time() - t0,
    )
    return out


def route_after_verify(state: State):
    if state.get("verification_passed"):
        return "end"
    if state.get("retry_count", 0) >= 2:
        return "end"
    return "generate"


# 路由函数：根据分类结果决定走哪条路
def route(state: State):
    if state["intent"] == "entity":
        return "retrieve_graph"
    elif state["intent"] == "semantic":
        return "retrieve_vector"
    else:
        return "retrieve_both"


# 混合检索：两条路都走
def retrieve_both(state: State) -> State:
    s1 = retrieve_vector(state)
    s2 = retrieve_graph(state)
    return {
        "vector_results": s1["vector_results"],
        "vector_chunks": s1["vector_chunks"],
        "graph_results": s2["graph_results"],
        "graph_relations": s2["graph_relations"],
        "graph_nodes": s2["graph_nodes"],
    }


# 组装状态图
graph = StateGraph(State)
graph.add_node("classify", classify)
graph.add_node("retrieve_vector", retrieve_vector)
graph.add_node("retrieve_graph", retrieve_graph)
graph.add_node("retrieve_both", retrieve_both)
graph.add_node("generate", generate)
graph.add_node("verify", verify)

graph.set_entry_point("classify")
graph.add_conditional_edges("classify", route)
graph.add_edge("retrieve_vector", "generate")
graph.add_edge("retrieve_graph", "generate")
graph.add_edge("retrieve_both", "generate")
graph.add_edge("generate", "verify")
# verify 之后：通过 -> END；不通过且没到重试上限 -> 回 generate 重生成
graph.add_conditional_edges(
    "verify", route_after_verify, {"generate": "generate", "end": END}
)

workflow = graph.compile()


def run_query(query: str, kb_id: str = None, history: list = None) -> dict:
    """供 Flask 后端调用：跑一遍状态图，返回结构化结果。
    不管中间怎么炸，都保证返回一个结构完整、可 JSON 序列化的 dict。"""
    t0 = time.time()
    kb_id = kb_store.resolve_kb_id(kb_id)
    logger.info("run_query 开始：%r（kb=%s，上下文 %d 条）", query, kb_id, len(history or []))
    try:
        result = workflow.invoke({
            "query": query,
            "kb_id": kb_id,
            "history": history or [],
            "intent": "",
            "entities": [],
            "vector_results": "",
            "graph_results": "",
            "vector_chunks": [],
            "graph_relations": [],
            "graph_nodes": [],
            "answer": "",
            "suggestions": [],
            "verification_passed": None,
            "retry_count": 0,
            "hallucinated_parts": [],
        })
    except Exception:
        logger.exception("workflow.invoke 整体失败：%r", query)
        return {
            "answer": "抱歉，处理这个问题时出错了，请查看后端日志。",
            "intent": "",
            "vector_results": [],
            "graph_results": [],
            "graph_nodes": [],
            "suggestions": [],
            "verified": False,
            "error": "workflow_failed",
        }

    out = {
        "answer": result.get("answer", ""),
        "intent": result.get("intent", ""),
        "vector_results": result.get("vector_chunks", []) or [],
        "graph_results": result.get("graph_relations", []) or [],
        "graph_nodes": result.get("graph_nodes", []) or [],
        "suggestions": result.get("suggestions", []) or [],
        "verified": bool(result.get("verification_passed")),
        "kb_id": kb_id,
    }
    out.update(_skill_meta(query))
    logger.info(
        "run_query 完成 (%.1fs)：intent=%s，向量 %d，关系 %d，节点 %d，回答 %d 字，追问 %d，verified=%s",
        time.time() - t0, out["intent"], len(out["vector_results"]),
        len(out["graph_results"]), len(out["graph_nodes"]), len(out["answer"]),
        len(out["suggestions"]), out["verified"],
    )
    return out


def run_query_stream(query: str, kb_id: str = None, history: list = None):
    """流式版：先跑意图分类 + 检索，再把生成阶段的 token 逐块 yield 出来，
    生成完做一次幻觉检测，不通过就重新生成（最多 2 次）。
    产出 (事件类型, 数据) 元组：
      ("meta",   {intent, vector_results, graph_results, graph_nodes})
      ("token",  "增量文本")            # 只含回答正文，不含 ===追问=== 之后的内容
      ("verifying", {})                 # 开始做幻觉检测
      ("revise", {reason})              # 验证没过，正在重新生成，前端应清空已显示的回答
      ("done",   {suggestions: [...], verified: bool})
      ("error",  {message})
    """
    t0 = time.time()
    kb_id = kb_store.resolve_kb_id(kb_id)
    logger.info("run_query_stream 开始：%r（kb=%s，上下文 %d 条）", query, kb_id, len(history or []))
    state: dict = {
        "query": query, "kb_id": kb_id, "history": history or [],
        "intent": "", "entities": [], "vector_results": "", "graph_results": "",
        "vector_chunks": [], "graph_relations": [], "graph_nodes": [], "answer": "",
        "verification_passed": None, "retry_count": 0, "hallucinated_parts": [],
    }
    try:
        state.update(classify(state))
        branch = route(state)
        if branch == "retrieve_graph":
            state.update(retrieve_graph(state))
        elif branch == "retrieve_vector":
            state.update(retrieve_vector(state))
        else:
            state.update(retrieve_both(state))
    except Exception as e:
        logger.exception("run_query_stream 检索阶段失败")
        yield ("error", {"message": f"检索失败: {e}"})
        return

    yield ("meta", {
        "intent": state.get("intent", ""),
        "vector_results": state.get("vector_chunks", []) or [],
        "graph_results": state.get("graph_relations", []) or [],
        "graph_nodes": state.get("graph_nodes", []) or [],
    })

    # 检索原文，验证阶段拿来跟回答比对
    context = "\n".join(
        s for s in (state.get("vector_results") or "", state.get("graph_results") or "") if s
    ).strip()

    answer = ""
    suggestions: List[str] = []
    verified = True

    # 生成 -> 验证 -> （不通过则）重新生成，最多重来 2 次
    while True:
        if state["verification_passed"] is False:
            # 上一轮没过：告诉前端清空已显示内容，准备重新流式
            yield ("revise", {"reason": "检测到可能超出原文范围的内容，正在重新生成"})

        # 流式生成。回答正文里可能带 ===追问===，要在分隔符处停止往外吐 token。
        prompt = _generate_prompt(state)
        full = ""
        emitted = 0
        answer_done = False
        guard = len(_SUGGEST_SEP)
        try:
            llm, _ = _get_llm()
            for chunk in llm.stream(prompt):
                piece = getattr(chunk, "content", "") or ""
                if not piece:
                    continue
                full += piece
                if answer_done:
                    continue
                idx = full.find(_SUGGEST_SEP)
                if idx != -1:
                    if idx > emitted:
                        yield ("token", full[emitted:idx])
                    emitted = idx
                    answer_done = True
                else:
                    safe = len(full) - guard  # 末尾留一小段，防止吐出半个分隔符
                    if safe > emitted:
                        yield ("token", full[emitted:safe])
                        emitted = safe
        except Exception as e:
            logger.exception("run_query_stream 生成阶段失败")
            yield ("error", {"message": f"生成失败: {e}"})
            return

        if not answer_done and len(full) > emitted:
            yield ("token", full[emitted:])

        answer, suggestions = _split_answer_suggestions(full)
        state["answer"] = answer

        # 没有检索原文就没法核对，直接放行
        if not context:
            verified = True
            break

        yield ("verifying", {})
        passed, parts = _verify_answer(context, answer)
        state["verification_passed"] = passed
        state["hallucinated_parts"] = parts
        verified = passed
        if passed:
            break
        if state["retry_count"] >= 2:
            # 到重试上限还不过：放行 + 末尾加提示
            verified = False
            yield ("token", "\n\n" + _HALLU_NOTE)
            answer = answer.rstrip() + "\n\n" + _HALLU_NOTE
            break
        state["retry_count"] += 1

    logger.info(
        "run_query_stream 完成 (%.1fs)：intent=%s，回答 %d 字，追问 %d，verified=%s，retry=%d",
        time.time() - t0, state.get("intent"), len(answer), len(suggestions),
        verified, state["retry_count"],
    )
    yield ("done", {"suggestions": suggestions, "verified": verified, **_skill_meta(query)})


def _skill_meta(query: str) -> dict:
    """当前激活 Skill 的 id + 该问题被判定的细粒度类型，没加载 Skill 时都是 None。"""
    skill = get_active_skill()
    if not skill:
        return {"skill": None, "question_type": None}
    return {
        "skill": skill["config"]["id"],
        "question_type": skill["detect_question_type"](query),
    }


def get_stats(kb_id: str = None) -> dict:
    """某个知识库的统计信息，供前端侧边栏展示。"""
    return kb_store.kb_stats(kb_store.resolve_kb_id(kb_id))


# 仅在直接运行本文件时做测试，import 时不执行
if __name__ == "__main__":
    out = run_query("什么是Chain of Thought？")
    print("\n=== 意图 ===", out["intent"])
    print("=== 向量结果条数 ===", len(out["vector_results"]))
    print("=== 图谱结果条数 ===", len(out["graph_results"]))
    print("=== 幻觉检测 ===", "通过" if out["verified"] else "未通过")
    print("\n=== 最终回答 ===")
    print(out["answer"])
