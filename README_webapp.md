# Knowledge Graph RAG — Web 版

## 一键启动（推荐）

双击桌面的 **「Knowledge Graph RAG」快捷方式** —— 只起一个 FastAPI / uvicorn 进程（后台隐藏窗口），
它同时托管打包后的前端和 `/api` 接口；就绪后自动打开 `http://localhost:5001`。
接口的 Swagger 文档在 `http://localhost:5001/docs`（`/redoc` 也有）。
**关掉那个状态窗口 = 停止服务**（靠 Windows Job 对象）。

- 桌面快捷方式 → `start_app.bat`（本目录）→ `start_app.ps1`（真正逻辑，可改）
- 图标 `app_icon.ico` / `app_icon.png`（`#aaaaff` 极简知识图谱图形）
- 首次运行会自动 `npm install` + `npm run build`（之后前端有改动，手动在 `frontend` 目录重跑 `npm run build` 即可，不用重启后端也能刷新页面看到——静态文件是现读的）
- 不再需要单独跑前端 dev server
- 打开的是 `http://127.0.0.1:5001`（用 IP 不用 `localhost`）
- 后端已经在跑时，再双击快捷方式只会打开浏览器、不重启服务（重复双击不会互相踢掉）

> **踩过的坑**：系统开了代理（Clash 之类，`127.0.0.1:7890`）时，`Invoke-WebRequest` 探活会走代理、
> 每次要 2+ 秒 → 超过 2 秒超时 → launcher 一直以为后端没起来 → 浏览器不打开（其实后端好好的）。
> 现在探活用 `HttpWebRequest` + `.Proxy = $null` 直连，17ms 就返回。

## Docker 部署

```bash
docker-compose up --build
```

起两个容器：`app`（FastAPI，映射 `5001:5001`）和 `redis`（`redis:7-alpine`）。就绪后开 `http://localhost:5001`。
停：`docker-compose down`（数据不丢）。

- **前端不在容器里编译**：镜像直接用仓库里现成的 `frontend/dist`。改了前端要先在宿主机 `frontend` 目录 `npm run build`，再 `docker-compose up --build`。
- **依赖**：`requirements.txt`（对齐开发环境版本；`Dockerfile` 里单独装 CPU 版 torch，避开 ~2GB 的 CUDA 轮子）。embedding 模型 `all-MiniLM-L6-v2` 在 build 时就下进镜像，首次查询不用联网。
- **Redis**：`app` 通过环境变量 `REDIS_HOST=redis`（compose 内部网络用服务名）连 `redis` 服务。`app.py` 默认 `localhost:6379`，所以在宿主机直接 `python app.py` 不受影响。Redis 连不上会自动降级（缓存关掉，服务照跑）。
- **持久化**：
  - `redis-data` volume → Redis 的 AOF 数据（`/data`）
  - `./knowledge_bases` bind mount → 所有知识库（SQLite / FAISS / 原始文件）
  - `./chat_history` bind mount → 聊天历史 JSON
- `.dockerignore` 排除了 `frontend/node_modules`、`__pycache__`、日志、启动器脚本、`eval/` 等。
- 首次启动容器里 `ensure_default_kb()` 会把镜像里的旧 `knowledge_graph.db` / `faiss_index/` 迁进 `knowledge_bases/ai_interview`（宿主机已迁过就跳过）。

## 结构

