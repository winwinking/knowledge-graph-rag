import { useCallback, useEffect, useState } from "react";
import { PageTabs } from "./Root.jsx";

const IconTrash = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" />
  </svg>
);

function Spinner() {
  return <span className="mini-spinner" aria-hidden />;
}

export default function ChatHistory({
  tab, onTab, active, refreshKey, currentChatId, onOpenChat,
}) {
  const [chats, setChats] = useState(null);
  const [err, setErr] = useState(null);
  const [deleting, setDeleting] = useState(null);

  const load = useCallback(async () => {
    try {
      const d = await fetch("/api/chats").then((r) => r.json());
      if (d.error) throw new Error(d.error);
      setChats(d.chats || []);
      setErr(null);
    } catch (e) {
      setErr(e.message || "加载历史失败");
    }
  }, []);

  useEffect(() => {
    if (active) load();
  }, [active, refreshKey, load]);

  const del = async (e, id) => {
    e.stopPropagation();
    if (!window.confirm("删除这个对话？删了不可恢复。")) return;
    setDeleting(id);
    try {
      const d = await fetch(`/api/chats/${encodeURIComponent(id)}`, {
        method: "DELETE",
      }).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      await load();
    } catch (e) {
      setErr(e.message || "删除失败");
    } finally {
      setDeleting(null);
    }
  };

  return (
    <div className="layout">
      <div className="main">
        <div className="topbar">
          <div className="topbar-left">
            <PageTabs tab={tab} onTab={onTab} />
          </div>
        </div>

        <div className="kb-page">
          <div className="hist-inner">
            {err && (
              <div className="kb-banner error">
                {err}
                <button onClick={() => setErr(null)}>×</button>
              </div>
            )}

            {chats === null && <div className="kb-muted">加载中…</div>}
            {chats && chats.length === 0 && (
              <div className="kb-muted">
                还没有历史对话。去「对话」页问点什么，回答完成后就会自动存下来。
              </div>
            )}

            <div className="hist-list">
              {(chats || []).map((c) => (
                <div
                  key={c.id}
                  className={`hist-row ${c.id === currentChatId ? "current" : ""}`}
                  onClick={() => onOpenChat(c.id)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") onOpenChat(c.id);
                  }}
                >
                  <div className="hist-main">
                    <div className="hist-title">
                      {c.title}
                      {c.id === currentChatId && (
                        <span className="hist-badge">当前</span>
                      )}
                    </div>
                    <div className="hist-meta">
                      {c.updated_at || c.created_at} · {c.kb_name} ·{" "}
                      {c.message_count} 条消息
                    </div>
                  </div>
                  <button
                    className="hist-del"
                    onClick={(e) => del(e, c.id)}
                    disabled={deleting === c.id}
                    title="删除"
                  >
                    {deleting === c.id ? <Spinner /> : <IconTrash />}
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
