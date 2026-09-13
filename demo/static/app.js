"use strict";

// Colors are read from the CSS custom properties in style.css so the graph
// palette stays in sync with the chrome (and follows light/dark mode) —
// canvas rendering (vis-network) can't resolve var() itself, so we resolve
// once at load time.
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const TYPE_COLOR = {
  File: cssVar("--node-file"),
  Artifact: cssVar("--node-artifact"),
  Text: cssVar("--node-text"),
  Table: cssVar("--node-table"),
  Image: cssVar("--node-image"),
  Group: cssVar("--node-group"),
};

// Fallback only — actual node depth is computed per graph from PARENT_OF edges
// (see computeLevels), because `propose_edges`'s HEADING_PARENT role/
// `propose_synthetic_groups` can reparent/insert a node one level deeper than the normal Artifact ->
// content depth. A static per-type table can't reflect that. Group defaults
// to the same level as its usual members (Text) since it's inserted between
// their old parent and them.
const TYPE_LEVEL_FALLBACK = { File: 0, Artifact: 1, Text: 2, Table: 2, Image: 2, Group: 2 };

// Walk PARENT_OF from the File root to assign each node's real depth in the
// current graph, so a reparented node (heading Text -> Table/Image) renders
// one level below its new parent instead of sharing its old, now-stale level.
function computeLevels(nodes, edges) {
  const childrenBySource = new Map();
  for (const e of edges) {
    if (e.type !== "PARENT_OF") continue;
    if (!childrenBySource.has(e.source_id)) childrenBySource.set(e.source_id, []);
    childrenBySource.get(e.source_id).push(e.target_id);
  }
  const levels = new Map();
  const root = nodes.find((n) => n.type === "File");
  if (root) {
    const queue = [[root.id, 0]];
    while (queue.length) {
      const [id, lvl] = queue.shift();
      if (levels.has(id)) continue;
      levels.set(id, lvl);
      for (const childId of childrenBySource.get(id) || []) queue.push([childId, lvl + 1]);
    }
  }
  for (const n of nodes) {
    if (!levels.has(n.id)) levels.set(n.id, TYPE_LEVEL_FALLBACK[n.type] ?? 2);
  }
  return levels;
}

const EDGE_STYLE = {
  PARENT_OF: { color: cssVar("--edge-parent"), dashes: false, width: 1 },
  NEXT: { color: cssVar("--edge-next"), dashes: [5, 3], width: 2 },
  CAPTION_OF: { color: cssVar("--edge-caption"), dashes: [2, 2], width: 1.5 },
  REFERENCES: { color: cssVar("--edge-references"), dashes: [1, 3], width: 1.5 },
};

const state = {
  nodesById: new Map(),
  currentEdges: [],
  network: null,
  activeSessionId: null,
};

const $ = (sel) => document.querySelector(sel);

const fileInput = $("#fileInput");
const vlmToggle = $("#vlmToggle");
const groupsToggle = $("#groupsToggle");
const apiKeyInput = $("#apiKeyInput");
const vlmHint = $("#vlmHint");
const fileMeta = $("#fileMeta");
const statusArea = $("#statusArea");
const summaryBar = $("#summaryBar");
const legend = $("#legend");
const graphEmpty = $("#graphEmpty");
const zoomControls = $("#zoomControls");
const drawer = $("#detailDrawer");
const detailContent = $("#detailContent");
const sessionListEl = $("#sessionList");
const sessionCountEl = $("#sessionCount");

// A large document (hundreds of nodes) shrinks the scale down to around 0.1
// to fit everything on screen, making it effectively impossible to click an
// individual node precisely, especially a small Image node (confirmed:
// clicks kept missing at scale=0.1048) — explicit zoom-in/zoom-out/fit
// buttons let you zoom in even without knowing about scroll-zoom.
const ZOOM_STEP = 1.3;
$("#zoomIn").addEventListener("click", () => {
  if (!state.network) return;
  state.network.moveTo({ scale: state.network.getScale() * ZOOM_STEP, animation: { duration: 150 } });
});
$("#zoomOut").addEventListener("click", () => {
  if (!state.network) return;
  state.network.moveTo({ scale: state.network.getScale() / ZOOM_STEP, animation: { duration: 150 } });
});
$("#zoomFit").addEventListener("click", () => {
  if (!state.network) return;
  state.network.fit({ animation: { duration: 300 } });
});

