import { useEffect, useState, useCallback } from "react";

export default function Sidebar({ kbId }) {
  const [stats, setStats] = useState(null);
  const [err, setErr] = useState(null);
  const [tick, setTick] = useState(0); // 手动重试用

  useEffect(() => {
    let alive = true;
    let timer;
    setErr(null);
    setStats(null);

    const url = kbId
      ? `/api/stats?kb_id=${encodeURIComponent(kbId)}`
      : "/api/stats";

    // 后端刚启动时要 import workflow（约 10s），5001 还没起来会 ECONNREFUSED，
    // 这里自动重试若干次，避免页面一打开就卡在“加载失败”。
    const attempt = (n) => {
      fetch(url)
        .then((r) => r.json())
        .then((d) => {
          if (!alive) return;
          if (d.error) throw new Error(d.error);
          setStats(d);
          setErr(null);
        })
        .catch((e) => {
          if (!alive) return;
          if (n < 8) {
            setErr(`连接后端中…（第 ${n + 1} 次重试）`);
            timer = setTimeout(() => attempt(n + 1), 2000);
          } else {
            setErr(e.message || "无法连接后端");
          }
        });
    };
    attempt(0);

    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [tick, kbId]);

  const retry = useCallback(() => {
    setStats(null);
    setTick((t) => t + 1);
  }, []);

  const maxTypeCount =
    stats?.top_types?.reduce((m, t) => Math.max(m, t.count), 0) || 1;

  const fmt = (n) => (n == null ? "—" : n.toLocaleString());

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <div className="brand-mark">KG</div>
        <div>
          <div className="brand-name">Knowledge Graph RAG</div>
          <div className="brand-sub">意图路由 · 双路检索</div>
        </div>
      </div>

      <div className="side-card">
        <div className="side-card-title">知识库</div>
        {err && !stats && (
          <div className="side-error">
            {err}
            {!err.includes("重试") && (
              <button className="retry-link" onClick={retry}>重试</button>
            )}
          </div>
        )}
        {!stats && !err && <div className="side-muted">加载中…</div>}
        {stats && (
          <>
            <div className="stat-grid">
              <div className="stat-box">
                <div className="stat-num">{fmt(stats.entities)}</div>
                <div className="stat-label">实体</div>
              </div>
              <div className="stat-box">
                <div className="stat-num">{fmt(stats.relations)}</div>
                <div className="stat-label">关系</div>
              </div>
              <div className="stat-box">
                <div className="stat-num">{fmt(stats.documents)}</div>
                <div className="stat-label">文档</div>
              </div>
            </div>

            {stats.top_types?.length > 0 && (
              <div className="type-dist">
                <div className="type-dist-title">实体类型分布</div>
                {stats.top_types.map((t) => (
                  <div className="type-row" key={t.type}>
                    <span className="type-name" title={t.type}>
                      {t.type}
                    </span>
                    <span className="type-bar">
                      <i style={{ width: `${(t.count / maxTypeCount) * 100}%` }} />
                    </span>
                    <span className="type-count">{t.count}</span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      <div className="side-card">
        <div className="side-card-title">检索路径</div>
        <ul className="route-list">
          <li><span className="intent-tag semantic">向量检索</span> 概念解释类</li>
          <li><span className="intent-tag entity">图谱检索</span> 实体关系类</li>
          <li><span className="intent-tag hybrid">混合检索</span> 两者都需要</li>
        </ul>
        <p className="side-muted">
          走图谱的问题会在回答下方画出本次检索到的实体关系图。
        </p>
      </div>
    </aside>
  );
}
