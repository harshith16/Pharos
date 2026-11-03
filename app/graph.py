"""Knowledge graph page: entities as nodes, relations as edges.

Own code only: vanilla canvas drawing and a small force-directed layout
(nodes repel, edges act as springs, gentle damping). No external assets,
no third-party libraries, no copied artwork, logos, or icons — every
visual is a plain shape or text drawn here.

The page renders only real database rows. When there are no entities it
shows an honest empty state and draws nothing else — never sample or
demo nodes, never invented data.

If the engine tables are missing or any query fails, the data endpoint
returns empty lists instead of an error.
"""

import logging

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse

from app import config

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_NODES = 500
_MAX_EDGES = 1000


def _company_id(company: str | None) -> str:
    if isinstance(company, str) and company.strip():
        return company.strip()
    return config.COMPANY_ID


def _fetch_graph(company_id: str) -> dict:
    """Read real rows only. Any failure yields empty lists, never a 500.

    RLS scoping is explicit here: this is the server operator's
    local debug view, not a sender-facing surface, so it reads as
    CEO with the unlock on (the full real graph, for the demo).
    WhatsApp answers are unaffected — they scope per sender in
    app.whatsapp, where RLS still hides sensitive rows from
    senders without a code.
    """
    try:
        from app.db import scoped_conn

        with scoped_conn("ceo", unlocked=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, display_name, kind FROM engine_entities"
                    " WHERE company_id = %s ORDER BY created_at LIMIT %s",
                    (company_id, _MAX_NODES),
                )
                node_rows = cur.fetchall()
                cur.execute(
                    "SELECT src_id, dst_id, rel FROM engine_relations"
                    " WHERE company_id = %s ORDER BY created_at LIMIT %s",
                    (company_id, _MAX_EDGES),
                )
                edge_rows = cur.fetchall()
    except Exception:
        logger.exception("graph data unavailable")
        return {"nodes": [], "edges": []}
    nodes = []
    for row in node_rows:
        entity_id, name, display_name, kind = row[0], row[1], row[2], row[3]
        label = display_name or name or str(entity_id)
        nodes.append(
            {"id": str(entity_id), "label": label, "kind": kind or ""}
        )
    edges = []
    for row in edge_rows:
        src_id, dst_id, rel = row[0], row[1], row[2]
        if src_id is None or dst_id is None:
            continue
        edges.append(
            {"source": str(src_id), "target": str(dst_id), "label": rel or ""}
        )
    return {"nodes": nodes, "edges": edges}


@router.get("/graph/data")
async def graph_data(company: str | None = Query(default=None)):
    return JSONResponse(_fetch_graph(_company_id(company)))


@router.get("/graph")
async def graph_page():
    return HTMLResponse(_GRAPH_HTML)


_GRAPH_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Knowledge graph</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff;
    --panel: #f2f2f7;
    --text: #1c1c1e;
    --muted: #636366;
    --border: #e5e5ea;
    --accent: #007aff;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #000000;
      --panel: #1c1c1e;
      --text: #f2f2f7;
      --muted: #98989d;
      --border: #2c2c2e;
      --accent: #0a84ff;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--text);
    -webkit-font-smoothing: antialiased;
  }
  header {
    padding: 20px 24px 12px;
    max-width: 1100px;
    margin: 0 auto;
  }
  h1 { font-size: 28px; font-weight: 700; letter-spacing: -0.02em; margin: 0 0 4px; }
  .subtitle { color: var(--muted); font-size: 15px; margin: 0 0 12px; }
  .meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .pill {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 5px 12px;
    font-size: 13px;
    transition: background-color 0.2s ease, color 0.2s ease;
  }
  .pill strong { font-weight: 600; }
  #legend { display: flex; flex-wrap: wrap; gap: 8px; margin-left: 4px; }
  .legend-item { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); }
  .swatch { width: 10px; height: 10px; border-radius: 50%; flex: none; }
  .hint { font-size: 13px; color: var(--muted); }
  main { max-width: 1100px; margin: 0 auto; padding: 0 24px 24px; }
  .canvas-wrap {
    position: relative;
    height: 68vh;
    min-height: 420px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 16px;
    overflow: hidden;
    transition: background-color 0.2s ease;
  }
  canvas { display: block; width: 100%; height: 100%; cursor: grab; touch-action: none; }
  canvas:active { cursor: grabbing; }
  #empty {
    position: absolute; inset: 0;
    display: none;
    align-items: center; justify-content: center;
    text-align: center; padding: 24px;
  }
  #empty.visible { display: flex; }
  #empty .box { max-width: 420px; }
  #empty h2 { font-size: 20px; margin: 0 0 8px; }
  #empty p { color: var(--muted); font-size: 15px; line-height: 1.5; margin: 0; }
  .footer { color: var(--muted); font-size: 13px; margin-top: 12px; }