const API_KEY_STORAGE_KEY = "articling_openai_api_key"; // sessionStorage only — gone when the tab closes, sent to the server only with each upload request

let serverHasKey = false; // before the /api/status response arrives, assuming "it's probably there" is less annoying than "unknown" (keeps the checkbox unlocked)

// Updates the "VLM enrichment"/"Synthetic groups" checkboxes' enabled state
// + hint text to reflect whether they're actually usable (the server has a
// key, or one was entered in the browser) — it used to be that you'd only
// see the "skipped, no key" warning after the upload finished, but now you
// know right away, before checking the box. Both use the same key, so
// availability is judged once and shared.
function refreshVlmAvailability() {
  const hasKey = serverHasKey || apiKeyInput.value.trim().length > 0;
  for (const toggle of [vlmToggle, groupsToggle]) {
    toggle.disabled = !hasKey;
    if (!hasKey) toggle.checked = false;
  }
  vlmHint.hidden = hasKey;
  if (!hasKey) vlmHint.textContent = "Needs an OpenAI API key — enter one below, or set OPENAI_API_KEY on the server.";
}

(async function initApiKeyUi() {
  apiKeyInput.value = sessionStorage.getItem(API_KEY_STORAGE_KEY) || "";
  try {
    const resp = await fetch("/api/status");
    const data = await resp.json();
    serverHasKey = !!data.openai_key_configured;
  } catch {
    serverHasKey = false; // even if the status check itself fails, keep uploads working — just treat it as "no server key"
  }
  apiKeyInput.hidden = serverHasKey; // don't show the input field at all if the server already has one (avoid asking twice)
  refreshVlmAvailability();
})();

apiKeyInput.addEventListener("input", () => {
  const value = apiKeyInput.value.trim();
  if (value) sessionStorage.setItem(API_KEY_STORAGE_KEY, value);
  else sessionStorage.removeItem(API_KEY_STORAGE_KEY);
  refreshVlmAvailability();
});

fileInput.addEventListener("change", () => {
  const f = fileInput.files[0];
  if (f) uploadFile(f);
});

$("#closeDrawer").addEventListener("click", closeDrawer);

$("#sidebarToggle").addEventListener("click", () => {
  $("#sessionSidebar").classList.toggle("collapsed");
});

// drag & drop onto the whole page
["dragover", "drop"].forEach((evt) =>
  document.addEventListener(evt, (e) => e.preventDefault())
);
document.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) uploadFile(f);
});

function setStatus(text, kind) {
  statusArea.textContent = text;
  statusArea.className = kind || "";
}

// graphEmpty starts as a static hint (the empty state), but during an
// upload that spot is swapped for a loading indicator and restored once
// it's done — the original HTML is remembered just once.
const EMPTY_HINT_HTML = graphEmpty.innerHTML;

function showLoadingHint(text) {
  graphEmpty.innerHTML = `<div><div class="spinner"></div><p>${text}</p></div>`;
  graphEmpty.hidden = false;
  zoomControls.hidden = true;
}

function restoreEmptyHint() {
  graphEmpty.innerHTML = EMPTY_HINT_HTML;
  // if a graph was already successfully drawn before (a re-upload failed),
  // keep showing that graph; if not (the first upload failed), show the hint.
  const hasGraph = state.network !== null;
  graphEmpty.hidden = hasGraph;
  zoomControls.hidden = !hasGraph;
}

