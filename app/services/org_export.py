# -*- coding: utf-8 -*-
"""Standalone, self-contained HTML export of the org tree (עץ ארגוני לשיתוף).

Produces a single HTML file (CSS + JS inlined) that renders the chart offline in
any browser — no login, no server. For sharing safety it embeds ONLY names,
titles and the manager flag; secret magic-link tokens and phone numbers are never
included.
"""
from __future__ import annotations

import html
from datetime import datetime


def _node(n: dict) -> str:
    cls = "node mgr" if n.get("is_manager") else "node"
    name = html.escape(n.get("name") or "")
    title = n.get("title") or ""
    title_html = f'<span class="tt">{html.escape(title)}</span>' if title else ""
    out = f'<div class="{cls}"><span class="nm">{name}</span>{title_html}</div>'
    kids = n.get("children") or []
    if kids:
        out += "<ul>" + "".join(f"<li>{_node(k)}</li>" for k in kids) + "</ul>"
    return out


def render_html(forest: list[dict], dept: str = "") -> str:
    trees = "".join(f'<ul class="tree"><li>{_node(r)}</li></ul>' for r in forest) \
        or '<p style="text-align:center;color:#64748b">אין נתונים בעץ.</p>'
    stamp = datetime.now().strftime("%d/%m/%Y %H:%M")
    dept_txt = f" — {html.escape(dept)}" if dept else ""
    return (_TEMPLATE
            .replace("__TREES__", trees)
            .replace("__DATE__", stamp)
            .replace("__DEPT__", dept_txt))


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>עץ ארגוני — ארקיע</title>
<style>
  * { box-sizing:border-box }
  body{ font-family: system-ui, "Segoe UI", Arial, sans-serif; margin:0; color:#0f172a; background:#f8fafc }
  .page{ padding:16px 18px 40px }
  .topbar{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:10px }
  .topbar h1{ font-size:20px; margin:0; color:#123a86 }
  .ctl{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-inline-start:auto }
  .ctl button{ background:#fff; border:1px solid #cbd5e1; border-radius:8px; padding:7px 11px;
    cursor:pointer; font-size:13px; color:#334155 }
  .segmented{ display:inline-flex; border:1px solid #cbd5e1; border-radius:9px; overflow:hidden }
  .segmented button{ border:0; border-radius:0 }
  .segmented button.on{ background:#1e63b8; color:#fff }
  .note{ color:#94a3b8; font-size:12px; margin:0 0 8px }

  #forest{ position:relative; overflow:hidden; padding-top:8px }
  #chartInner{ display:inline-block; transform-origin:top left }
  .chartpad{ height:20px }
  .tree, .tree ul{ list-style:none; margin:0; padding:0 }
  .node{ position:relative; background:#fff; border:1px solid #e2e8f0; border-radius:10px;
    padding:8px 14px; box-shadow:0 1px 2px rgba(0,0,0,.05) }
  .node.mgr{ border-color:#bfdbfe; background:#f7fbff }
  .node .nm{ font-weight:600; color:#0f172a; white-space:nowrap }
  .node .tt{ color:#64748b; font-size:12px; white-space:nowrap }

  /* horizontal: top-down chart */
  .mode-h .tree ul{ display:flex; justify-content:center; position:relative; padding-top:22px }
  .mode-h .tree li{ position:relative; text-align:center; padding:22px 8px 0 8px }
  .mode-h .tree li::before, .mode-h .tree li::after{ content:''; position:absolute; top:0; right:50%;
    width:50%; height:22px; border-top:2px solid #cbd5e1 }
  .mode-h .tree li::after{ right:auto; left:50%; border-left:2px solid #cbd5e1 }
  .mode-h .tree li:first-child::before, .mode-h .tree li:last-child::after{ border:0 }
  .mode-h .tree li:last-child::before{ border-right:2px solid #cbd5e1; border-radius:0 6px 0 0 }
  .mode-h .tree li:first-child::after{ border-radius:6px 0 0 0 }
  .mode-h .tree ul ul::before{ content:''; position:absolute; top:0; left:50%; width:2px; height:22px; background:#cbd5e1 }
  .mode-h .tree li:only-child{ padding-top:0 }
  .mode-h .tree li:only-child::before, .mode-h .tree li:only-child::after{ display:none }
  .mode-h .node{ display:inline-block; text-align:center; min-width:120px }
  .mode-h .node .nm, .mode-h .node .tt{ display:block }
  .mode-h .node .tt{ margin-top:1px }

  /* vertical: indented list with elbow connectors */
  .mode-v #chartInner{ transform:none !important }
  .mode-v .tree ul{ margin-right:26px }
  .mode-v .tree li{ position:relative; padding:7px 26px 0 0; text-align:right }
  .mode-v .tree li::before{ content:''; position:absolute; top:0; right:0; width:2px; height:100%; background:#cbd5e1 }
  .mode-v .tree li::after{ content:''; position:absolute; top:24px; right:0; width:22px; height:2px; background:#cbd5e1 }
  .mode-v .tree li:last-child::before{ height:24px }
  .mode-v .tree > li{ padding:0 }
  .mode-v .tree > li::before, .mode-v .tree > li::after{ display:none }
  .mode-v .node{ display:inline-flex; align-items:center; gap:8px }

  @media print {
    @page { margin: 10mm }
    .topbar, .note { display:none !important }
    #forest{ overflow:visible !important }
    .node{ box-shadow:none !important }
    body{ background:#fff !important }
    * { -webkit-print-color-adjust:exact; print-color-adjust:exact }
  }
</style>
</head>
<body>
<div class="page">
  <div class="topbar">
    <h1>🌳 עץ ארגוני</h1>
    <div class="ctl">
      <span class="segmented">
        <button id="viewH" class="on" onclick="setView('h')">↔ אופקי</button>
        <button id="viewV" onclick="setView('v')">↕ אנכי</button>
      </span>
      <span id="zoomctl">
        <button onclick="zoom(1/1.15)">➖</button>
        <button onclick="fitChart()">⤢ התאם למסך</button>
        <button onclick="zoom(1.15)">➕</button>
      </span>
      <button onclick="window.print()">🖨️ הדפסה / PDF</button>
    </div>
  </div>
  <p class="note">הופק ממערכת התמחיר של ארקיע · __DATE____DEPT__</p>
  <div id="forest" class="mode-h"><div id="chartInner">__TREES__<div class="chartpad"></div></div></div>
</div>
<script>
let viewMode = 'h', natW = 1, natH = 1, scale = 1, fitScale = 1;
function el(id){ return document.getElementById(id); }
function setView(mode){
  viewMode = (mode === 'v') ? 'v' : 'h';
  const forest = el('forest'), inner = el('chartInner');
  forest.classList.toggle('mode-h', viewMode === 'h');
  forest.classList.toggle('mode-v', viewMode === 'v');
  el('viewH').classList.toggle('on', viewMode === 'h');
  el('viewV').classList.toggle('on', viewMode === 'v');
  el('zoomctl').style.display = viewMode === 'h' ? '' : 'none';
  if (viewMode === 'v'){ if (inner) inner.style.transform = 'none';
    forest.style.height = 'auto'; forest.style.overflow = 'visible'; }
  else fitChart();
}
function measure(){
  const forest = el('forest'), inner = el('chartInner'); if (!inner) return false;
  inner.style.transform = 'none'; forest.style.height = 'auto';
  natW = inner.scrollWidth || 1; natH = inner.scrollHeight || 1; return true;
}
function applyScale(){
  const forest = el('forest'), inner = el('chartInner'); if (!inner) return;
  const availW = forest.clientWidth, scaledW = natW * scale;
  const overflowing = scaledW > availW + 1;
  const offsetX = overflowing ? 0 : Math.max(0, (availW - scaledW) / 2);
  inner.style.transform = 'translateX(' + offsetX + 'px) scale(' + scale + ')';
  forest.style.height = (natH * scale) + 'px';
  forest.style.overflow = overflowing ? 'auto' : 'hidden';
}
function fitChart(){
  if (viewMode !== 'h' || !measure()) return;
  const forest = el('forest');
  const availW = forest.clientWidth;
  const availH = window.innerHeight - forest.getBoundingClientRect().top - 20;
  fitScale = Math.min(availW / natW, availH / natH, 1);
  if (!isFinite(fitScale) || fitScale <= 0) fitScale = 1;
  scale = fitScale; applyScale();
}
function zoom(f){ if (!measure()) return; scale = Math.max(0.1, Math.min(1, scale * f)); applyScale(); }
window.addEventListener('resize', function(){ if (viewMode === 'h' && scale <= fitScale + 0.001) fitChart(); });
setView('h');
setTimeout(fitChart, 60);
</script>
</body>
</html>
"""
