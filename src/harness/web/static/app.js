"use strict";

// ---- tiny DOM helper ----
function el(tag, props, ...kids) {
  const n = document.createElement(tag);
  if (props) for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
  }
  return n;
}
const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function fmtAgo(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s | 0}s ago`;
  if (s < 3600) return `${(s / 60) | 0}m ago`;
  if (s < 86400) return `${(s / 3600) | 0}h ago`;
  return `${(s / 86400) | 0}d ago`;
}
function money(x) { return "$" + (x || 0).toFixed(2); }

let CONFIG = { allow_actions: true, poll_interval_ms: 1500 };
let OPEN_RUN = null;
let POLL_MS = 1500;

// ---- polling ----
async function poll() {
  try {
    const data = await api("/api/overview");
    CONFIG = data.config || CONFIG;
    POLL_MS = CONFIG.poll_interval_ms || 1500;
    render(data);
    $("conn").classList.remove("bad");
    if (OPEN_RUN) refreshDrawer(OPEN_RUN);
  } catch (e) {
    $("conn").classList.add("bad");
    $("conn").title = "disconnected: " + e.message;
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

// ---- render overview ----
function render(d) {
  $("instance").textContent = d.instance ? "· " + d.instance : "";
  $("stat-active").textContent = d.totals.active;
  $("stat-gates").textContent = d.totals.waiting;

  const queued = (d.board?.projects || []).reduce((a, p) => a + (p.queued || 0), 0);
  $("stat-queued").textContent = (d.board?.projects?.length ? queued : "–");

  const sp = d.spend || {};
  const pct = sp.ceiling_usd ? Math.min(100, (sp.window_usd / sp.ceiling_usd) * 100) : 0;
  $("spend-fill").style.width = pct + "%";
  $("spend-label").textContent = `${money(sp.window_usd)} / ${money(sp.ceiling_usd)}`;

  $("btn-start").hidden = !CONFIG.allow_actions;
  $("btn-tick").hidden = !CONFIG.allow_actions;

  // active
  const al = $("active-list"); al.innerHTML = "";
  (d.active || []).forEach((r) => al.appendChild(runRow(r)));
  $("active-empty").hidden = (d.active || []).length > 0;

  // recent
  const rl = $("recent-list"); rl.innerHTML = "";
  (d.recent || []).forEach((r) => rl.appendChild(runRow(r)));
  $("recent-count").textContent = `(${(d.recent || []).length})`;

  // board
  const bd = $("board"); bd.innerHTML = "";
  (d.board?.projects || []).forEach((p) => bd.appendChild(boardCard(p)));
}

function runRow(r) {
  const isGate = r.status === "WAITING" && r.has_gate;
  const cls = "run" + (isGate ? " gate" : "");
  const issue = r.issue ? `#${r.issue}` : (r.loop || "");
  return el("div", { class: cls, onclick: () => openDrawer(r.run_id) },
    el("span", { class: "dot " + r.status }),
    el("div", { class: "proj" }, `${r.project || "—"} `, el("span", { class: "loop" }, issue)),
    el("div", { class: "step" }, isGate ? "awaiting your verification" : (r.current_step || r.status.toLowerCase())),
    el("div", { class: "iter" }, `it ${r.iter}/${r.max_iter}`),
    el("div", { class: "cost" }, `${money(r.cost_usd)}/${money(r.budget_usd)}`),
    el("div", { class: "meta" }, fmtAgo(r.updated_at)),
    isGate ? el("span", { class: "badge" }, "NEEDS YOU") : el("span", {}),
  );
}

function boardCard(p) {
  const chips = [];
  if (p.error) chips.push(el("span", { class: "chip" }, "err: " + p.error));
  else {
    const add = (lbl, n) => chips.push(el("span", { class: "chip" }, lbl + " ", el("b", {}, String(n || 0))));
    add("queued", p.queued); add("in-prog", p.in_progress); add("verify", p.needs_verification);
    add("pr-open", p.pr_open); add("PRs", p.open_prs);
  }
  return el("div", { class: "boardcard" },
    el("div", { class: "repo" }, p.repo || p.id),
    el("div", { class: "chips" }, ...chips),
  );
}