</style>
</head>
<body>
<header>
  <h1>Knowledge graph</h1>
  <p class="subtitle">Entities and how they connect, from your documents.</p>
  <div class="meta">
    <span class="pill"><strong id="node-count">0</strong> nodes</span>
    <span class="pill"><strong id="edge-count">0</strong> edges</span>
    <span id="legend" aria-label="Legend"></span>
  </div>
  <p class="hint">Drag a node to move it. Click a node to highlight its neighbours. Click empty space to clear.</p>
</header>
<main>
  <div class="canvas-wrap">
    <canvas id="graph" aria-label="Knowledge graph"></canvas>
    <div id="empty">
      <div class="box">
        <h2>No entities yet</h2>
        <p>Nothing has been extracted into the graph so far. Uploading documents grows the graph — entities and their relations will appear here automatically.</p>
      </div>
    </div>
  </div>
  <p class="footer" id="status">Loading the graph…</p>
</main>
<script>
(function () {
  'use strict';
  var canvas = document.getElementById('graph');
  var ctx = canvas.getContext('2d');
  var emptyEl = document.getElementById('empty');
  var nodeCountEl = document.getElementById('node-count');
  var edgeCountEl = document.getElementById('edge-count');
  var legendEl = document.getElementById('legend');
  var statusEl = document.getElementById('status');
  var params = new URLSearchParams(window.location.search);
  var company = params.get('company') || '';
  var dataUrl = company ? ('/graph/data?company=' + encodeURIComponent(company)) : '/graph/data';

  var PALETTE = ['#007aff', '#34c759', '#ff9f0a', '#af52de', '#ff375f', '#64d2ff', '#ff6482', '#30d158', '#ffd60a', '#5e5ce6'];
  function colorForKind(kind) {
    var h = 0, i;
    for (i = 0; i < kind.length; i++) { h = ((h * 31) + kind.charCodeAt(i)) >>> 0; }
    return PALETTE[h % PALETTE.length];
  }
  function isDark() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  var nodes = {};       // id -> {id, label, kind, x, y, vx, vy}
  var nodeOrder = [];   // ids, stable order for drawing
  var edges = [];       // {source, target, label}
  var selectedId = null;
  var neighbours = {};  // id -> true for neighbours of selectedId
  var dragging = null;
  var dragMoved = false;
  var dragStartX = 0, dragStartY = 0;
  var width = 0, height = 0, dpr = 1;

  function resize() {
    var rect = canvas.getBoundingClientRect();
    dpr = window.devicePixelRatio || 1;
    width = Math.max(100, rect.width);
    height = Math.max(100, rect.height);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  window.addEventListener('resize', resize);

  function placeNew(id) {
    // Spread fresh nodes around the centre so they do not pile up.
    var n = nodeOrder.length;
    var angle = (n * 2.399963) % (Math.PI * 2); // golden angle
    var radius = 40 + (n % 8) * 28;
    return {
      x: width / 2 + Math.cos(angle) * radius,
      y: height / 2 + Math.sin(angle) * radius
    };
  }

  function applyData(data) {
    var seen = {};
    var fresh = [];
    (data.nodes || []).forEach(function (n) {
      if (!n || n.id === undefined || n.id === null) return;
      var id = String(n.id);
      seen[id] = true;
      if (nodes[id]) {
        nodes[id].label = n.label || nodes[id].label;
        nodes[id].kind = n.kind || '';
      } else {
        var p = placeNew(id);
        nodes[id] = { id: id, label: n.label || id, kind: n.kind || '', x: p.x, y: p.y, vx: 0, vy: 0 };
        nodeOrder.push(id);
        fresh.push(id);
      }
    });
    // Drop nodes that no longer exist; keep every surviving position.
    nodeOrder = nodeOrder.filter(function (id) {
      if (!seen[id]) { delete nodes[id]; return false; }
      return true;
    });
    edges = [];
    (data.edges || []).forEach(function (e) {
      if (!e) return;
      var s = String(e.source), t = String(e.target);
      if (nodes[s] && nodes[t]) edges.push({ source: s, target: t, label: e.label || '' });
    });
    if (selectedId && !nodes[selectedId]) { selectedId = null; neighbours = {}; }
    if (selectedId) recomputeNeighbours();
    renderMeta();
  }

  function recomputeNeighbours() {
    neighbours = {};
    edges.forEach(function (e) {
      if (e.source === selectedId) neighbours[e.target] = true;
      if (e.target === selectedId) neighbours[e.source] = true;
    });
  }

  function renderMeta() {
    nodeCountEl.textContent = String(nodeOrder.length);
    edgeCountEl.textContent = String(edges.length);
    while (legendEl.firstChild) legendEl.removeChild(legendEl.firstChild);
    var kinds = {};
    nodeOrder.forEach(function (id) {
      var k = nodes[id].kind || 'entity';
      kinds[k] = true;
    });
    Object.keys(kinds).sort().forEach(function (k) {
      var item = document.createElement('span');
      item.className = 'legend-item';
      var dot = document.createElement('span');
      dot.className = 'swatch';
      dot.style.backgroundColor = colorForKind(k);
      var text = document.createElement('span');
      text.textContent = k;
      item.appendChild(dot);
      item.appendChild(text);
      legendEl.appendChild(item);
    });
    emptyEl.classList.toggle('visible', nodeOrder.length === 0);
    statusEl.textContent = nodeOrder.length === 0
      ? 'No entities yet — the graph updates automatically as documents are processed.'
      : 'Live — refreshes every 5 seconds. Positions are kept between refreshes.';
  }

  function step() {
    var i, j, a, b, dx, dy, dist, force, k;
    var cx = width / 2, cy = height / 2;
    // Repulsion between every pair.
    for (i = 0; i < nodeOrder.length; i++) {
      a = nodes[nodeOrder[i]];
      if (a === dragging) continue;
      for (j = i + 1; j < nodeOrder.length; j++) {
        b = nodes[nodeOrder[j]];
        dx = a.x - b.x; dy = a.y - b.y;
        dist = Math.sqrt(dx * dx + dy * dy) || 1;
        force = Math.min(2600 / (dist * dist), 6);
        a.vx += (dx / dist) * force;
        a.vy += (dy / dist) * force;
        if (b !== dragging) { b.vx -= (dx / dist) * force; b.vy -= (dy / dist) * force; }
      }
    }
    // Springs along edges.
    edges.forEach(function (e) {
      a = nodes[e.source]; b = nodes[e.target];
      if (!a || !b) return;
      dx = b.x - a.x; dy = b.y - a.y;
      dist = Math.sqrt(dx * dx + dy * dy) || 1;
      force = (dist - 130) * 0.012;
      if (a !== dragging) { a.vx += (dx / dist) * force; a.vy += (dy / dist) * force; }
      if (b !== dragging) { b.vx -= (dx / dist) * force; b.vy -= (dy / dist) * force; }
    });
    // Gentle pull to centre, damping, and bounds.
    for (i = 0; i < nodeOrder.length; i++) {
      a = nodes[nodeOrder[i]];
      if (a === dragging) { a.vx = 0; a.vy = 0; continue; }
      a.vx += (cx - a.x) * 0.0012;
      a.vy += (cy - a.y) * 0.0012;
      a.vx *= 0.86; a.vy *= 0.86;
      a.x = Math.max(28, Math.min(width - 28, a.x + a.vx));
      a.y = Math.max(28, Math.min(height - 28, a.y + a.vy));
    }
  }

  function drawLabel(text, x, y, dim) {
    var label = String(text);
    if (label.length > 28) label = label.slice(0, 27) + '…';
    ctx.globalAlpha = dim ? 0.35 : 0.92;
    ctx.font = '12px -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif';
    ctx.textAlign = 'center';
    ctx.fillStyle = isDark() ? '#f2f2f7' : '#1c1c1e';
    ctx.fillText(label, x, y);
    ctx.globalAlpha = 1;
  }

  function draw() {
    var dark = isDark();
    ctx.clearRect(0, 0, width, height);
    var edgeStroke = dark ? 'rgba(255,255,255,0.22)' : 'rgba(0,0,0,0.18)';
    var edgeText = dark ? 'rgba(255,255,255,0.45)' : 'rgba(0,0,0,0.45)';
    edges.forEach(function (e) {
      var a = nodes[e.source], b = nodes[e.target];
      if (!a || !b) return;
      var dim = selectedId && !(e.source === selectedId || e.target === selectedId);
      ctx.globalAlpha = dim ? 0.12 : 1;
      ctx.strokeStyle = edgeStroke;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      if (e.label && edges.length <= 200) {
        ctx.globalAlpha = dim ? 0.12 : 1;
        ctx.font = '10px -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif';
        ctx.textAlign = 'center';
        ctx.fillStyle = edgeText;
        ctx.fillText(String(e.label).slice(0, 24), (a.x + b.x) / 2, (a.y + b.y) / 2 - 4);
      }
      ctx.globalAlpha = 1;
    });
    nodeOrder.forEach(function (id) {
      var n = nodes[id];
      var dim = selectedId && id !== selectedId && !neighbours[id];
      var r = id === selectedId ? 13 : 10;
      ctx.globalAlpha = dim ? 0.3 : 1;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fillStyle = colorForKind(n.kind || 'entity');
      ctx.fill();
      ctx.lineWidth = id === selectedId ? 3 : 2;
      ctx.strokeStyle = dark ? '#000000' : '#ffffff';
      ctx.stroke();
      ctx.globalAlpha = 1;
      drawLabel(n.label, n.x, n.y + r + 16, dim);
    });
  }

  function loop() {
    step();
    draw();
    requestAnimationFrame(loop);
  }

  function nodeAt(x, y) {
    for (var i = nodeOrder.length - 1; i >= 0; i--) {
      var n = nodes[nodeOrder[i]];
      var dx = n.x - x, dy = n.y - y;
      if (Math.sqrt(dx * dx + dy * dy) <= 16) return n;
    }
    return null;
  }

  function canvasPos(evt) {
    var rect = canvas.getBoundingClientRect();
    return { x: evt.clientX - rect.left, y: evt.clientY - rect.top };
  }

  canvas.addEventListener('pointerdown', function (evt) {
    var p = canvasPos(evt);
    var n = nodeAt(p.x, p.y);
    dragMoved = false;
    dragStartX = p.x; dragStartY = p.y;
    if (n) {
      dragging = n;
      canvas.setPointerCapture(evt.pointerId);
    }
  });
  canvas.addEventListener('pointermove', function (evt) {
    if (!dragging) return;
    var p = canvasPos(evt);
    if (Math.abs(p.x - dragStartX) + Math.abs(p.y - dragStartY) > 4) dragMoved = true;
    dragging.x = Math.max(28, Math.min(width - 28, p.x));
    dragging.y = Math.max(28, Math.min(height - 28, p.y));
  });
  function endDrag(evt) {
    if (!dragging) return;
    var was = dragging;
    var p = canvasPos(evt);
    dragging = null;
    if (!dragMoved) {
      selectedId = was.id;
      recomputeNeighbours();
    } else if (Math.abs(p.x - dragStartX) + Math.abs(p.y - dragStartY) <= 4) {
      selectedId = null; neighbours = {};
    }
  }
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', function () { dragging = null; });
  canvas.addEventListener('click', function (evt) {
    // Clicks on empty space clear the selection (node clicks handled above).
    if (dragMoved) return;
    var p = canvasPos(evt);
    if (!nodeAt(p.x, p.y)) { selectedId = null; neighbours = {}; }
  });

  function refresh() {
    fetch(dataUrl, { headers: { 'Accept': 'application/json' } })
      .then(function (res) { return res.json(); })
      .then(function (data) { applyData(data || {}); })
      .catch(function () {
        // Keep the last good render; the next tick retries.
        statusEl.textContent = 'Could not refresh just now — keeping the last view, retrying.';
      });
  }

  resize();
  refresh();
  setInterval(refresh, 5000);
  requestAnimationFrame(loop);
})();
</script>
</body>
</html>
"""