```
knowledge-graph-rag/
├─ workflow.py      # LangGraph 状态图：classify→检索→generate→verify（幻觉检测，不通过回环重生成，最多 2 次）
│                   #   classify 用 DeepSeek function calling 一次搞定「选检索路径 + 抽关键实体」，
│                   #   三个 tool（retrieve_vector 无参 / retrieve_graph / retrieve_both 带 entities）→ intent + State.entities，
│                   #   retrieve_graph 直接拿 State.entities 查 SQLite，不再单独调 API 抽实体
│                   #   按 kb_id 加载对应知识库的 SQLite / FAISS；run_query() / run_query_stream() / get_stats()
├─ kb_store.py      # 多知识库：registry.json 注册表 + 每库独立 graph.db / faiss_index/ / docs/
│                   #   切块（md 两层 / pdf 按字数）、抽实体关系、增删文档、重建索引都在这里
├─ chat_store.py    # 聊天历史（chat_history/<chat_id>.json，后端每轮自动追加）+ 内存里的对话上下文
├─ app.py           # FastAPI/uvicorn：托管 frontend/dist（页面）+ /api/* 接口（知识库管理 + 聊天历史），一个进程；Swagger 在 /docs
│                   #   请求/响应体都用 Pydantic BaseModel；SSE 用 StreamingResponse；dist 静态资源 StaticFiles mount 到 /assets；启动 `python app.py`（内部 uvicorn.run）
│                   #   /api/chat 走 Redis 查询缓存（key=md5(query+kb_id)，TTL 1h；Redis 挂了自动降级）；流式接口不缓存
├─ graph_builder.py # 只是个薄壳：把源文档目录整体重灌进默认知识库（日常增删走前端管理页）
├─ knowledge_bases/ # 所有知识库
│  ├─ registry.json #   库名 / 创建时间 / 文档列表
│  └─ <kb_id>/      #   graph.db（entities/relations，带 source_file）+ faiss_index/ + docs/（原始文件副本）
├─ chat_history/    # 每个对话一个 JSON（id / 标题 / 时间 / kb_id / messages[]）
├─ knowledge_graph.db / faiss_index/   # 旧的扁平产物，首次启动已迁移成默认库「AI面试题」，留作备份
├─ eval/            # 评测：30 道题 + 对比脚本 + 打分页
├─ frontend/        # Vite + React：对话 / 历史对话 / 知识库管理 三个页面
├─ requirements.txt # 运行期依赖（对齐开发环境版本，间接依赖交给 pip 解析）
├─ Dockerfile       # python:3.9-slim；装依赖 + CPU 版 torch + 预下 embedding 模型；用现成 dist；CMD python app.py
├─ docker-compose.yml # app（FastAPI，5001:5001）+ redis（7-alpine）；REDIS_HOST=redis；redis-data volume + knowledge_bases/chat_history bind mount
└─ .dockerignore    # 排除 node_modules / __pycache__ / 日志 / 启动器 / eval 等
```

## 聊天历史 + 上下文

前端顶部三个 tab：**对话 / 历史对话 / 知识库管理**。

- **历史持久化**：每轮问答完成后，后端把用户消息 + 助手回答（含 `verify_status`）自动写进
  `chat_history/<chat_id>.json`，标题取第一条用户消息前 20 字。刷新 / 关页面 / 开新对话都不丢已完成的对话
  （前端不参与存盘，所以没有「未保存」状态）。「历史对话」页按更新时间倒序列出，点开继续聊，× 删除。
- **上下文**：后端内存里维护 `chat_id -> 最近 8 条消息`，喂给 `generate` 节点理解「它」「上面那个」之类的指代
  （只影响生成，检索不变）。`chat_id` 由前端 `crypto.randomUUID()` 生成，随每次 `/api/chat/stream` 带上。
- **新建对话**：前端换一个新 `chat_id` + 清空界面。旧对话已经存好了，不需要手动保存。
- **清空上下文**：`POST /api/chats/<id>/clear_context` —— 清空内存上下文 + 往历史文件写一条 `divider`，
  聊天界面插一条「—— 上下文已清空 ——」分隔线。消息记录都还在，只是后续提问模型不再参考前文。
  重载对话时，上下文只恢复最后一条 divider 之后的消息。

## 知识库管理

前端顶部「知识库管理」tab：新建知识库、往库里上传 `.md` / `.pdf`、删文档。聊天页顶部下拉框选当前查哪个库。

- **多库**：每个库有独立的 `graph.db` + `faiss_index/` + `docs/`，互不干扰。首次启动把旧的
  `knowledge_graph.db` / `faiss_index/` + 源 md 目录迁移成默认库「AI面试题」（`kb_id=ai_interview`）。
