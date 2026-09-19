import { useState, useRef, useEffect, useCallback } from "react";
import Sidebar from "./Sidebar.jsx";
import GraphPanel from "./GraphPanel.jsx";
import { PageTabs } from "./Root.jsx";

const INTENT_LABEL = {
  semantic: "向量检索",
  entity: "图谱检索",
  hybrid: "混合检索",
};

const STARTER_QUESTIONS = [
  "什么是 RAG？",
  "Agent 和 LLM Chain 有什么区别？",
  "CoT 和 ReAct 是什么关系？",
];

/* --- icons --- */
const IconPlus = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"
    strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
);
const IconSend = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
    strokeLinecap="round" strokeLinejoin="round"><path d="M7 11l5-5 5 5M12 6v13" /></svg>
);
const IconArrow = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"
    strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M13 6l6 6-6 6" /></svg>
);

function IntentTag({ intent }) {
  if (!intent) return null;
  const label = INTENT_LABEL[intent] || intent;
  const cls = ["semantic", "entity", "hybrid"].includes(intent) ? intent : "hybrid";
  return <span className={`intent-tag ${cls}`}>{label}</span>;
}

function VerifiedTag({ verified }) {
  if (verified === null || verified === undefined) return null;
  return verified ? (
    <div className="verify-tag ok">✓ 已验证 · 回答内容基于检索原文</div>
  ) : (
    <div className="verify-tag warn">⚠️ 部分内容未验证 · 可能超出文档原文范围</div>
  );
}

function Collapsible({ title, count, children }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="collapsible">
      <button className="collapsible-head" onClick={() => setOpen((v) => !v)}>
        <span className={`caret ${open ? "open" : ""}`}>▶</span>
        {title} <span className="count">{count}</span>
      </button>
      {open && <div className="collapsible-body">{children}</div>}
    </div>
  );
}

function RetrievalDetails({ vectorResults, graphResults }) {
  const hasVector = vectorResults?.length > 0;
  const hasGraph = graphResults?.length > 0;
  if (!hasVector && !hasGraph) return null;
  return (
    <div className="details">
      {hasVector && (
        <Collapsible title="向量检索 · 原文片段" count={vectorResults.length}>
          {vectorResults.map((c, i) => (
            <div key={i} className="chunk">
              <span className="chunk-idx">#{i + 1}</span>
              <pre className="chunk-text">{c.content}</pre>
            </div>
          ))}
        </Collapsible>
      )}
      {hasGraph && (
        <Collapsible title="图谱检索 · 关系三元组" count={graphResults.length}>
          <ul className="triples">
            {graphResults.map((r, i) => (
              <li key={i}>
                <span className="ent">{r.entity1}</span>
                <span className="rel"> —{r.relation}→ </span>
                <span className="ent">{r.entity2}</span>
              </li>
            ))}
          </ul>
        </Collapsible>
      )}
    </div>
  );
}

function Suggestions({ items, onPick }) {
  if (!items?.length) return null;
  return (
    <div className="suggests">
      <span className="suggests-label">推荐追问</span>
      {items.map((q, i) => (
        <button key={i} className="suggest-btn" onClick={() => onPick(q)}>
          <IconArrow />
          {q}
        </button>
      ))}
    </div>
  );
}

