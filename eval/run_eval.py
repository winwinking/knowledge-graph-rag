# -*- coding: utf-8 -*-
"""对比评测：纯向量 RAG  vs  图谱增强 RAG（完整 workflow）。

用法：
    cd E:\\ailearning\\LightRAG\\knowledge-graph-rag\\eval
    python run_eval.py

输入：eval/test_questions.json（30 道题）
输出：eval/eval_results.json（score 字段留 null，人工打分）
"""

import json
import os
import sys
import time

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文/符号打印报错
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# workflow.py 在上级目录
HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

# 复用 workflow 里已配好的 LLM、FAISS 加载逻辑和完整状态图
import workflow  # noqa: E402

QUESTIONS_PATH = os.path.join(HERE, "test_questions.json")
RESULTS_PATH = os.path.join(HERE, "eval_results.json")

VECTOR_ONLY_K = 5


def vector_only_rag(query: str) -> str:
    """纯向量 RAG：只做 FAISS similarity_search(k=5)，结果直接喂模型，
    不走意图分类、不走图谱检索。"""
    vector_store = workflow.get_vector_store()
    docs = vector_store.similarity_search(query, k=VECTOR_ONLY_K)
    context = "\n\n".join(doc.page_content for doc in docs)
    prompt = f"""根据以下检索到的资料回答问题。

【检索资料】
{context}

问题：{query}
请根据以上资料回答。如果资料不足以回答，请说明。"""
    response = workflow.llm.invoke(prompt)
    return response.content


def graph_enhanced_rag(query: str) -> str:
    """图谱增强 RAG：完整 workflow（意图分类 → 条件路由 → 双路检索 → 生成）。"""
    result = workflow.run_query(query)
    return result.get("answer", "")


def main() -> None:
    with open(QUESTIONS_PATH, "r", encoding="utf-8") as f:
        questions = json.load(f)

    total = len(questions)
    results = []

    print(f"共 {total} 道题，开始评测\n")

    for i, q in enumerate(questions, start=1):
        qid = q["id"]
        qtype = q["type"]
        question = q["question"]

        print(f"[{i}/{total}] (id={qid}, type={qtype})")
        print(f"  问题：{question}")

        # 1) 纯向量 RAG
        t0 = time.time()
        try:
            vec_ans = vector_only_rag(question)
            print(f"  [OK]  纯向量 RAG 完成（{time.time() - t0:.1f}s）")
        except Exception as e:
            vec_ans = f"[ERROR] {e}"
            print(f"  [ERR] 纯向量 RAG 出错：{e}")

        # 2) 图谱增强 RAG
        t0 = time.time()
        try:
            graph_ans = graph_enhanced_rag(question)
            print(f"  [OK]  图谱增强 RAG 完成（{time.time() - t0:.1f}s）")
        except Exception as e:
            graph_ans = f"[ERROR] {e}"
            print(f"  [ERR] 图谱增强 RAG 出错：{e}")

        results.append({
            "id": qid,
            "type": qtype,
            "question": question,
            "vector_only_answer": vec_ans,
            "graph_enhanced_answer": graph_ans,
            "vector_only_score": None,
            "graph_enhanced_score": None,
        })

        # 每题跑完就落盘，中途崩了也不丢进度
        with open(RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        print()

    print(f"全部完成，结果已写入 {RESULTS_PATH}")
    print("score 字段为 null，请人工打分。")


if __name__ == "__main__":
    main()
