import { useCallback, useEffect, useState } from "react";

const DEEPSEEK_SIGNUP_URL = "https://platform.deepseek.com";

const IconGear = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </svg>
);

async function fetchStatus() {
  const r = await fetch("/api/settings/apikey/status");
  return r.json();
}

async function submitKey(key) {
  const r = await fetch("/api/settings/apikey", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: key }),
  });
  const d = await r.json();
  if (d.error) throw new Error(d.error);
  return d;
}

function KeyForm({ onSaved, onCancel, cancelLabel }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const save = async () => {
    const k = key.trim();
    if (!k || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await submitKey(k);
      setKey("");
      onSaved();
    } catch (e) {
      setErr(e.message || "保存失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="apikey-form">
      <input
        type="password"
        autoFocus
        placeholder="sk-..."
        value={key}
        onChange={(e) => setKey(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") save();
        }}
      />
      {err && <div className="apikey-err">{err}</div>}
      <div className="apikey-actions">
        <button className="btn-primary" onClick={save} disabled={!key.trim() || busy}>
          {busy ? "保存中…" : "保存"}
        </button>
        {onCancel && (
          <button className="btn-ghost" onClick={onCancel}>
            {cancelLabel || "取消"}
          </button>
        )}
      </div>
    </div>
  );
}

export default function SettingsGate() {
  const [status, setStatus] = useState(null); // null=加载中
  const [showFirstRun, setShowFirstRun] = useState(false);
  const [showPanel, setShowPanel] = useState(false);
  const [dismissedFirstRun, setDismissedFirstRun] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const d = await fetchStatus();
      setStatus(d);
    } catch {
      setStatus({ configured: false, source: "none", last4: null });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (status && !status.configured && !dismissedFirstRun) {
      setShowFirstRun(true);
    }
  }, [status, dismissedFirstRun]);

  const handleSaved = async () => {
    await refresh();
    setShowFirstRun(false);
    setShowPanel(false);
  };

  return (
    <>
      <button className="settings-fab" title="API Key 设置" onClick={() => setShowPanel(true)}>
        <IconGear />
      </button>

      {showFirstRun && (
        <div className="modal-mask">
          <div className="modal-card">
            <div className="modal-title">需要 DeepSeek API Key</div>
            <p className="modal-desc">
              本项目需要 DeepSeek API key 才能使用对话生成功能，请输入你的 key
              （只存在这次后端运行的内存里，不会写入文件，重启后端后需要重新输入）。
              还没有 key？去{" "}
              <a href={DEEPSEEK_SIGNUP_URL} target="_blank" rel="noreferrer">
                platform.deepseek.com
              </a>{" "}
              申请。
            </p>
            <p className="modal-note">
              提示：如果你只是通过 MCP Server 检索知识库（不走网页对话），不需要设置 key。
            </p>
            <KeyForm
              onSaved={handleSaved}
              onCancel={() => {
                setShowFirstRun(false);
                setDismissedFirstRun(true);
              }}
              cancelLabel="稍后设置"
            />
          </div>
        </div>
      )}

      {showPanel && (
        <div className="modal-mask" onClick={() => setShowPanel(false)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">API Key 设置</div>
            <div className="apikey-status">
              {status?.configured ? (
                <span className="apikey-badge ok">
                  已设置（{status.source === "env" ? "来自环境变量" : "来自网页输入"} · 尾号{" "}
                  {status.last4}）
                </span>
              ) : (
                <span className="apikey-badge warn">未设置</span>
              )}
            </div>
            <p className="modal-desc">
              DeepSeek API key 只用于对话生成（意图分类 / 生成回答 / 幻觉检测）。向量检索本身不需要
              key，MCP Server 检索知识库也不需要 key。
            </p>
            <KeyForm onSaved={handleSaved} onCancel={() => setShowPanel(false)} cancelLabel="关闭" />
          </div>
        </div>
      )}
    </>
  );
}