// ---- detail drawer ----
async function openDrawer(runId) {
  OPEN_RUN = runId;
  $("overlay").hidden = false;
  $("drawer").hidden = false;
  refreshDrawer(runId);
}
function closeDrawer() {
  OPEN_RUN = null;
  $("overlay").hidden = true;
  $("drawer").hidden = true;
}
async function refreshDrawer(runId) {
  let rec;
  try { rec = await api(`/api/runs/${runId}`); }
  catch (e) { $("d-body").innerHTML = `<p class="err">${e.message}</p>`; return; }
  renderDetail(rec);
}

function renderDetail(rec) {
  $("d-title").textContent = `${rec.project_id || "—"} · ${rec.loop_name}`;
  $("d-sub").textContent = `${rec.status} · ${rec.run_id}`;

  // gate box
  const gb = $("d-gate");
  if (rec.status === "WAITING" && rec.pending_request) {
    gb.hidden = false; gb.innerHTML = "";
    gb.appendChild(el("div", { class: "gateprompt" }, rec.pending_request.prompt));
    const row = el("div", { class: "gateactions" });
    if (rec.pending_request.artifact_path) {
      row.appendChild(el("a", { class: "btn", href: `/api/runs/${rec.run_id}/artifact`, target: "_blank" }, "open artifact"));
    }
    if (CONFIG.allow_actions) {
      const notes = el("input", { class: "notes", type: "text", placeholder: "notes (optional)" });
      row.appendChild(el("button", { class: "btn ok", onclick: () => answer(rec.run_id, true, notes.value) }, "approve"));
      row.appendChild(el("button", { class: "btn danger", onclick: () => answer(rec.run_id, false, notes.value) }, "reject"));
      row.appendChild(notes);
    }
    gb.appendChild(row);
  } else {
    gb.hidden = true;
  }

  // body
  const b = $("d-body"); b.innerHTML = "";
  const br = rec.breakers || {};
  const active = ["RUNNING", "WAITING", "CREATED"].includes(rec.status);
  const kv = el("div", { class: "kv" },
    el("div", { class: "k" }, "status"), el("div", { class: "v" }, rec.status),
    el("div", { class: "k" }, "current step"), el("div", { class: "v" }, rec.current_step || "—"),
    el("div", { class: "k" }, "iteration"), el("div", { class: "v" }, `${br.loop_count}/${br.max_iterations}`),
    el("div", { class: "k" }, "spend"), el("div", { class: "v" }, `${money(br.cumulative_cost_usd)} / ${money(br.budget_ceiling_usd)}`),
    el("div", { class: "k" }, "failures"), el("div", { class: "v" }, `${br.consecutive_failures}/${br.max_consecutive_failures}`),
    rec.data?.issue_number && el("div", { class: "k" }, "issue"),
    rec.data?.issue_number && el("div", { class: "v" }, `#${rec.data.issue_number} (${rec.data.repo || ""})`),
    rec.data?.branch && el("div", { class: "k" }, "branch"),
    rec.data?.branch && el("div", { class: "v" }, rec.data.branch),
    rec.data?.pr_url && el("div", { class: "k" }, "PR"),
    rec.data?.pr_url && el("div", { class: "v" }, el("a", { href: rec.data.pr_url, target: "_blank" }, rec.data.pr_url)),
    rec.terminal_reason && el("div", { class: "k" }, "ended"),
    rec.terminal_reason && el("div", { class: "v" }, rec.terminal_reason),
  );
  b.appendChild(kv);

  if (active && CONFIG.allow_actions) {
    b.appendChild(el("div", { style: "margin-bottom:16px" },
      el("button", { class: "btn danger", onclick: () => abort(rec.run_id) }, "abort run")));
  }

  // step timeline
  const steps = Object.values(rec.step_log || {}).sort((a, b2) => (a.started_at || "").localeCompare(b2.started_at || ""));
  if (steps.length) {
    b.appendChild(el("h2", {}, "timeline"));
    const tl = el("div", { class: "timeline" });
    steps.forEach((s) => {
      const dur = (s.started_at && s.finished_at)
        ? ((new Date(s.finished_at) - new Date(s.started_at)) / 1000).toFixed(1) + "s" : "";
      const row = el("div", { class: "tl" + (s.status === "failed" ? " failed" : "") },
        el("span", { class: "dot " + (s.status === "failed" ? "ABORTED" : "COMPLETED") }),
        el("span", { class: "tlid" }, s.step_id),
        el("span", { class: "tldur" }, dur),
        s.error && el("span", { class: "tlerr" }, s.error),
      );
      tl.appendChild(row);
    });
    b.appendChild(tl);
  }

  // outputs
  const out = (label, text) => {
    if (!text) return;
    b.appendChild(el("details", { class: "out" }, el("summary", {}, label), el("pre", {}, String(text))));
  };
  out("claude result", rec.data?.claude_result);
  out("test output", rec.data?.test_stdout);
  out("build output", rec.data?.build_stdout);
  if (rec.data?.last_failure) out("last failure", JSON.stringify(rec.data.last_failure, null, 2));
}