async function uploadFile(file) {
  const withVlm = vlmToggle.checked;
  const withGroups = groupsToggle.checked;
  fileMeta.textContent = `${file.name} · ${(file.size / 1e6).toFixed(1)}MB`;
  const steps = ["building graph"];
  if (withVlm) steps.push("VLM enrichment (headings + relations)");
  if (withGroups) steps.push("synthetic groups");
  setStatus(`Extracting… (${steps.join(" + ")})`, "loading");
  showLoadingHint(withVlm || withGroups ? "Extracting and running VLM enrichment…" : "Extracting…");
  closeDrawer();

  const form = new FormData();
  form.append("file", file);
  form.append("vlm_enrichment", withVlm ? "true" : "false");
  form.append("synthetic_groups", withGroups ? "true" : "false");
  if ((withVlm || withGroups) && !serverHasKey && apiKeyInput.value.trim()) {
    form.append("openai_api_key", apiKeyInput.value.trim()); // sent with only this one request — never stored by the server (see app.py)
  }

  let resp;
  try {
    resp = await fetch("/api/extract", { method: "POST", body: form });
  } catch (err) {
    setStatus(`Could not reach the server: ${err}`, "error");
    restoreEmptyHint();
    return;
  }

  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    setStatus(`Failed: ${body.detail || resp.statusText}`, "error");
    restoreEmptyHint();
    return;
  }

  const data = await resp.json();
  renderSummary(data);
  renderGraph(data);
  // The server has already saved this result as-is to
  // demo/.sessions/<id>/result.json — keeping the session id in the URL
  // lets a reload/bookmark/share load the same result again with no
  // re-extraction (see loadSession). Since a non-deterministic step like
  // VLM enrichment mixed in can change the result on re-extraction, being
  // able to pin and revisit "that exact result" is what makes
  // capture/annotation artifacts reliably debuggable.
  history.replaceState(null, "", `?session=${data.session_id}`);
  // With the same filename, the server may have overwritten an existing
  // session (app.py's _find_existing_session_id), which can change the
  // list's order/content — re-fetch to bring the sidebar up to date.
  refreshSessionList(data.session_id);

  const warnCount = data.warnings?.length || 0;
  setStatus(
    `Done — ${data.elapsed_sec}s${warnCount ? ` · ${warnCount} warning(s), see summary` : ""}`,
    warnCount ? "loading" : "ok"
  );
}

// Loads a saved session's result as-is, with no re-extraction (GET
// /api/session/{id} — see app.py). Called automatically if the URL has
// `?session=<id>` on page load, and a just-extracted result also leaves
// this id in the URL once uploadFile finishes, so a reload lands back here
// too.
async function loadSession(sessionId) {
  setStatus(`Loading saved session ${sessionId}…`, "loading");
  showLoadingHint("Loading saved session…");
  let resp;
  try {
    resp = await fetch(`/api/session/${sessionId}`);
  } catch (err) {
    setStatus(`Could not reach the server: ${err}`, "error");
    restoreEmptyHint();
    return;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    setStatus(`Failed to load session ${sessionId}: ${body.detail || resp.statusText}`, "error");
    restoreEmptyHint();
    return;
  }
  const data = await resp.json();
  fileMeta.textContent = `${data.filename} · saved session ${sessionId}`;
  renderSummary(data);
  renderGraph(data);
  setActiveSessionItem(sessionId);
  const warnCount = data.warnings?.length || 0;
  setStatus(
    `Loaded saved result — ${data.elapsed_sec}s${warnCount ? ` · ${warnCount} warning(s), see summary` : ""}`,
    warnCount ? "loading" : "ok"
  );
}

const initialSessionId = new URLSearchParams(location.search).get("session");
if (initialSessionId) loadSession(initialSessionId);

// ---------- session sidebar ----------

// Remembers the initial HTML's "no saved sessions" hint, to show again
// when the list is empty (no session on the server has a result.json).
const SESSION_LIST_EMPTY_HTML = sessionListEl.innerHTML;

// Refetches `GET /api/sessions` and redraws the sidebar with the latest
// state — called at every point where a new extraction or an overwrite
// (same filename) could change the list/ordering (initial load, right
// after an upload finishes). A plain click switching sessions doesn't
// change the list itself, so that just uses setActiveSessionItem with no
// refetch.
async function refreshSessionList(activeId) {
  let items = [];
  try {
    const resp = await fetch("/api/sessions");
    if (resp.ok) items = await resp.json();
  } catch {
    // silently ignore a listing failure — the sidebar just stays empty;
    // an upload or a direct load via URL's ?session= keeps working
    // regardless of this list.
  }
  renderSessionList(items, activeId ?? state.activeSessionId);
}

function renderSessionList(items, activeId) {
  sessionCountEl.textContent = items.length ? `(${items.length})` : "";
  if (!items.length) {
    sessionListEl.innerHTML = SESSION_LIST_EMPTY_HTML;
    return;
  }
  // The server already sorts by most-recent (app.py's /api/sessions), so
  // this defensively just trusts that order and renders it.
  // Each row places two buttons side by side (open session / delete) — a
  // <button> can't contain a <button> (nested interactive content is
  // invalid), so a container wrapping .session-row is used instead, and
  // the highlight (active) state is applied to the whole row too (see
  // style.css).
  sessionListEl.innerHTML = items
    .map((item) => {
      const isActive = item.session_id === activeId;
      const time = new Date(item.mtime * 1000).toLocaleString();
      const id = escapeHtml(item.session_id);
      return `<div class="session-row${isActive ? " active" : ""}" data-session-id="${id}">
        <button type="button" class="session-item" data-session-id="${id}" title="${escapeHtml(item.filename)}">
          <span class="title">${escapeHtml(item.filename)}</span>
          <span class="time">${escapeHtml(time)}</span>
        </button>
        <button type="button" class="session-delete" data-session-id="${id}" title="Delete this session" aria-label="Delete this session">×</button>
      </div>`;
    })
    .join("");
}

