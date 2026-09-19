import { useRef, useEffect, useState, useMemo } from "react";
import ForceGraph2D from "react-force-graph-2d";

// 一次问答涉及的关系可能上百条，全画出来会糊成一团；截断到可读范围
const MAX_LINKS = 28;
const PANEL_HEIGHT = 380;

const C = {
  bg: "#fbfbfc",
  core: "170,170,255",       // #aaaaff（rgb，方便配 alpha）
  coreRing: "111,111,214",
  coreLabel: "90,90,184",
  node: "199,199,210",
  nodeLabel: "122,122,134",
  link: "200,200,216",
  linkLabel: "154,154,166",
  labelBg: "251,251,252",
};

const FADE_MS = 260;

export default function GraphPanel({ relations, nodes, onReady }) {
  const wrapRef = useRef(null);
  const fgRef = useRef(null);
  const [width, setWidth] = useState(600);
  const doneRef = useRef(false);
  const onReadyRef = useRef(onReady);
  onReadyRef.current = onReady;

  // 组件挂载即滚到底，让用户第一时间看到图谱
  useEffect(() => {
    onReadyRef.current?.();
  }, []);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const cr = entries[0].contentRect;
      setWidth(Math.max(280, Math.round(cr.width)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // 完整目标图（一次算好）
  const full = useMemo(() => {
    const rels = Array.isArray(relations) ? relations : [];
    const metaByName = new Map((nodes || []).map((n) => [n.name, n]));
    const isCore = (name) => !!metaByName.get(name)?.core;

    const sorted = [...rels].sort((a, b) => {
      const sa = (isCore(a.entity1) ? 1 : 0) + (isCore(a.entity2) ? 1 : 0);
      const sb = (isCore(b.entity1) ? 1 : 0) + (isCore(b.entity2) ? 1 : 0);
      return sb - sa;
    });
    const kept = sorted.slice(0, MAX_LINKS);

    const nodeMap = new Map();
    const addNode = (name) => {
      if (nodeMap.has(name)) return;
      const meta = metaByName.get(name) || {};
      nodeMap.set(name, { id: name, name, type: meta.type || "未知", core: !!meta.core });
    };
    const pairSeen = new Map();
    const links = kept.map((r) => {
      addNode(r.entity1);
      addNode(r.entity2);
      const key = [r.entity1, r.entity2].sort().join(" ");
      const n = pairSeen.get(key) || 0;
      pairSeen.set(key, n + 1);
      const curv = n === 0 ? 0 : (n % 2 ? 1 : -1) * 0.18 * Math.ceil(n / 2);
      return { source: r.entity1, target: r.entity2, label: r.relation, curv };
    });

    const allNodes = [...nodeMap.values()];
    allNodes.sort((a, b) => (b.core ? 1 : 0) - (a.core ? 1 : 0)); // 核心实体排前面
    return { nodes: allNodes, links, totalRel: rels.length, shown: kept.length };
  }, [relations, nodes]);

  // 渐进出现的可见子集
  const [viz, setViz] = useState({ nodes: [], links: [] });

  useEffect(() => {
    doneRef.current = false;
    setViz({ nodes: [], links: [] });
    full.nodes.forEach((n) => { delete n.__appearAt; });
    full.links.forEach((l) => { delete l.__appearAt; });

    const timers = [];
    const shownN = [];
    const shownL = [];
    const at = (fn, d) => timers.push(setTimeout(fn, d));
    const push = () =>
      setViz({ nodes: shownN.slice(), links: shownL.slice() });

    const cores = full.nodes.filter((n) => n.core);
    const rest = full.nodes.filter((n) => !n.core);

    let t = 40;
    // 1) 核心实体先一个个浮现
    cores.forEach((n) => {
      at(() => { n.__appearAt = performance.now(); shownN.push(n); push(); }, t);
      t += 90;
    });
    t += 120;
    // 2) 关联实体铺开
    const nStep = Math.max(14, Math.min(45, 620 / Math.max(1, rest.length)));
    rest.forEach((n) => {
      at(() => { n.__appearAt = performance.now(); shownN.push(n); push(); }, t);
      t += nStep;
    });
    t += 100;
    // 3) 连线逐条接上
    const lStep = Math.max(12, Math.min(38, 460 / Math.max(1, full.links.length)));
    full.links.forEach((l) => {
      at(() => { l.__appearAt = performance.now(); shownL.push(l); push(); }, t);
      t += lStep;
    });
    // 4) 收尾：标记完成 + 视图归中
    at(() => {
      doneRef.current = true;
      fgRef.current?.zoomToFit(500, 24);
      onReadyRef.current?.();
    }, t + 250);

    return () => timers.forEach(clearTimeout);
  }, [full]);

  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    fg.d3Force("charge")?.strength(-150);
    fg.d3Force("link")?.distance(58);
  }, [viz]);

  const alphaOf = (obj) => {
    if (!obj.__appearAt) return 1;
    return Math.max(0.06, Math.min(1, (performance.now() - obj.__appearAt) / FADE_MS));
  };

  const coreCount = full.nodes.filter((n) => n.core).length;

  return (
    <div className="graph-panel">
      <div className="graph-panel-head">
        <span className="graph-title">知识图谱</span>
        <span className="graph-caption">
          {full.nodes.length} 个实体 · {full.shown}
          {full.totalRel > full.shown ? ` / ${full.totalRel}` : ""} 条关系
          {coreCount > 0 ? ` · 核心实体 ${coreCount}` : ""}
        </span>
      </div>

      <div className="graph-canvas" ref={wrapRef}>
        <ForceGraph2D
          ref={fgRef}
          width={width}
          height={PANEL_HEIGHT}
          graphData={viz}
          backgroundColor={C.bg}
          cooldownTicks={140}
          d3AlphaDecay={0.04}
          onEngineStop={() => {
            if (doneRef.current) fgRef.current?.zoomToFit(400, 24);
          }}
          nodeRelSize={5}
          linkCurvature="curv"
          linkWidth={() => 1}
          linkColor={(l) => `rgba(${C.link},${alphaOf(l)})`}
          linkDirectionalArrowLength={3}
          linkDirectionalArrowRelPos={1}
          linkDirectionalArrowColor={(l) => `rgba(${C.link},${alphaOf(l)})`}
          nodeLabel={(n) => `${n.name}（${n.type}）`}
          nodeCanvasObject={(node, ctx, scale) => {
            const a = alphaOf(node);
            const r = (node.core ? 6 : 4) * (0.6 + 0.4 * a); // 出现时略微放大到位
            ctx.save();
            ctx.globalAlpha = a;
            ctx.beginPath();
            ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
            ctx.fillStyle = node.core ? `rgb(${C.core})` : `rgb(${C.node})`;
            ctx.fill();
            if (node.core) {
              ctx.lineWidth = 1.8 / scale;
              ctx.strokeStyle = `rgb(${C.coreRing})`;
              ctx.stroke();
            }
            const fs = node.core ? 4.6 : 3.6;
            ctx.font = `${node.core ? "700 " : ""}${fs}px -apple-system, "Microsoft YaHei", sans-serif`;
            ctx.textAlign = "center";
            ctx.textBaseline = "top";
            ctx.fillStyle = node.core ? `rgb(${C.coreLabel})` : `rgb(${C.nodeLabel})`;
            const label = node.name.length > 12 ? node.name.slice(0, 12) + "…" : node.name;
            ctx.fillText(label, node.x, node.y + r + 1.5);
            ctx.restore();
          }}
          linkCanvasObjectMode={() => "after"}
          linkCanvasObject={(link, ctx, scale) => {
            if (scale < 0.9) return;
            const s = link.source;
            const t = link.target;
            if (typeof s !== "object" || typeof t !== "object") return;
            const label = link.label || "";
            if (!label) return;
            const a = alphaOf(link);
            if (a < 0.35) return;
            const mx = s.x + (t.x - s.x) / 2;
            const my = s.y + (t.y - s.y) / 2;
            const fs = 3;
            ctx.font = `${fs}px -apple-system, "Microsoft YaHei", sans-serif`;
            const w = ctx.measureText(label).width;
            ctx.fillStyle = `rgba(${C.labelBg},${0.92 * a})`;
            ctx.fillRect(mx - w / 2 - 1, my - fs / 2 - 0.6, w + 2, fs + 1.2);
            ctx.fillStyle = `rgba(${C.linkLabel},${a})`;
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText(label, mx, my);
          }}
        />
      </div>

      <div className="graph-legend">
        <span><i className="lg-dot core" /> 当前问题的核心实体</span>
        <span><i className="lg-dot" /> 关联实体</span>
        <span className="graph-hint">滚轮缩放 · 拖动平移 · 悬停看类型</span>
      </div>
    </div>
  );
}
