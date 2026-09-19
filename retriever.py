from langchain_openai import ChatOpenAI
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
import os
import sqlite3
import json

# 连接API（key 从环境变量读，运行前先设置 DEEPSEEK_API_KEY）
_api_key = os.environ.get("DEEPSEEK_API_KEY")
if not _api_key:
    raise SystemExit("请先设置环境变量 DEEPSEEK_API_KEY 再运行本脚本")

llm = ChatOpenAI(
    model="deepseek-chat",
    api_key=_api_key,
    base_url="https://api.deepseek.com"
)

# 加载向量库
embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
vector_store = FAISS.load_local("faiss_index", embeddings, allow_dangerous_deserialization=True)

# 测试一个问题：向量搜索
question = "RAG的核心流程是什么？"
vector_results = vector_store.similarity_search(question, k=3)

print("=== 向量搜索结果 ===")
for i, doc in enumerate(vector_results):
    print(f"\n第{i+1}条：")
    print(doc.page_content[:150])
# 图谱搜索：先让LLM从问题里提取关键实体
extract_entity_prompt = """从以下问题中提取关键实体（名词），只输出JSON列表，不要其他文字。
例如输入"RAG和LangChain是什么关系"，输出["RAG", "LangChain"]

问题："""

response = llm.invoke(extract_entity_prompt + question)
raw = response.content.replace("```json", "").replace("```", "").strip()
query_entities = json.loads(raw)
print(f"\n从问题中提取到实体：{query_entities}")

# 在SQLite里查这些实体的关系
conn = sqlite3.connect("knowledge_graph.db")
cursor = conn.cursor()

print("\n=== 图谱搜索结果 ===")
for entity in query_entities:
    cursor.execute(
        "SELECT entity1, relation, entity2 FROM relations WHERE entity1 LIKE ? OR entity2 LIKE ?",
        (f"%{entity}%", f"%{entity}%"))
    results = cursor.fetchall()
    for r in results:
        print(f"  {r[0]} → {r[1]} → {r[2]}")

conn.close()

# 收集图谱搜索的关系
conn = sqlite3.connect("knowledge_graph.db")
cursor = conn.cursor()
graph_context = []
for entity in query_entities:
    cursor.execute(
        "SELECT entity1, relation, entity2 FROM relations WHERE entity1 LIKE ? OR entity2 LIKE ?",
        (f"%{entity}%", f"%{entity}%"))
    for r in cursor.fetchall():
        graph_context.append(f"{r[0]} → {r[1]} → {r[2]}")
conn.close()

# 拼最终prompt
vector_text = "\n".join([doc.page_content[:300] for doc in vector_results])
graph_text = "\n".join(graph_context)

final_prompt = f"""根据以下检索到的信息回答问题。

【向量检索结果】
{vector_text}

【图谱检索结果】
{graph_text}

问题：{question}
请综合以上信息回答。如果信息不足，请说明。"""

answer = llm.invoke(final_prompt)
print("\n=== 最终回答 ===")
print(answer.content)