// Attaches the listener once to the list container rather than each
// individual button (event delegation), so it keeps working even when the
// list is redrawn (its innerHTML replaced by a refetch).
sessionListEl.addEventListener("click", (e) => {
  const deleteBtn = e.target.closest(".session-delete");
  if (deleteBtn) {
    deleteSession(deleteBtn.dataset.sessionId);
    return;
  }
  const btn = e.target.closest(".session-item");
  if (!btn) return;
  const sessionId = btn.dataset.sessionId;
  if (sessionId) {
    history.replaceState(null, "", `?session=${sessionId}`);
    loadSession(sessionId);
  }
});

// Deletion is irreversible (wipes the session directory wholesale from
// disk, app.py's delete_session), so a confirmation dialog comes first. If
// the currently viewed session was deleted, the graph on the right / the
// URL are cleared too, so a vanished session doesn't stay on screen.
async function deleteSession(sessionId) {
  const row = sessionListEl.querySelector(`.session-row[data-session-id="${CSS.escape(sessionId)}"]`);
  const filename = row?.querySelector(".title")?.textContent || sessionId;
  if (!confirm(`Delete saved session "${filename}"? This cannot be undone.`)) return;

  let resp;
  try {
    resp = await fetch(`/api/session/${sessionId}`, { method: "DELETE" });
  } catch (err) {
    alert(`Could not reach the server: ${err}`);
    return;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    alert(`Failed to delete: ${body.detail || resp.statusText}`);
    return;
  }

  const wasActive = state.activeSessionId === sessionId;
  if (wasActive) state.activeSessionId = null; // clear it before redrawing the list, so the deleted session doesn't stay highlighted
  await refreshSessionList();
  if (wasActive) {
    history.replaceState(null, "", location.pathname);
    fileMeta.textContent = "";
    setStatus("Deleted saved session", "ok");
    summaryBar.hidden = true;
    legend.hidden = true;
    state.nodesById = new Map();
    state.currentEdges = [];
    if (state.network) {
      state.network.destroy();
      state.network = null;
    }
    closeDrawer();
    restoreEmptyHint();
  }
}

// Reflects just the current selection without refetching the list (e.g.
// right after a click) — if the list itself hasn't been drawn yet (e.g. the
// /api/sessions response is slow), this is silently a no-op, and
// refreshSessionList later draws it reflecting activeId.
function setActiveSessionItem(sessionId) {
  state.activeSessionId = sessionId;
  for (const el of sessionListEl.querySelectorAll(".session-row")) {
    el.classList.toggle("active", el.dataset.sessionId === sessionId);
  }
}

refreshSessionList(initialSessionId || null);

// ---------- summary bar ----------

function renderSummary(data) {
  const { node_counts, edge_counts, invariant_violations } = data.summary;
  const chips = [];

  for (const [type, count] of Object.entries(node_counts)) {
    if (count > 0) chips.push(`<span class="chip"><i class="dot" style="width:8px;height:8px;background:${TYPE_COLOR[type]}"></i>${type} <b>${count}</b></span>`);
  }
  for (const [type, count] of Object.entries(edge_counts)) {
    chips.push(`<span class="chip">${type} <b>${count}</b></span>`);
  }
  chips.push(`<span class="chip">${data.elapsed_sec}s</span>`);

  if (invariant_violations.length) {
    chips.push(`<span class="chip err">${invariant_violations.length} invariant violation(s)</span>`);
  } else {
    chips.push(`<span class="chip good">Graph invariants passed</span>`);
  }
  for (const w of data.warnings || []) {
    // not truncated: API error text (e.g. "Error code: 429 - {...}") is exactly
    // what you need to diagnose a failed relation proposal — hiding it defeats
    // the point of showing a warning at all.
    chips.push(`<span class="chip warn">${escapeHtml(w)}</span>`);
  }

  summaryBar.innerHTML = chips.join("");
  summaryBar.hidden = false;
  legend.hidden = false;
}