- **上传**：切块 → 逐块调 DeepSeek 抽实体关系存该库 SQLite → 向量化进该库 FAISS。**很慢**（每块一次 API 调用），
  所以后端丢后台线程处理，返回 `job_id`，前端轮询 `/api/kb/job/<job_id>` 显示进度。原始文件存一份到 `docs/`。
  同名文档 = 替换。
- **删文档**：按 `source_file` 删该库 SQLite 里的实体/关系 + 删 `docs/` 里的原始文件 + 用剩余文档重建整个 FAISS
  索引（FAISS 删不掉单条向量）。删空了保留空库。
  > 迁移来的默认库是「legacy」：旧实体/关系没有 `source_file`，删旧文档只能移除向量、清不掉实体关系；
  > 要彻底重建就重新上传该文档，或 `python graph_builder.py` 整体重灌。
- **索引热更新**：`workflow.py` 按 `graph.db` / `index.faiss` 的 mtime 判断缓存是否过期，上传/删文档后
  **不用重启后端**，下一次提问自动加载新索引。

### 整体重建默认知识库

```bash
python graph_builder.py     # 清空默认库，把源文档目录里所有 .md/.pdf 重新灌一遍
```

切块规则：

- **`.md`**：`MarkdownHeaderTextSplitter` 按 `#` / `##` 标题切，每个 `##` 段一个 chunk；
  超过 1500 字符的再用 `RecursiveCharacterTextSplitter(800/150)` 二次切。
- **`.pdf`**：`pypdf` 提取全文（扫描件/图片型提不出文本会跳过），没有标题结构，直接 `RecursiveCharacterTextSplitter(800/150)` 切。需要 `pip install pypdf`。

每个 chunk 的 `metadata` 带 `source_file`（原始文件名，删文档 / 重建用）和 `header`（标题路径，PDF 为空串）。

## 启动（日常）

```bash
cd E:\ailearning\LightRAG\knowledge-graph-rag
python app.py
```

监听 `http://0.0.0.0:5001`（5000 被别的项目占了，改到了 5001；`python app.py` 里就是 `uvicorn.run(app, host="0.0.0.0", port=5001)`）。
**这一个进程既是页面也是接口**：`/` 返回 `frontend/dist/index.html`，`/assets/*` 是打包后的 js/css（`StaticFiles` mount），
其它未匹配路径回退 `index.html`（SPA），业务接口都在 `/api/*` 下。`frontend/dist` 不存在时页面会提示先 `npm run build`。

### 接口（都在 `/api` 下）

Swagger 文档 `http://127.0.0.1:5001/docs`。每个接口的请求体 / 响应体都是 Pydantic `BaseModel`。
出错时统一返回 `{"error": "说明"}`（HTTP 4xx/5xx），前端只认 body 里的 `error` 字段。

`POST /api/chat`  body: `{"query": "问题", "kb_id": "ai_interview", "chat_id": "uuid"}`（`kb_id` / `chat_id` 可选）

**查询缓存（Redis）**：非流式 `/api/chat` 会查 Redis 缓存，key = `md5(query + kb_id)`，命中直接返回（响应多一个 `"cached": true`），TTL 1 小时。
未命中就正常走 workflow，跑完把结果写回缓存。**有对话上下文时（`chat_id` 且不是第一轮）不走缓存**（答案依赖前文）。
`/api/chat/stream` 不走缓存。Redis 连不上时缓存整体失效、服务照常跑。上传 / 删除文档后会自动清掉那个库的缓存。

返回：

```json
{
  "answer": "回答内容",
  "intent": "semantic | entity | hybrid",
  "vector_results": [ { "content": "chunk 原文", "metadata": { "source_file": "03-RAG技术.md", "header": "标题 > 小节" } } ],
  "graph_results": [ { "entity1": "A", "relation": "关系", "entity2": "B" } ],
  "graph_nodes":   [ { "name": "A", "type": "实体类型", "core": true } ],
  "suggestions":   [ "推荐追问1", "推荐追问2" ],
  "verified":      true
}
```

