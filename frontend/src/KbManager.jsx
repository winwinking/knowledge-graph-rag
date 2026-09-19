import { useCallback, useEffect, useRef, useState } from "react";
import { PageTabs } from "./Root.jsx";

const IconUpload = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
    strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 16V5M7 10l5-5 5 5M5 19h14" />
  </svg>
);
const IconTrash = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" />
  </svg>
);
const IconPlus = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"
    strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
);

function Spinner() {
  return <span className="mini-spinner" aria-hidden />;
}

export default function KbManager({
  tab, onTab, active, kbs, kbId, setKbId, reloadKbs, kbError,
}) {
  // 管理页里当前查看的库，默认跟着聊天页选中的库
  const [selId, setSelId] = useState(kbId);
  useEffect(() => {
    if (!selId && kbId) setSelId(kbId);
  }, [kbId, selId]);
  useEffect(() => {
    // 选中的库被删了 / 列表变化后不在了，回退到第一个
    if (kbs.length && !kbs.some((k) => k.id === selId)) {
      setSelId(kbs[0].id);
    }
  }, [kbs, selId]);

  const [detail, setDetail] = useState(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [createBusy, setCreateBusy] = useState(false);
  const [job, setJob] = useState(null);     // {phase, done, total}
  const [deleting, setDeleting] = useState(null); // filename
  const [err, setErr] = useState(null);
  const fileRef = useRef(null);
  const pollRef = useRef(null);

  const loadDetail = useCallback(async (id) => {
    if (!id) return;
    setLoadingDetail(true);
    try {
      const d = await fetch(`/api/kb/${encodeURIComponent(id)}`).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      setDetail(d);
    } catch (e) {
      setErr(e.message || "加载知识库详情失败");
      setDetail(null);
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  useEffect(() => {
    if (active && selId) loadDetail(selId);
  }, [active, selId, loadDetail]);

  useEffect(() => () => clearTimeout(pollRef.current), []);

  const busy = !!job || !!deleting || createBusy;

  const createKb = async () => {
    const name = newName.trim();
    if (!name || createBusy) return;
    setCreateBusy(true);
    setErr(null);
    try {
      const d = await fetch("/api/kb/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      }).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      setNewName("");
      setCreating(false);
      await reloadKbs();
      setSelId(d.id);
    } catch (e) {
      setErr(e.message || "创建失败");
    } finally {
      setCreateBusy(false);
    }
  };

  const pollJob = useCallback(
    (jobId) => {
      fetch(`/api/kb/job/${jobId}`)
        .then((r) => r.json())
        .then(async (j) => {
          if (j.error) throw new Error(j.error);
          setJob({ phase: j.phase, done: j.done || 0, total: j.total || 0 });
          if (j.status === "running") {
            pollRef.current = setTimeout(() => pollJob(jobId), 1500);
            return;
          }
          setJob(null);
          if (j.status === "error") {
            setErr("处理失败：" + (j.error || "未知错误"));
          } else if (j.result?.replaced) {
            setErr(null);
          }
          await reloadKbs();
          await loadDetail(selId);
        })
        .catch((e) => {
          setJob(null);
          setErr(e.message || "查询处理进度失败");
        });
    },
    [reloadKbs, loadDetail, selId]
  );

  const onPickFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !selId || busy) return;
    setErr(null);
    setJob({ phase: "上传中", done: 0, total: 0 });
    try {
      const fd = new FormData();
      fd.append("file", file);
      const d = await fetch(`/api/kb/${encodeURIComponent(selId)}/upload`, {
        method: "POST",
        body: fd,
      }).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      setJob({ phase: "解析文件", done: 0, total: 0 });
      pollJob(d.job_id);
    } catch (e) {
      setJob(null);
      setErr(e.message || "上传失败");
    }
  };

  const deleteDoc = async (filename) => {
    if (busy) return;
    if (!window.confirm(`确定从「${detail?.name}」删除文档 ${filename}？\n会同时删掉它产生的实体关系，并重建向量索引。`))
      return;
    setDeleting(filename);
    setErr(null);
    try {
      const d = await fetch(
        `/api/kb/${encodeURIComponent(selId)}/doc/${encodeURIComponent(filename)}`,
        { method: "DELETE" }
      ).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      await reloadKbs();
      await loadDetail(selId);
    } catch (e) {
      setErr(e.message || "删除失败");
    } finally {
      setDeleting(null);
    }
  };

  const docs = detail?.docs || [];
  const stats = detail?.stats || {};

  return (
    <div className="layout">
      <div className="main">
        <div className="topbar">
          <div className="topbar-left">
            <PageTabs tab={tab} onTab={onTab} />
          </div>
        </div>

        <div className="kb-page">
          <div className="kb-page-inner">
            {(err || kbError) && (
              <div className="kb-banner error">
                {err || kbError}
                <button onClick={() => setErr(null)}>×</button>
              </div>
            )}

            <div className="kb-grid">
              {/* 左：知识库列表 */}
              <div className="kb-list-col">
                <div className="kb-col-head">
                  <span>知识库</span>
                  {!creating && (
                    <button
                      className="kb-add-btn"
                      onClick={() => setCreating(true)}
                      disabled={busy}
                    >
                      <IconPlus /> 新建
                    </button>
                  )}
                </div>

                {creating && (
                  <div className="kb-create">
                    <input
                      autoFocus
                      placeholder="知识库名称"
                      value={newName}
                      maxLength={40}
                      onChange={(e) => setNewName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") createKb();
                        if (e.key === "Escape") {
                          setCreating(false);
                          setNewName("");
                        }
                      }}
                    />
                    <div className="kb-create-actions">
                      <button
                        className="btn-primary"
                        onClick={createKb}
                        disabled={!newName.trim() || createBusy}
                      >
                        {createBusy ? <Spinner /> : "创建"}
                      </button>
                      <button
                        className="btn-ghost"
                        onClick={() => {
                          setCreating(false);
                          setNewName("");
                        }}
                      >
                        取消
                      </button>
                    </div>
                  </div>
                )}

                <div className="kb-cards">
                  {kbs.map((k) => (
                    <button
                      key={k.id}
                      className={`kb-card ${k.id === selId ? "active" : ""}`}
                      onClick={() => setSelId(k.id)}
                    >
                      <div className="kb-card-name">{k.name}</div>
                      <div className="kb-card-meta">
                        {k.doc_count} 篇文档
                        {k.created_at ? ` · ${k.created_at.slice(0, 10)}` : ""}
                      </div>
                      {k.id === kbId && (
                        <span className="kb-card-badge">对话中</span>
                      )}
                    </button>
                  ))}
                  {!kbs.length && (
                    <div className="kb-muted">还没有知识库，点「新建」创建一个。</div>
                  )}
                </div>
              </div>

              {/* 右：选中库的文档 */}
              <div className="kb-detail-col">
                {!detail && loadingDetail && (
                  <div className="kb-muted">加载中…</div>
                )}
                {detail && (
                  <>
                    <div className="kb-detail-head">
                      <div>
                        <div className="kb-detail-name">{detail.name}</div>
                        <div className="kb-detail-stats">
                          {stats.documents ?? docs.length} 篇 ·{" "}
                          {(stats.entities || 0).toLocaleString()} 实体 ·{" "}
                          {(stats.relations || 0).toLocaleString()} 关系
                        </div>
                      </div>
                      <div className="kb-upload">
                        <input
                          ref={fileRef}
                          type="file"
                          accept=".md,.markdown,.txt,.pdf"
                          hidden
                          onChange={onPickFile}
                        />
                        <button
                          className="btn-primary"
                          onClick={() => fileRef.current?.click()}
                          disabled={busy}
                        >
                          <IconUpload /> 上传文档
                        </button>
                      </div>
                    </div>

                    {detail.legacy && (
                      <div className="kb-banner note">
                        这是从旧版本迁移来的库。旧文档的实体关系没有来源标记，
                        删除时只能移除它的向量、清不掉对应实体关系；
                        需要彻底重建可重新上传该文档。
                      </div>
                    )}

                    {job && (
                      <div className="kb-banner progress">
                        <Spinner />
                        <span>
                          {job.phase}
                          {job.phase === "提取实体关系" && job.total > 0
                            ? ` ${job.done}/${job.total} 块`
                            : "（建库较慢，几十秒到几分钟，别关页面）"}
                        </span>
                        {job.phase === "提取实体关系" && job.total > 0 && (
                          <span className="kb-progress-bar">
                            <i
                              style={{
                                width: `${Math.round(
                                  (job.done / job.total) * 100
                                )}%`,
                              }}
                            />
                          </span>
                        )}
                      </div>
                    )}

                    <div className="kb-doc-table">
                      <div className="kb-doc-row head">
                        <span>文件名</span>
                        <span>上传时间</span>
                        <span>块数</span>
                        <span />
                      </div>
                      {docs.map((d) => (
                        <div className="kb-doc-row" key={d.filename}>
                          <span className="kb-doc-name" title={d.filename}>
                            {d.filename}
                          </span>
                          <span className="kb-doc-time">
                            {d.uploaded_at || "—"}
                          </span>
                          <span className="kb-doc-chunks">
                            {d.chunks ?? "—"}
                          </span>
                          <button
                            className="kb-doc-del"
                            onClick={() => deleteDoc(d.filename)}
                            disabled={busy}
                            title="删除"
                          >
                            {deleting === d.filename ? <Spinner /> : <IconTrash />}
                          </button>
                        </div>
                      ))}
                      {!docs.length && (
                        <div className="kb-muted" style={{ padding: "16px 4px" }}>
                          这个库还没有文档。上传 .md 或 .pdf 开始建库。
                        </div>
                      )}
                    </div>
                  </>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
