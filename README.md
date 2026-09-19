# Knowledge Graph RAG

## 项目简介

这是一个**双路检索增强生成（RAG）系统**：把知识库同时建成**向量索引（FAISS）**和**知识图谱（SQLite 三元组）**，用 LangGraph 编排一条「意图分类 → 检索 → 生成 → 幻觉验证」的状态图，让模型先判断问题是「概念解释」「实体关系」还是「两者都要」，再选最合适的检索路径，最后对生成结果做一次幻觉检测，不通过就自动重生成。

解决的问题：纯向量 RAG 擅长回答「什么是 XXX」，但回答「A 和 B 是什么关系」「X 依赖哪些技术」这类多跳/关系型问题时经常检索不到关键信息、或把关系答错。这里额外维护一份从文档里抽取出的实体关系图谱，遇到关系型问题时改走图谱查询，两条路径可以在同一个问题里混合使用。

核心技术：FastAPI + LangGraph + LangChain + FAISS + SQLite + DeepSeek（生成/实体抽取）+ HuggingFace Embedding（向量化，本地跑，不依赖任何 API）。

评测结果（30 道题，人工评分，0/0.5/1 三档）：图谱增强整体平均分 **0.867**，纯向量基线 **0.75**；在「实体关系类」问题上从 0.75 提升到 **1.00**，提升最明显（见下文「评测结果」）。

## 功能特点

- **双路检索（向量 + 知识图谱）**：FAISS 语义检索原文片段 + SQLite 三元组查实体关系，按问题类型自动选路径或两者都用
- **LangGraph 意图路由 + Function Calling**：一次模型调用同时完成「判断该走哪条检索路径」和「从问题里抽取关键实体」，不用额外调一次 API
- **验证 Agent 幻觉检测**：生成回答后再调一次模型核对内容是否超出检索原文范围，不通过自动重生成（最多 2 次），诚实的「资料不足」不算幻觉
- **多知识库管理**：网页上新建/切换知识库，每个库独立的向量索引 + 图谱 + 原始文档，互不干扰
- **知识图谱可视化**：命中图谱检索时，前端用力导向图画出本次问题涉及的实体和关系
- **Redis 查询缓存**：非流式接口按 query+知识库 缓存 1 小时，Redis 不可用时自动降级，不影响核心功能
- **MCP Server 支持**：把检索能力（不含生成）暴露成 MCP 工具，Claude Desktop / Cursor 等客户端可以直接查询你的知识库

## 技术栈

| 分类 | 技术 |
|---|---|
| 后端框架 | FastAPI + uvicorn（单进程，同时托管接口和前端静态文件） |
| 编排 | LangGraph（状态图）+ LangChain |
| 生成模型 | DeepSeek（`deepseek-chat`，OpenAI 兼容接口） |
| 向量检索 | FAISS + HuggingFace `all-MiniLM-L6-v2`（本地 embedding，不需要 API） |
| 知识图谱 | SQLite（entities / relations 三元组表） |
| 查询缓存 | Redis（可选，未安装/未启动自动禁用） |
| 前端 | React + Vite，`react-force-graph-2d`（图谱可视化） |
| MCP | FastMCP（独立虚拟环境，Python 3.11+） |
| 部署 | Docker + docker-compose（app + redis 两个容器） |

## 快速开始

三种方式任选一种：

### 方式一：Docker（推荐，最简单）

```bash
git clone <仓库地址>
cd knowledge-graph-rag
docker-compose up --build
# 打开 http://localhost:5001
# 首次使用会弹窗要求输入 DeepSeek API key
```

前端不在容器里编译，镜像直接用仓库里已经打包好的 `frontend/dist`。`docker-compose.yml` 会额外起一个 Redis 容器做查询缓存（可选，缺了也不影响主功能）。

### 方式二：手动安装

```bash
git clone <仓库地址>
cd knowledge-graph-rag
pip install -r requirements.txt
# 方法A：设置环境变量
export DEEPSEEK_API_KEY=your_api_key_here    # Windows PowerShell: $env:DEEPSEEK_API_KEY="..."
# 方法B：不设置环境变量，直接启动，在网页弹窗里输入 key
python app.py
# 打开 http://localhost:5001
```

### 方式三：MCP Server（让 AI 客户端直接调用知识库检索）