- `semantic` 路径：`graph_results` / `graph_nodes` 为空
- `entity` 路径：`vector_results` 为空
- `hybrid` 路径：两者都有
- `graph_nodes.core` 为 `true` 表示该实体是从当前问题里抽出来的核心实体（前端高亮）
- `suggestions`：generate 节点顺带生成的 2-3 个后续问题（不额外调 LLM）
- `verified`：verify 节点的幻觉检测结果。`true`=回答内容都能在检索原文里找到依据；
  `false`=检测到疑似超出原文的内容且已重生成 2 次仍未通过（回答末尾会带 `⚠️` 提示）。
  模型回答「原文里没有足够信息回答」也算通过 —— 诚实拒答不是幻觉，只有编造原文没有的事实/数据/关系才是。

`GET /api/stats?kb_id=<id>` → `{"entities": N, "relations": N, "documents": N, "top_types": [{"type","count"}]}`（`kb_id` 可选）

### 知识库管理接口

- `GET /api/kb` → `{"kbs": [{id, name, created_at, doc_count, docs}], "default": "ai_interview"}`
- `POST /api/kb/create` body `{"name": "库名"}` → 新建空库，返回 `{id, name, created_at, docs}`（同名报 400）
- `GET /api/kb/<kb_id>` → 库详情 + `stats`
- `POST /api/kb/<kb_id>/upload` multipart `file`（.md/.pdf）→ `{job_id, filename}`，后台建库
- `GET /api/kb/job/<job_id>` → `{status: running|done|error, phase, done, total, result?, error?}`，前端轮询
- `DELETE /api/kb/<kb_id>/doc/<filename>` → `{status: "deleted"}`，删文档 + 重建该库索引

### 聊天历史接口

- `GET /api/chats` → `{"chats": [{id, title, created_at, updated_at, kb_id, kb_name, message_count}]}`（更新时间倒序）
- `GET /api/chats/<chat_id>` → 完整对话（`{id, title, created_at, updated_at, kb_id, messages}`）
- `DELETE /api/chats/<chat_id>` → `{status: "deleted"}`
- `POST /api/chats/<chat_id>/clear_context` → `{status: "cleared", divider}`；清空后端上下文 + 写 divider

`/api/chat/stream` body 收 `chat_id`：完成一轮就把这轮追加进 `chat_history/<chat_id>.json`（`revise` 事件会重置累积的回答，存的是最终版）。

`DELETE /api/cache` → 清空查询缓存。带 `?kb_id=<id>` 只清那个库的，不带清全部。返回 `{status: cleared|disabled, cleared: 条数, kb_id}`。

`POST /api/reset` → `{"status": "reset"}`。旧端点，保留兼容；新建对话现在由前端换 `chat_id` 完成。

`GET /api/health` → `{"status": "ok"}`（`/health` 也留着，启动脚本探活用）

`POST /api/chat/stream` → **SSE 流式**。事件：
- `meta` `{intent, vector_results, graph_results, graph_nodes}` —— 检索完、开始生成前发一次
- `token` `"增量文本"` —— 生成阶段逐块吐（不含 `===追问===` 之后的内容）
- `verifying` `{}` —— 一次生成结束，开始做幻觉检测
- `revise` `{reason}` —— 检测没通过，正在重新生成；前端收到应清空已显示的回答
- `done` `{suggestions: [...], verified: true/false}`
- `error` `{message}`

`/api/chat`（非流式）保留，评测脚本仍用它（评测脚本直接 import workflow，不走 HTTP）。

## 前端：打包 vs 调试

**日常**：`npm run build` 生成 `frontend/dist`，FastAPI 直接托管，不用单独跑 dev server。
前端改了代码就重跑一次 `npm run build`，刷新页面即可（后端不用重启）。

**调试**：另开终端在 `frontend` 跑 `npm run dev`（端口 5173，热更新）。
`vite.config.js` 把 `/api` 代理到 `127.0.0.1:5001`（不 rewrite 前缀，和打包模式路径一致），
所以 dev 时后端 `python app.py` 也要开着。

```bash
cd E:\ailearning\LightRAG\knowledge-graph-rag\frontend
npm install         # 首次
npm run build       # 日常：生成 dist 给 FastAPI 托管
# 或
npm run dev         # 调试：热更新，打开 http://localhost:5173（需后端也开着）
```