// ---- actions ----
async function answer(runId, approved, notes) {
  try { await api(`/api/runs/${runId}/answer`, postJson({ approved, notes: notes || "" })); }
  catch (e) { alert("answer failed: " + e.message); }
  setTimeout(() => refreshDrawer(runId), 250);
}
async function abort(runId) {
  if (!confirm("Abort this run? (cannot kill a live process; marks the record aborted)")) return;
  try { await api(`/api/runs/${runId}/abort`, { method: "POST" }); }
  catch (e) { alert("abort failed: " + e.message); }
  setTimeout(() => refreshDrawer(runId), 250);
}
async function tick() {
  try { await api("/api/scheduler/tick", { method: "POST" }); }
  catch (e) { alert("tick failed: " + e.message); }
}
function postJson(body) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

// ---- start dialog ----
function openStart() { $("start-overlay").hidden = false; $("start-dialog").hidden = false; $("start-error").hidden = true; }
function closeStart() { $("start-overlay").hidden = true; $("start-dialog").hidden = true; }
async function doStart() {
  const loop = $("start-loop").value;
  const project = $("start-project").value.trim();
  const issueRaw = $("start-issue").value.trim();
  if (!project) { showStartErr("project id is required"); return; }
  const body = { loop, project };
  if (issueRaw) body.issue = parseInt(issueRaw, 10);
  try {
    const r = await api("/api/runs", postJson(body));
    closeStart();
    openDrawer(r.run_id);
  } catch (e) { showStartErr(e.message); }
}
function showStartErr(m) { const e = $("start-error"); e.textContent = m; e.hidden = false; }

// ---- wire up ----
$("d-close").addEventListener("click", closeDrawer);
$("overlay").addEventListener("click", closeDrawer);
$("btn-tick").addEventListener("click", tick);
$("btn-start").addEventListener("click", openStart);
$("start-cancel").addEventListener("click", closeStart);
$("start-overlay").addEventListener("click", closeStart);
$("start-go").addEventListener("click", doStart);
$("recent-toggle").addEventListener("click", (e) => {
  e.target.classList.toggle("collapsed"); $("recent-list").hidden = !$("recent-list").hidden;
});
$("board-toggle").addEventListener("click", (e) => {
  e.target.classList.toggle("collapsed"); $("board").hidden = !$("board").hidden;
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeDrawer(); closeStart(); } });

poll();