// ---------- graph ----------

function renderGraph(data) {
  graphEmpty.hidden = true;
  zoomControls.hidden = false;
  state.nodesById = new Map(data.nodes.map((n) => [n.id, n]));
  state.currentEdges = data.edges;

  const levels = computeLevels(data.nodes, data.edges);
  const visNodes = data.nodes.map((n) => nodeToVis(n, levels.get(n.id)));
  const visEdges = data.edges.map((e, i) => edgeToVis(e, i));

  const container = $("#graph");
  const nodesDS = new vis.DataSet(visNodes);
  const edgesDS = new vis.DataSet(visEdges);

  if (state.network) state.network.destroy();
  state.network = new vis.Network(
    container,
    { nodes: nodesDS, edges: edgesDS },
    {
      layout: {
        hierarchical: {
          enabled: true,
          direction: "UD",
          sortMethod: "directed",
          levelSeparation: 120,
          nodeSpacing: 80,
          treeSpacing: 130,
          parentCentralization: true,
        },
      },
      physics: {
        hierarchicalRepulsion: { nodeDistance: 90, springLength: 100 },
        minVelocity: 0.9,
        stabilization: { iterations: 200 },
      },
      interaction: { hover: true, tooltipDelay: 150 },
      nodes: {
        font: { size: 12, face: "Pretendard, sans-serif" },
        shapeProperties: { borderRadius: 0 }, // flat corners on box nodes, matches the chrome
      },
      edges: { smooth: { type: "cubicBezier", forceDirection: "vertical", roundness: 0.5 } },
    }
  );

  state.network.on("click", (params) => {
    if (params.nodes.length) showDetail(params.nodes[0]);
    else closeDrawer();
  });
}

function nodeToVis(n, level) {
  const color = TYPE_COLOR[n.type] || "#8c8c8c";
  const isDot = n.type === "Text";
  // --node-group is the (light) warning yellow — white label text on it fails
  // contrast the way it doesn't on the other three (dark) box colors.
  const labelColor = n.type === "Group" ? cssVar("--ink") : cssVar("--on-primary");
  return {
    id: n.id,
    label: labelFor(n),
    level: level ?? TYPE_LEVEL_FALLBACK[n.type] ?? 2,
    shape: isDot ? "dot" : "box",
    size: isDot ? 6 : undefined,
    color: { background: color, border: color, highlight: { background: color, border: cssVar("--ink") } },
    font: isDot ? { size: 0 } : { color: labelColor, size: 11 },
    margin: 6,
    borderWidth: 1,
  };
}

function labelFor(n) {
  const p = n.properties || {};
  if (n.type === "File") return truncate(n.name, 24);
  if (n.type === "Artifact") return truncate(n.name, 18);
  if (n.type === "Table") {
    const grid = p.grid;
    const dims = Array.isArray(grid) ? `${grid.length}×${grid[0]?.length || 0}` : "";
    return `Table ${dims}`;
  }
  if (n.type === "Image") return "Image";
  if (n.type === "Group") return `Group${p.group_type ? `: ${p.group_type}` : ""}`;
  // Text
  const t = p.text || p.nearby_text || n.name || "";
  return truncate(t, 16);
}

function edgeToVis(e, i) {
  const style = EDGE_STYLE[e.type] || { color: "#8c8c8c", dashes: false, width: 1 };
  return {
    id: `e${i}`,
    from: e.source_id,
    to: e.target_id,
    color: { color: style.color, opacity: e.type === "PARENT_OF" ? 0.6 : 0.9 },
    dashes: style.dashes,
    width: style.width,
    arrows: e.type === "PARENT_OF" ? undefined : "to",
    title: e.type,
  };
}

// ---------- detail drawer ----------

