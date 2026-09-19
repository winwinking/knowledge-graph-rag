import { useCallback, useEffect, useState } from "react";
import App from "./App.jsx";
import KbManager from "./KbManager.jsx";
import ChatHistory from "./ChatHistory.jsx";
import SettingsGate from "./Settings.jsx";

const TABS = [
  { id: "chat", label: "对话" },
  { id: "history", label: "历史对话" },
  { id: "kb", label: "知识库管理" },
];

export function PageTabs({ tab, onTab }) {
  return (
    <div className="page-tabs">
      {TABS.map((t) => (
        <button
          key={t.id}
          className={tab === t.id ? "active" : ""}
          onClick={() => onTab(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function newChatId() {
  try {
    if (crypto?.randomUUID) return crypto.randomUUID();
  } catch {
    /* fall through */
  }
  return `c-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

// 历史文件里的消息 -> 前端聊天消息结构
function toUiMessages(messages) {
  return (messages || []).map((m) => {
    if (m.role === "divider") return { role: "divider" };
    if (m.role === "user") return { role: "user", content: m.content };
    return {
      role: "assistant",
      content: m.content || "",
      streaming: false,
      intent: null,
      vectorResults: [],
      graphResults: [],
      graphNodes: [],
      suggestions: [],
      verified: typeof m.verify_status === "boolean" ? m.verify_status : null,
      fromHistory: true,
    };
  });
}

export default function Root() {
  const [tab, setTab] = useState("chat");
  const [kbs, setKbs] = useState([]);
  const [kbId, setKbId] = useState(null);
  const [kbError, setKbError] = useState(null);

  const [chatId, setChatId] = useState(() => newChatId());
  const [loadedChat, setLoadedChat] = useState(null); // {nonce, messages}
  // 每完成一轮问答 +1，历史页据此刷新列表
  const [historyTick, setHistoryTick] = useState(0);

  const reloadKbs = useCallback(async () => {
    try {
      const r = await fetch("/api/kb");
      const d = await r.json();
      if (d.error) throw new Error(d.error);
      const list = d.kbs || [];
      setKbs(list);
      setKbError(null);
      setKbId((cur) => {
        if (cur && list.some((k) => k.id === cur)) return cur;
        return d.default || (list[0] && list[0].id) || null;
      });
      return list;
    } catch (e) {
      setKbError(e.message || "无法加载知识库列表");
      return [];
    }
  }, []);

  useEffect(() => {
    let alive = true;
    let n = 0;
    const attempt = () => {
      reloadKbs().then((list) => {
        if (!alive) return;
        if (!list.length && n < 8) {
          n += 1;
          setTimeout(attempt, 2000);
        }
      });
    };
    attempt();
    return () => {
      alive = false;
    };
  }, [reloadKbs]);

  const startNewChat = useCallback(() => {
    setChatId(newChatId());
    setLoadedChat({ nonce: Date.now(), messages: [] });
  }, []);

  const openChat = useCallback(
    async (id) => {
      try {
        const d = await fetch(`/api/chats/${encodeURIComponent(id)}`).then((r) => r.json());
        if (d.error) throw new Error(d.error);
        setChatId(id);
        setLoadedChat({ nonce: Date.now(), messages: toUiMessages(d.messages) });
        if (d.kb_id) {
          setKbId((cur) => (kbs.some((k) => k.id === d.kb_id) ? d.kb_id : cur));
        }
        setTab("chat");
      } catch (e) {
        alert("打开对话失败：" + (e.message || e));
      }
    },
    [kbs]
  );

  const onTurnComplete = useCallback(() => setHistoryTick((t) => t + 1), []);

  const currentKb = kbs.find((k) => k.id === kbId) || null;

  return (
    <>
      <SettingsGate />
      <div hidden={tab !== "chat"} className="page-holder">
        <App
          tab={tab}
          onTab={setTab}
          kbs={kbs}
          kbId={kbId}
          setKbId={setKbId}
          currentKb={currentKb}
          chatId={chatId}
          startNewChat={startNewChat}
          loadedChat={loadedChat}
          onTurnComplete={onTurnComplete}
        />
      </div>
      <div hidden={tab !== "history"} className="page-holder">
        <ChatHistory
          tab={tab}
          onTab={setTab}
          active={tab === "history"}
          refreshKey={historyTick}
          currentChatId={chatId}
          onOpenChat={openChat}
        />
      </div>
      <div hidden={tab !== "kb"} className="page-holder">
        <KbManager
          tab={tab}
          onTab={setTab}
          active={tab === "kb"}
          kbs={kbs}
          kbId={kbId}
          setKbId={setKbId}
          reloadKbs={reloadKbs}
          kbError={kbError}
        />
      </div>
    </>
  );
}
