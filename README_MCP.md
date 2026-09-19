# Knowledge Graph RAG —— MCP Server

把项目的知识库检索能力（FAISS 向量检索 + SQLite 知识图谱）包装成 [MCP](https://modelcontextprotocol.io/)
工具，让 Claude Desktop、Cursor 等任何支持 MCP 协议的 AI 客户端都能直接查询你的知识库。

**这个 MCP server 只负责检索，不生成回答**：不调用 DeepSeek API，不 import `workflow.py` /
LangGraph。生成回答的工作完全交给连接它的 AI 客户端自己完成——客户端调工具拿到检索结果后，
用它自己的模型组织语言回答用户。

## 环境准备（重要：单独的 Python 版本）

主项目（`app.py` / `workflow.py` / `kb_store.py`）跑在 **Python 3.9**。但 `fastmcp`
（以及它依赖的官方 `mcp` SDK）**从各自最新的历史版本起都要求 Python ≥ 3.10**，装不进
项目主环境。所以 MCP server 用一个**完全独立的虚拟环境**跑，跟主项目的 3.9 环境互不影响：

```bash
cd E:\ailearning\LightRAG\knowledge-graph-rag

# 1. 装 Python 3.11（如果系统还没有）
winget install Python.Python.3.11

# 2. 建独立虚拟环境
py -3.11 -m venv mcp_venv

# 3. 装依赖（用 requirements_mcp.txt，跟主项目的 requirements.txt 分开）
mcp_venv\Scripts\python.exe -m pip install -r requirements_mcp.txt
```

**主项目的 Python 3.9 环境和 `requirements.txt` 完全不受影响**——`app.py` 该怎么跑还怎么跑。
`mcp_server.py` 只 `import kb_store`，而 `kb_store.py` 顶层只用标准库、langchain 系列全是
函数内部才 `import`，所以两边环境不会互相污染。

## mcp_server.py 做了什么

直接复用 `kb_store.py` 里现成的路径 / embedding 工具函数，自己另起一套轻量的 FAISS / SQLite
访问逻辑（不走 `workflow.py`）：

| 用到的东西 | 来自 |
|---|---|
| `knowledge_bases/<kb_id>/faiss_index/` 加载 | `kb_store.kb_faiss_dir()` + `FAISS.load_local()` |
| embedding 模型（all-MiniLM-L6-v2） | `kb_store.get_embeddings()`（和 app.py 共用同一份实现，启动时加载一次） |
| `knowledge_bases/<kb_id>/graph.db` 查询 | 自己开 `sqlite3` 连接，查 `entities`/`relations` 表 |
| 知识库列表 | `kb_store.list_kbs()` / `get_kb()` |

FAISS 索引按 `index.faiss` 文件的修改时间缓存——你在网页管理页上传/删除文档、索引被重建后，
MCP server **不用重启**，下次检索会自动发现文件变了并重新加载。

## 四个工具

- **`list_knowledge_bases()`** —— 列出所有知识库（`kb_id` / `name` / `doc_count` / `created_at`）。
  客户端应该先调这个，再决定用哪个 `kb_id` 查。
- **`search_knowledge(query, kb_id, top_k=5)`** —— 向量语义检索，返回原文片段 `{content, metadata}`。
  概念解释类问题用这个。
- **`query_graph(entities, kb_id)`** —— 用一批实体名去知识图谱里查关联的三元组
  `{entity1, relation, entity2}`。`entities` 由客户端自己从用户问题里识别出来传进来——
  这个工具**不做实体提取、不调用任何外部 LLM**。
- **`search_all(query, entities, kb_id, top_k=5)`** —— 一次性把上面两个都跑一遍，返回
  `{vector_results, graph_results}`。适合不确定该用哪个、或者两种都要的情况。同样，
  `entities` 需要客户端自己先识别好再传入。

无效的 `kb_id` / 空的 `query` / 空的 `entities` 会直接抛错（报错信息里带提示，比如列出当前
可用的 `kb_id`），方便客户端据此重试，而不是像网页版那样静默换成默认知识库。

## Claude Desktop 配置

打开 Claude Desktop 的配置文件（Windows：`%APPDATA%\Claude\claude_desktop_config.json`），
在 `mcpServers` 里加一项，`command` 要写 **`mcp_venv` 里那个 `python.exe` 的完整路径**
（不能写成 `python`，那样会用系统 PATH 里的 3.9，装不了 fastmcp 就直接跑不起来）：

```json
{
  "mcpServers": {
    "knowledge-graph-rag": {
      "command": "E:\\ailearning\\LightRAG\\knowledge-graph-rag\\mcp_venv\\Scripts\\python.exe",
      "args": ["E:\\ailearning\\LightRAG\\knowledge-graph-rag\\mcp_server.py"]
    }
  }
}
```

（这段也存了一份在项目根目录的 `claude_desktop_config.example.json`，可以直接复制那段 `mcpServers`
内容合并进你自己的 `claude_desktop_config.json`——如果里面已经有其它 MCP server，别整个文件覆盖掉，
只把 `knowledge-graph-rag` 这一项加进已有的 `mcpServers` 对象里。）

改完保存，完全退出并重新打开 Claude Desktop（不是刷新窗口，是整个退出重启，Windows 上从
系统托盘图标右键 Quit）。之后新对话里 Claude 应该能看到 `list_knowledge_bases` 等 4 个工具
（工具图标 🔨 或输入框旁的插槽图标能看到）。

Cursor 等其它支持 MCP 的客户端配置方式类似，找它们文档里 "MCP servers" / "stdio" 相关设置，
同样把 `command` 指到 `mcp_venv\Scripts\python.exe`。

## 测试方法（已实测，下面每条命令都跑通过）

### 1. `fastmcp inspect` —— 纯命令行，最快，不需要 Node

```bash
mcp_venv\Scripts\fastmcp.exe inspect mcp_server.py
```

打印服务器信息 + 工具数量摘要，几秒钟出结果，适合先确认 server 能正常加载、4 个工具都注册上了：

```
Server
  Name:         knowledge-graph-rag
Components
  Tools:        4
  ...
```

### 2. MCP Inspector（图形界面，能直接点按钮调工具，需要 Node/npx）

```bash
mcp_venv\Scripts\fastmcp.exe dev mcp_server.py
```

会起一个本地网页，左边能看到 4 个工具，点开填参数就能测：

1. 先调 `list_knowledge_bases()`，确认能看到知识库列表（比如 `ai_interview` / `AI面试题`）
2. 拿到的 `kb_id` 传给 `search_knowledge(query="什么是RAG", kb_id="ai_interview", top_k=5)`，
   确认能返回原文片段（见下面「已知限制」——检索质量取决于 query 写得够不够具体）
3. `query_graph(entities=["RAG", "向量数据库"], kb_id="ai_interview")`，确认返回三元组关系
4. `search_all(query="RAG和向量数据库什么关系", entities=["RAG", "向量数据库"], kb_id="ai_interview")`，
   确认 `vector_results` 和 `graph_results` 都有内容
5. 故意传一个不存在的 `kb_id` 或空的 `entities`，确认报错信息清楚（列出了可用 kb_id / 提示不能为空）

### 3. 用 fastmcp 的 `Client` 直接走协议调用（脚本化，CI/自测最方便）

```bash
mcp_venv\Scripts\python.exe -c "
import asyncio
from fastmcp import Client
import mcp_server as s

async def main():
    async with Client(s.mcp) as client:
        print([t.name for t in await client.list_tools()])
        r = await client.call_tool('list_knowledge_bases', {})
        print(r.data)

asyncio.run(main())
"
```

### 4. 跳过协议层，直接调工具函数（验证检索逻辑本身，最快最直接）

`@mcp.tool()` 包出来的还是普通函数，可以直接调（也可以调 `_search_knowledge_impl` /
`_query_graph_impl` 这两个内部实现）：

```bash
mcp_venv\Scripts\python.exe -c "
import mcp_server as s
print(s.list_knowledge_bases())
print(s.search_knowledge('什么是RAG', 'ai_interview', 5))
print(s.query_graph(['RAG'], 'ai_interview'))
print(s.search_all('RAG和向量数据库什么关系', ['RAG', '向量数据库'], 'ai_interview', 3))
"
```

### 已实测结果

- `fastmcp inspect` ✅ 4 个工具（`list_knowledge_bases` / `search_knowledge` / `query_graph` / `search_all`）
  都正确注册，docstring 和参数 schema 都被 FastMCP 自动解析出来了
- 通过真实 MCP 协议（`Client(s.mcp)`）调用 `list_knowledge_bases` / `query_graph` ✅ 正常返回
- 无效 `kb_id` 通过协议调用会被包成 `ToolError`，但错误信息完整透传（`知识库 'nope' 不存在。可用的
  kb_id：ai_interview, kb`），客户端能读到并据此重试 ✅
- `search_all` 在 `entities=[]` 时只返回向量结果、`graph_results` 为空 ✅；`query="" `时只返回图谱结果 ✅
- FAISS 索引是主项目（Python 3.9 + langchain-community 0.3.x）建的，在 `mcp_venv`（Python 3.11 +
  langchain-community 0.4.x）里 `FAISS.load_local()` 加载完全没问题，两边版本差了一个大版本号但
  索引格式兼容 ✅

### 已知限制（不是 bug，是设计上的取舍）

`search_knowledge` / `search_all` 不会像网页版的 `workflow.py` 那样先用 DeepSeek 把 query
改写扩展一遍再检索（那样做需要引入 LLM 依赖，违背了"MCP server 只检索、不依赖任何外部 LLM"的
要求）。所以像"什么是RAG"这种很短的 query，向量检索质量取决于 embedding 模型
（all-MiniLM-L6-v2）本身对中文短查询的语义区分能力——已知它对这种"一个主题下有很多具体变体"
的情况（比如 RAG 底下一堆 Self-RAG/Corrective RAG 小节）排名不够准。**实践中应该让调用的 AI
客户端把 query 写得更具体**（比如"RAG 的定义和基本原理是什么"而不是干巴巴的"什么是RAG"），
或者多调几次 `search_knowledge` 换不同措辞试试、加大 `top_k`。这是当前项目 embedding 模型的
已知限制，网页版那边也一样存在（只是网页版额外加了一层 LLM 查询改写来缓解，MCP server 按需求
不能这么做）。

### 在 Claude Desktop 里直接问

配好配置、重启 Claude Desktop 后，直接问一句"用我的 knowledge-graph-rag 知识库查一下 RAG
是什么"，观察它是否：① 先调 `list_knowledge_bases`，② 再调 `search_knowledge`（或
`search_all`，如果它自己判断问题里有实体），③ 基于返回的片段自己组织语言回答。

## 目录里多了什么

```
knowledge-graph-rag/
├─ mcp_server.py                          # MCP server 本体，只 import kb_store.py，不碰 workflow.py
├─ requirements_mcp.txt                   # mcp_venv 专用依赖清单
├─ mcp_venv/                              # 独立的 Python 3.11 虚拟环境（体积较大，已加进 .dockerignore）
├─ claude_desktop_config.example.json     # Claude Desktop 配置示例，照抄 mcpServers 那段
└─ README_MCP.md                          # 本文件
```

主项目的 `app.py`、`workflow.py`、`requirements.txt`、`Dockerfile` 等一律没有改动。