function showDetail(nodeId) {
  const n = state.nodesById.get(nodeId);
  if (!n) return;
  const p = n.properties || {};
  const color = TYPE_COLOR[n.type] || "#8c8c8c";

  // --node-group is the (light) warning yellow — the badge's default white
  // text (var(--on-primary)) fails contrast on it, unlike the other four.
  const badgeTextColor = n.type === "Group" ? cssVar("--ink") : "";
  const badgeStyle = `background:${color}${badgeTextColor ? `;color:${badgeTextColor}` : ""}`;
  let html = `<span class="detail-type-badge" style="${badgeStyle}">${n.type}</span>`;
  html += `<div class="detail-name">${escapeHtml(n.name || n.id)}</div>`;

  const field = (label, valueHtml) =>
    `<div class="detail-field"><div class="label">${label}</div><div class="value">${valueHtml}</div></div>`;

  if (n.type === "Text" && p.text) {
    html += field("Text", escapeHtml(p.text));
  }
  if (p.image_url) {
    html += field("Image", `<img src="${p.image_url}" />`);
  }
  if (p.capture_url) {
    html += field("Table capture (PNG synthesized by the engine)", `<img src="${p.capture_url}" />`);
  }
  if (Array.isArray(p.grid)) {
    html += field("Grid", renderGridTable(p.grid));
  }
  if (p.vlm_description) {
    html += field("VLM description", escapeHtml(p.vlm_description) +
      (p.vlm_content_type ? ` <span class="muted">(${escapeHtml(p.vlm_content_type)}${p.vlm_confidence ? `, ${p.vlm_confidence}` : ""})</span>` : ""));
  }

  // `propose_synthetic_groups`: this Group node has no `text` (it's synthetic,
  // not extracted content) — show why it was created and who its members are
  // instead. `basis` is the independent cues (spatial/visual/structural/
  // semantic/boundary) the model required at least two of before grouping.
  if (n.type === "Group") {
    const basis = Array.isArray(p.basis) ? p.basis.join(", ") : "";
    html += field(
      "Synthetic group",
      `group_type: <strong>${escapeHtml(p.group_type ?? "(none)")}</strong>` +
        (p.confidence != null ? ` · confidence: ${escapeHtml(String(p.confidence))}` : "") +
        (basis ? `<br />basis: ${escapeHtml(basis)}` : "") +
        (p.rationale ? `<br /><span class="muted">${escapeHtml(p.rationale)}</span>` : "")
    );
    const members = (state.currentEdges || [])
      .filter((e) => e.type === "PARENT_OF" && e.source_id === n.id)
      .map((e) => state.nodesById.get(e.target_id))
      .filter(Boolean);
    if (members.length) {
      html += field(
        `Members (${members.length})`,
        members.map((m) => escapeHtml(m.name || m.id)).join("<br />")
      );
    }
  }

  // this node's incoming PARENT_OF — worth calling out when it's a heading Text
  // rather than the usual Artifact (i.e. propose_edges's HEADING_PARENT role reparented it)
  const parentEdge = (state.currentEdges || []).find((e) => e.type === "PARENT_OF" && e.target_id === n.id);
  if (parentEdge) {
    const parentNode = state.nodesById.get(parentEdge.source_id);
    if (parentNode && parentNode.type === "Text") {
      html += field(
        "Parent (promoted heading)",
        `${escapeHtml(parentNode.name || parentNode.id)}` +
          (parentEdge.properties?.rationale ? `<br /><span class="muted">${escapeHtml(parentEdge.properties.rationale)}</span>` : "")
      );
    }
  }

  // remaining scalar properties, as a table
  const skipKeys = new Set([
    "text", "grid", "capture_url", "image_url", "capture_path", "image_path",
    "vlm_description", "vlm_content_type", "vlm_confidence",
    "group_type", "confidence", "basis", "rationale", "synthetic", "proposed_by",
  ]);
  const rest = Object.entries(p).filter(([k, v]) => !skipKeys.has(k) && v !== null && typeof v !== "object");
  if (rest.length) {
    const rows = rest.map(([k, v]) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(String(v))}</td></tr>`).join("");
    html += field("Properties", `<table class="grid-table">${rows}</table>`);
  }

  detailContent.innerHTML = html;
  drawer.hidden = false;
}

function renderGridTable(grid) {
  const maxRows = 25, maxCols = 14;
  const rows = grid.slice(0, maxRows);
  let html = "<table class=\"grid-table\">";
  for (const row of rows) {
    html += "<tr>" + row.slice(0, maxCols).map((c) => `<td>${escapeHtml(c == null ? "" : String(c))}</td>`).join("") + "</tr>";
  }
  html += "</table>";
  if (grid.length > maxRows) html += `<div class="muted" style="margin-top:4px;font-size:11px;">…${grid.length - maxRows} more row(s), preview truncated</div>`;
  return html;
}

function closeDrawer() {
  drawer.hidden = true;
}

// ---------- utils ----------

function truncate(s, n) {
  s = String(s ?? "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