改了后端端口的话，同步改 `frontend/vite.config.js` 里的 `target`。

## 前端功能

- **三个页面**（顶部 tab 切换，都常驻不卸载，切走再切回来不丢状态）：「对话」/「历史对话」/「知识库管理」。
  `main.jsx → Root.jsx`（持有 tab / 知识库列表 / kbId / chatId / 加载的历史对话），
  `App.jsx`（聊天）、`ChatHistory.jsx`（历史列表）、`KbManager.jsx`（知识库管理）。
- 聊天页顶栏：知识库下拉框（选哪个库就查哪个，随请求带 `kb_id`，侧边栏统计跟着切换）+
  「清空上下文」「新建对话」两个按钮。
- 历史对话页：倒序列出所有对话（标题 / 时间 / 知识库 / 消息数），点开加载进聊天页继续（会一并切到那个库），× 删除有二次确认。
- 管理页：左列知识库卡片（库名 / 文档数 / 创建日期，标「对话中」）+ 新建；右列选中库的文档表
  （文件名 / 上传时间 / 块数 / 删除）+ 上传按钮。上传轮询 job 显示「解析文件 → 提取实体关系 N/M 块 → 建立向量索引」，删除显示 spinner。
- 视觉：护眼淡紫蓝（主色 `#aaaaff`）+ 浅灰白底 + 深灰字，claude.ai 风格极简聊天界面
- **流式输出**：走 `/api/chat/stream` SSE，回答逐字往外蹦（客户端打字机匀速渲染 + 闪烁光标）；
  先显示「检索中」，收到 `meta` 后变「生成中」，再开始吐字
- **知识图谱生成动画**：核心实体先逐个浮现 → 关联实体铺开 → 连线逐条接上 → 力导向布局归位，约 1.3s
- 顶栏「新建对话」：换新 `chat_id` + 清空界面（旧对话后端已存好）；「清空上下文」：`/api/chats/<id>/clear_context` + 插分隔线
- 加载历史对话：`GET /api/chats/<id>` 的 messages 转成前端消息结构（只有文字 + verify 标签，图谱/检索详情不回放）
- 首屏示例问题 chip；每条回答下方 2-3 个「推荐追问」按钮，点一下自动发送
- 左侧边栏：知识库统计（实体 / 关系 / 文档数、实体类型分布）、检索路径说明
- 每条回答上方标签：向量检索 / 图谱检索 / 混合检索
- 回答文字下方、图谱上方：幻觉检测标签 —— `✓ 已验证`（内容基于检索原文）/ `⚠️ 部分内容未验证`
  （疑似超出原文且重生成 2 次未通过）；重生成时前端会清空并重新逐字渲染
- 回答下方可折叠面板：向量搜到的原文片段、图谱搜到的关系三元组
- **知识图谱可视化**（`react-force-graph-2d` 力导向图）：intent 为 entity / hybrid 且图谱有结果时，
  在回答下方画出本次检索涉及的实体和关系。核心实体 `#aaaaff` 高亮，关联实体灰色，连线上标关系文字。
  关系过多时截断到 28 条（优先保留与核心实体相连的），标题栏注明总数。滚轮缩放、拖动平移、悬停看类型。

## 依赖

前端新增 `react-force-graph-2d`（已在 `package.json`，`npm install` 会装）。
后端：
- Web 框架 `pip install fastapi uvicorn python-multipart`（app.py 从 Flask 迁到 FastAPI 后需要；已装 fastapi 0.128 / uvicorn 0.39）。
- 查询缓存 `pip install redis`（已装 7.0.1）+ 本地 Redis 服务。Windows 用 `winget install Redis.Redis`（装的是 MSOpenTech 3.0.504，会注册成开机自启的 Windows 服务「Redis」，端口 6379，安装目录 `C:\Program Files\Redis`）。**Redis 没起也能跑**，只是缓存失效。`redis-cli ping` 应返回 PONG。
- 处理 PDF 需要 `pip install pypdf`（`kb_store.py` 里 lazy import，上传 md-only 时不需要；已装 6.16.2）。