```bash
# 需要 Python 3.11+（MCP 框架要求，跟主项目 3.9 环境完全独立）
py -3.11 -m venv mcp_venv
mcp_venv\Scripts\activate
pip install -r requirements_mcp.txt

# Claude Code
claude mcp add knowledge-graph-rag <mcp_venv python路径> <mcp_server.py路径>

# Claude Desktop：把配置加到 %APPDATA%\Claude\claude_desktop_config.json
# Cursor：Settings → MCP Servers → Add Server
# 详见 README_MCP.md
```

注明：**MCP server 只提供检索功能，不需要 DeepSeek API key**（它不调 DeepSeek，也不 import 生成相关的代码）。

## 项目架构

```
用户提问
  │
  ▼
[classify] LangGraph 节点：一次 Function Calling 调用，同时判断走哪条检索路径
  │                        （语义 / 实体关系 / 两者都要）+ 抽取关键实体
  ├─ retrieve_vector ──► FAISS 相似度检索，取原文片段
  ├─ retrieve_graph  ──► SQLite 三元组查询，取实体间关系
  └─ retrieve_both   ──► 两者都跑
  │
  ▼
[generate] 把检索结果拼进 prompt，流式生成回答
  │
  ▼
[verify] 幻觉检测：回答内容是否都能在检索原文里找到依据
  │
  ├─ 不通过且未达重试上限 ──► 回到 generate 重新生成（最多 2 次）
  └─ 通过 / 达上限 ──► 结束，返回给用户
```

API key 三段链路：网页提交的 key（进程内存，不落盘）> 环境变量 `DEEPSEEK_API_KEY` > 无（提示用户输入）。向量检索、图谱查询、MCP Server 都不经过这条链路，不依赖 DeepSeek。

## API 接口说明

完整 Swagger 文档见 `http://127.0.0.1:5001/docs`（`/redoc` 也有）。主要接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | 非流式问答，走 Redis 缓存 |
| POST | `/api/chat/stream` | SSE 流式问答（前端用这个） |
| GET | `/api/stats` | 知识库统计（实体/关系/文档数） |
| GET | `/api/kb` | 知识库列表 |
| POST | `/api/kb/create` | 新建知识库 |
| POST | `/api/kb/{kb_id}/upload` | 上传文档（后台任务，轮询 `/api/kb/job/{job_id}`） |
| DELETE | `/api/kb/{kb_id}/doc/{filename}` | 删除文档，重建该库索引 |
| GET / DELETE | `/api/chats`、`/api/chats/{id}` | 聊天历史 |
| POST | `/api/settings/apikey` | 提交 DeepSeek API key（仅存内存） |
| GET | `/api/settings/apikey/status` | 查看 key 是否已配置（不返回完整 key） |
| DELETE | `/api/cache` | 清空查询缓存 |

## 评测结果

`eval/` 目录下 30 道题（语义类 / 实体关系类 / 混合类各 10 道），对比「纯向量 RAG」与「图谱增强 RAG（完整 workflow）」，人工按 0 / 0.5 / 1 三档打分：

| 问题类型 | 纯向量 RAG | 图谱增强 RAG |
|---|---|---|
| 语义类（10 题） | 0.750 | 0.750 |
| 实体关系类（10 题） | 0.750 | **1.000** |
| 混合类（10 题） | 0.750 | 0.850 |
| **整体平均** | **0.750** | **0.867** |

图谱增强在纯概念解释类问题上和向量检索打平（符合预期，这类问题本就不需要图谱），但在需要理清「A 和 B 是什么关系」的实体关系类问题上有明显提升。评测脚本：`eval/run_eval.py`，原始问答见 `eval/eval_results_scored.json`。

## 注意事项

- API key 通过网页界面输入或环境变量设置，**不要硬编码在代码里**；网页提交的 key 只存进程内存，重启后端需要重新输入
- `knowledge_bases/` 目录在首次上传文档时自动创建，不会随仓库分发
- Redis 可选，没有 Redis 缓存功能自动禁用，不影响核心问答/检索功能
- 更详细的网页版说明（接口全量文档、前端功能、聊天历史/上下文机制）见 [`README_webapp.md`](README_webapp.md)；MCP Server 的环境搭建、工具说明、测试方法见 [`README_MCP.md`](README_MCP.md)