export default function App({
  tab, onTab, kbs = [], kbId, setKbId, currentKb,
  chatId, startNewChat, loadedChat, onTurnComplete,
}) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const scrollRef = useRef(null);
  const taRef = useRef(null);
  const abortRef = useRef(null);

  // 历史对话被加载进来（或点了「新建对话」）：替换当前消息列表
  useEffect(() => {
    if (!loadedChat) return;
    abortRef.current?.abort();
    setLoading(false);
    setInput("");
    setMessages(loadedChat.messages || []);
  }, [loadedChat?.nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
  }, [messages, loading]);

  useEffect(() => {
    const ta = taRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
    }
  }, [input]);

  // 更新最后一条消息（流式渲染都作用在末尾的 assistant 占位消息上）
  const patchLast = useCallback((patch) => {
    setMessages((m) => {
      if (!m.length) return m;
      const copy = m.slice();
      const last = copy[copy.length - 1];
      copy[copy.length - 1] =
        typeof patch === "function" ? patch(last) : { ...last, ...patch };
      return copy;
    });
  }, []);

  const send = useCallback(
    async (text) => {
      const query = (text ?? input).trim();
      if (!query || loading) return;

      abortRef.current?.abort();
      const ac = new AbortController();
      abortRef.current = ac;

      setInput("");
      setMessages((m) => [
        ...m,
        { role: "user", content: query },
        {
          role: "assistant",
          content: "",
          streaming: true,
          phase: "retrieving", // retrieving -> generating -> verifying
          intent: null,
          vectorResults: [],
          graphResults: [],
          graphNodes: [],
          suggestions: [],
          verified: null, // null=未知, true=已验证, false=可能超范围
          verifying: false,
        },
      ]);
      setLoading(true);

      // 打字机：网络 token 先进 buf，定时器把 buf 里的字逐步"吐"到界面
      let buf = "";
      let revealed = 0;
      let streamEnded = false;
      let pendingSuggestions = [];
      let pendingVerified = null;

      const finish = () => {
        clearInterval(typer);
        setLoading(false);
        patchLast((last) => ({
          ...last,
          content: buf,
          streaming: false,
          verifying: false,
          suggestions: pendingSuggestions,
          verified: pendingVerified,
        }));
        onTurnComplete?.(); // 后端已存好这一轮，通知历史列表刷新
      };

      const typer = setInterval(() => {
        if (ac.signal.aborted) {
          clearInterval(typer);
          return;
        }
        if (revealed < buf.length) {
          // 落后越多吐得越快，保证 1-2 秒内追上
          const step = Math.max(1, Math.min(8, Math.ceil((buf.length - revealed) / 6)));
          revealed = Math.min(buf.length, revealed + step);
          const shown = buf.slice(0, revealed);
          patchLast((last) => ({ ...last, content: shown, phase: "generating" }));
        } else if (streamEnded) {
          finish();
        }
      }, 18);

      try {
        const resp = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            query,
            kb_id: kbId || undefined,
            chat_id: chatId || undefined,
          }),
          signal: ac.signal,
        });

        if (!resp.ok || !resp.body) {
          const t = await resp.text().catch(() => "");
          let msg = `HTTP ${resp.status}`;
          try {
            msg = JSON.parse(t).error || msg;
          } catch {
            /* keep */
          }
          throw new Error(msg);
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let sseBuf = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          sseBuf += decoder.decode(value, { stream: true });

          let sep;
          while ((sep = sseBuf.indexOf("\n\n")) !== -1) {
            const rawEvent = sseBuf.slice(0, sep);
            sseBuf = sseBuf.slice(sep + 2);
            let evName = "message";
            let dataStr = "";
            for (const line of rawEvent.split("\n")) {
              if (line.startsWith("event:")) evName = line.slice(6).trim();
              else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
            }
            if (!dataStr) continue;
            let payload;
            try {
              payload = JSON.parse(dataStr);
            } catch {
              continue;
            }

            if (evName === "meta") {
              patchLast((last) => ({
                ...last,
                phase: "generating",
                intent: payload.intent,
                vectorResults: payload.vector_results || [],
                graphResults: payload.graph_results || [],
                graphNodes: payload.graph_nodes || [],
              }));
            } else if (evName === "token") {
              buf += payload;
            } else if (evName === "verifying") {
              patchLast((last) => ({ ...last, verifying: true, phase: "verifying" }));
            } else if (evName === "revise") {
              // 验证没过，后端要重新流式生成：清空已显示的回答
              buf = "";
              revealed = 0;
              patchLast((last) => ({
                ...last,
                content: "",
                verifying: false,
                phase: "generating",
                reviseNote: payload.reason || "正在重新生成",
              }));
            } else if (evName === "done") {
              pendingSuggestions = payload.suggestions || [];
              pendingVerified =
                typeof payload.verified === "boolean" ? payload.verified : null;
              streamEnded = true;
            } else if (evName === "error") {
              throw new Error(payload.message || "生成出错");
            }
          }
        }
        streamEnded = true;
      } catch (e) {
        clearInterval(typer);
        if (e.name === "AbortError") return;
        setLoading(false);
        patchLast((last) => ({
          ...last,
          content: `出错了：${e.message}`,
          streaming: false,
          error: true,
        }));
      }
    },
    [input, loading, patchLast, kbId, chatId, onTurnComplete]
  );

  const newChat = useCallback(() => {
    abortRef.current?.abort();
    setLoading(false);
    setInput("");
    startNewChat?.(); // Root 换新 chatId 并把 messages 清空（走 loadedChat effect）
    taRef.current?.focus();
  }, [startNewChat]);

  const clearContext = useCallback(async () => {
    if (!chatId || !messages.length) return;
    try {
      await fetch(`/api/chats/${encodeURIComponent(chatId)}/clear_context`, {
        method: "POST",
      });
    } catch {
      /* 后端清不掉也无所谓，前端插分隔线仍然有意义 */
    }
    setMessages((m) => [...m, { role: "divider" }]);
  }, [chatId, messages.length]);

  const onKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="layout">
      <Sidebar kbId={kbId} />

      <div className="main">
        <div className="topbar">
          <div className="topbar-left">
            <PageTabs tab={tab} onTab={onTab} />
            {kbs.length > 0 && (
              <label className="kb-select">
                <span>知识库</span>
                <select
                  value={kbId || ""}
                  onChange={(e) => setKbId(e.target.value)}
                  disabled={loading}
                >
                  {kbs.map((k) => (
                    <option key={k.id} value={k.id}>
                      {k.name}（{k.doc_count} 篇）
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          <div className="topbar-actions">
            <button
              className="ghost-btn"
              onClick={clearContext}
              disabled={loading || !messages.length}
              title="清空后端记忆，聊天记录保留，后续提问不再参考前文"
            >
              清空上下文
            </button>
            <button className="new-chat-btn" onClick={newChat}>
              <IconPlus /> 新建对话
            </button>
          </div>
        </div>

        <div className="chat" ref={scrollRef}>
          <div className="chat-inner">
            {messages.length === 0 && (
              <div className="empty">
                <div className="empty-logo">KG</div>
                <div className="empty-title">从一个问题开始</div>
                <div className="empty-sub">
                  当前知识库：{currentKb?.name || "默认"} · 向量检索取原文、图谱检索理关系
                </div>
                <div className="empty-chips">
                  {STARTER_QUESTIONS.map((q) => (
                    <button key={q} className="chip" onClick={() => send(q)}>
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((msg, i) => {
              if (msg.role === "divider") {
                return (
                  <div key={i} className="ctx-divider">
                    <span>—— 上下文已清空 ——</span>
                  </div>
                );
              }
              if (msg.role === "user") {
                return (
                  <div key={i} className="msg-user">
                    <div className="bubble">{msg.content}</div>
                  </div>
                );
              }

              // 图谱等文字完全流式输出结束后再挂载，避免把聊天内容顶上去
              const showGraph =
                !msg.error &&
                !msg.streaming &&
                (msg.intent === "entity" || msg.intent === "hybrid") &&
                msg.graphResults?.length > 0;

              const showThinking = msg.streaming && !msg.content;

              return (
                <div key={i} className="msg-ai">
                  <div className="ai-row">
                    <div className="ai-avatar">KG</div>
                    <div className="ai-body">
                      <IntentTag intent={msg.intent} />
                      {showThinking ? (
                        <div className="answer-text loading">
                          {msg.phase === "generating" ? "生成中" : "检索中"}
                          <span className="dots"><i /><i /><i /></span>
                        </div>
                      ) : (
                        <div className={`answer-text ${msg.error ? "error" : ""}`}>
                          {msg.content}
                          {msg.streaming && <span className="type-caret" />}
                        </div>
                      )}
                      {msg.streaming && msg.verifying && (
                        <div className="verify-running">
                          核对回答与原文一致性
                          <span className="dots"><i /><i /><i /></span>
                        </div>
                      )}
                    </div>
                  </div>

                  {!msg.error && !msg.streaming && (
                    <VerifiedTag verified={msg.verified} />
                  )}

                  {!msg.error && (
                    <RetrievalDetails
                      vectorResults={msg.vectorResults}
                      graphResults={msg.graphResults}
                    />
                  )}
                  {showGraph && (
                    <GraphPanel
                      key={`g-${i}`}
                      relations={msg.graphResults}
                      nodes={msg.graphNodes}
                      onReady={() =>
                        scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight)
                      }
                    />
                  )}
                  {!msg.error && !msg.streaming && (
                    <Suggestions items={msg.suggestions} onPick={(q) => send(q)} />
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <div className="composer-wrap">
          <div className="composer">
            <textarea
              ref={taRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="输入问题，Enter 发送 · Shift+Enter 换行"
              rows={1}
            />
            <button
              className="send-btn"
              onClick={() => send()}
              disabled={loading || !input.trim()}
              title="发送"
            >
              <IconSend />
            </button>
          </div>
          <div className="composer-hint">
            回答由 DeepSeek 基于检索内容流式生成，可能有误
          </div>
        </div>
      </div>
    </div>
  );
}
