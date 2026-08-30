"use strict";

const byId = (id) => document.getElementById(id);
const has = (value) => value !== null && value !== undefined && value !== "";
const show = (value) => has(value) ? String(value) : "—";
const node = (tag, className = "", text) => {
  const value = document.createElement(tag);
  value.className = className;
  if (text !== undefined) value.textContent = text;
  return value;
};
const cell = (value) => node("td", "", show(value));
const localTime = (value) => {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime())
    ? new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "medium"}).format(date)
    : "—";
};
const elapsed = (seconds) => {
  if (!Number.isFinite(Number(seconds))) return "—";
  let rest = Math.max(0, Number(seconds));
  const parts = [[Math.floor(rest / 86400), "d"]]; rest %= 86400;
  parts.push([Math.floor(rest / 3600), "h"]); rest %= 3600;
  parts.push([Math.floor(rest / 60), "m"], [Math.floor(rest % 60), "s"]);
  return parts.filter(([amount]) => amount).slice(0, 2).map(([amount, unit]) => `${amount}${unit}`).join(" ") || "0s";
};
const status = (value) => node("span", `status status-${value || "unknown"}`, value || "unknown");
const badge = (value, kind = "") => node("span", `badge ${kind}`, value);
const link = (href, label, external = false) => {
  const value = node("a", "", label); value.href = href;
  if (external) { value.target = "_blank"; value.rel = "noopener noreferrer"; }
  return value;
};
const setError = (id, message = "") => {
  const value = byId(id); if (!value) return;
  value.textContent = message; value.classList.toggle("hidden", !message);
};
const queryFrom = (form) => {
  const query = new URLSearchParams();
  for (const [key, value] of new FormData(form).entries()) if (String(value)) query.set(key, String(value));
  return query;
};
const restoreForm = (form) => {
  const query = new URLSearchParams(location.search);
  for (const field of form.elements) if (field.name && query.has(field.name)) field.value = query.get(field.name);
};
const persistForm = (form) => {
  const query = queryFrom(form).toString();
  history.replaceState(null, "", `${location.pathname}${query ? `?${query}` : ""}`);
};
const list = (entries, className = "detail-grid") => {
  const result = node("dl", className);
  for (const [label, value] of entries) {
    const wrapper = node("div"); wrapper.append(node("dt", "", label), node("dd", "", show(value))); result.append(wrapper);
  }
  return result;
};
const timestampCell = (value) => {
  const result = node("td"); const time = node("time", "", localTime(value));
  if (value) { time.dateTime = value; time.title = `${value} (UTC)`; }
  result.append(time); return result;
};

const csrfToken = () => document.body.dataset.csrfToken || "";
let pendingCandidate = null;

const actionEffect = (action) => {
  if (["refresh_connections", "audit_plan"].includes(action)) return "Read-only probe or metadata plan; no product mutation.";
  if (["pause", "pause_after_current", "resume", "nudge", "retry", "stop_after_current", "cancel", "skip"].includes(action)) return "Durable workflow signal; no shell or arbitrary command execution.";
  if (action === "solve_issue" || action === "solve_backlog") return "Source, GitHub, build, staging, and runtime effects remain subject to immutable Engineering Control gates. Production is prohibited.";
  return "Typed Engineering Control operation; current policy and capability gates remain authoritative.";
};

async function prepareAction(command, errorTarget = null) {
  try {
    const response = await fetch("/api/dashboard/actions/candidates", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({csrf_token: csrfToken(), command}),
    });
    const candidate = await response.json();
    if (!response.ok) throw new Error(candidate.detail || "Unable to prepare operation.");
    if (candidate.capability.status === "RUNNING" && candidate.operation_binding?.resolved?.existing_workflow_id) {
      location.assign(`/workflows/${encodeURIComponent(candidate.operation_binding.resolved.existing_workflow_id)}`);
      return;
    }
    if (candidate.capability.status !== "AVAILABLE") {
      throw new Error((candidate.capability.blockers || []).join(" · ") || `Capability is ${candidate.capability.status}.`);
    }
    pendingCandidate = candidate;
    byId("action-effect").textContent = actionEffect(command.action);
    byId("action-binding").textContent = JSON.stringify(candidate.operation_binding, null, 2);
    byId("action-password").value = ""; byId("action-phrase").value = "";
    const phrase = candidate.confirmation_phrase || "";
    byId("action-phrase-field").classList.toggle("hidden", !phrase);
    byId("action-phrase").required = Boolean(phrase);
    byId("action-phrase-help").textContent = phrase ? `Required phrase: ${phrase}` : "This operation is bound to the displayed SHA-256 digest.";
    setError("action-blockers"); byId("action-dialog").showModal(); byId("action-password").focus();
    if (errorTarget) setError(errorTarget);
  } catch (error) {
    if (errorTarget) setError(errorTarget, error.message); else window.alert(error.message);
  }
}

async function confirmAction(event) {
  event.preventDefault(); if (!pendingCandidate) return;
  const execute = byId("action-execute"); execute.disabled = true;
  try {
    const reauth = await fetch("/api/dashboard/actions/reauthenticate", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({csrf_token: csrfToken(), candidate_id: pendingCandidate.candidate_id, current_password: byId("action-password").value}),
    });
    const authorized = await reauth.json();
    if (!reauth.ok) throw new Error(authorized.detail || "Unable to authorize operation.");
    byId("action-password").value = "";
    const response = await fetch("/api/dashboard/actions/execute", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({csrf_token: csrfToken(), candidate_id: pendingCandidate.candidate_id, reauthentication_token: authorized.reauthentication_token, confirmation_phrase: byId("action-phrase").value || null}),
    });
    let operation = await response.json();
    if (!response.ok) throw new Error(operation.detail || "Operation was not accepted.");
    for (let attempt = 0; attempt < 20 && ["PENDING", "DISPATCHING"].includes(operation.status); attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 500));
      const poll = await fetch(`/api/dashboard/operations/${operation.operation_id}`, {cache: "no-store"});
      if (poll.ok) operation = await poll.json();
    }
    if (["FAILED", "BLOCKED"].includes(operation.status)) throw new Error(operation.safe_error || `Operation ${operation.status.toLowerCase()}.`);
    byId("action-dialog").close(); pendingCandidate = null;
    if (operation.workflow_id) location.assign(`/workflows/${encodeURIComponent(operation.workflow_id)}`);
    else if (operation.result_available) location.assign(`/api/dashboard/operations/${operation.operation_id}/export`);
    else location.reload();
  } catch (error) { setError("action-blockers", error.message); }
  finally { execute.disabled = false; }
}

function setupActionDialog() {
  const form = byId("action-confirmation"); if (!form) return;
  form.addEventListener("submit", confirmAction);
  byId("action-cancel").addEventListener("click", () => { pendingCandidate = null; byId("action-password").value = ""; byId("action-dialog").close(); });
}

function setupOverview() {
  if (!byId("summary-cards")) return;
  const load = async () => {
    try {
      const [response, connectionResponse] = await Promise.all([
        fetch("/api/dashboard/overview", {cache: "no-store"}),
        fetch("/api/dashboard/connections", {cache: "no-store"}),
      ]);
      if (!response.ok || !connectionResponse.ok) throw new Error("Overview data is temporarily unavailable.");
      const data = await response.json(); const connections = await connectionResponse.json();
      const labels = [["active_workflows", "Active workflows"], ["queued_issues", "Queued issues"], ["blocked_workflows", "Blocked workflows"], ["failed_24h", "Failed in 24h"], ["active_audits", "Active audits"]];
      byId("summary-cards").replaceChildren(...labels.map(([key, label]) => {
        const card = node("article", "summary-card"); card.append(node("p", "muted", label), node("strong", "summary-value", show(data.summary[key]))); return card;
      }));
      const work = data.active_work.map((item) => {
        const card = node("article", `work-card${item.stale ? " stale" : ""}`);
        const heading = node("div", "work-card-heading"); const identity = node("div");
        identity.append(node("p", "eyebrow", `${item.project} · ${item.workflow_type}`), node("h3", "", item.repository || item.campaign_name || item.workflow_id)); heading.append(identity, status(item.normalized_status));
        const owner = item.issue_url && item.issue_number ? link(item.issue_url, `GitHub issue #${item.issue_number}`, true) : node("span", "muted", item.campaign_id ? `Audit ${item.campaign_id}` : "No issue correlation");
        const links = node("p", "card-links"); links.append(link(`/workflows/${encodeURIComponent(item.workflow_id)}`, "Workflow detail")); if (item.execution_id) links.append(" · ", link(`/executions/${item.execution_id}`, "Execution transcript"));
        card.append(heading, owner, list([["Raw status", item.raw_status], ["Phase", item.current_phase], ["Provider", item.active_provider], ["Role", item.active_role], ["Host", item.host], ["Worker", item.worker], ["Task queue", item.task_queue], ["Elapsed", elapsed(item.duration_seconds)], ["Heartbeat", localTime(item.last_heartbeat)], ["Attempt", `${show(item.attempt)} / ${show(item.max_attempts)}`], ["Completed", (item.completed_steps || []).join(", ")], ["Current step", item.current_step], ["Expected next", item.expected_next_step], ["Blocker", item.blocker], ["Source", item.source], ["Freshness", `${item.freshness}${has(item.age_seconds) ? ` · ${elapsed(item.age_seconds)} old` : ""}`]]), links);
        return card;
      });
      byId("active-work").replaceChildren(...work); byId("idle-state").classList.toggle("hidden", !data.idle);
      if (data.idle) byId("idle-details").replaceChildren(...list([["Queue depth", data.summary.queued_issues], ["Next eligible", data.next_eligible_issue ? `${data.next_eligible_issue.project} #${data.next_eligible_issue.issue_number}` : null], ["Active blockers", data.summary.blocked_workflows], ["Provider readiness", (data.providers || []).map((item) => `${item.provider}: ${item.available ? "available" : item.authentication_status}`).join(", ")]], "").children);
      const health = [...(data.services || []), ...(data.providers || [])].map((item) => {
        const card = node("article", `health-card${item.stale ? " stale" : ""}`); card.append(node("h4", "", item.name || item.provider), status(item.status || item.capacity_status || (item.available ? "ready" : "unknown")), list([["Kind", item.kind || "provider"], ["CLI", item.cli_version], ["Authentication", item.authentication_status], ["Active", item.active_executions], ["Queue", item.queue_depth], ["Last success", localTime(item.last_success)], ["Last failure", localTime(item.last_failure)], ["Observed", localTime(item.observed_at)], ["Freshness", item.freshness], ["Source", item.source]])); return card;
      });
      byId("health-cards").replaceChildren(...health);
      const priority = ["engineering-dashboard", "hermes-gateway", "telegram", "engineering-api", "engineering-cli", "provider-codex", "provider-claude"];
      const connectionCards = priority.map((component) => connections.items.find((item) => item.component === component)).filter(Boolean).map((item) => {
        const card = node("article", `health-card${item.status === "STALE" ? " stale" : ""}`); card.append(node("h4", "", item.component), status(item.status), list([["Checked", localTime(item.checked_at)], ["Freshness", elapsed(item.freshness_seconds)], ["Source", item.source], ["Next action", item.next_action]])); return card;
      });
      byId("overview-connections").replaceChildren(...connectionCards); byId("overview-freshness").textContent = `Observed ${localTime(data.observed_at)} · ${data.source}`; setError("overview-error");
    } catch (error) { setError("overview-error", error.message); }
  };
  load(); window.setInterval(load, 5000);
}

function setupBacklog() {
  const form = byId("backlog-filters"); if (!form) return;
  const deliveryMode = () => byId("backlog-delivery-mode").value;
  restoreForm(form); let page = 1; let pages = 1; let currentItems = []; const expanded = new Set();
  const load = async () => {
    const query = queryFrom(form); query.set("page", page); query.set("page_size", "50");
    try {
      const response = await fetch(`/api/dashboard/backlog?${query}`, {cache: "no-store"}); if (!response.ok) throw new Error("Backlog data is temporarily unavailable."); const data = await response.json(); const rows = [];
      currentItems = data.items;
      for (const item of data.items) {
        const key = `${item.project}:${item.issue_number || item.workflow_id}`; const row = node("tr", "expandable-row"); row.tabIndex = 0; row.setAttribute("aria-expanded", expanded.has(key));
        const issue = node("td"); if (item.issue_url && item.issue_number) issue.append(link(item.issue_url, `#${item.issue_number}`, true)); else issue.textContent = item.issue_number ? `#${item.issue_number}` : "—";
        const actions = node("td");
        if (item.backlog_campaign_id && item.repository && item.issue_number) { const solve = node("button", "compact", "Solve"); solve.type = "button"; const eligible = item.issue_state === "OPEN" && ["READY", "ELIGIBLE_FOR_REVALIDATION", "ELIGIBLE_FOR_RUNTIME_REVALIDATION"].includes(item.eligibility_status) && !["preparing", "implementing", "reviewing", "reworking", "testing", "awaiting_ci", "awaiting_approval", "building", "deploying", "verifying"].includes(item.normalized_status); solve.disabled = !eligible; solve.title = eligible ? "Prepare an immutable solve candidate" : `Unavailable: ${item.blocker || item.eligibility_status || item.issue_state}`; solve.addEventListener("click", (event) => { event.stopPropagation(); prepareAction({action: "solve_issue", project: item.project, backlog_campaign_id: item.backlog_campaign_id, repository: item.repository, issue_number: item.issue_number, maximum_risk: "R2", delivery_mode: deliveryMode(), correction_limit: 2, production: false}, "backlog-error"); }); actions.append(solve); }
        if (item.workflow_id) actions.append(" ", link(`/workflows/${encodeURIComponent(item.workflow_id)}`, "View"));
        const state = node("td"); state.append(status(item.normalized_status)); row.append(issue, cell(item.title), cell(item.project), state, cell(item.raw_status), cell(item.current_phase), cell(item.active_provider), cell(item.provider_role), cell(item.worker), timestampCell(item.last_heartbeat), cell(elapsed(item.duration_seconds)), cell(`${show(item.attempt)} / ${show(item.max_attempts)}`), cell(item.pr_state), cell(item.ci_state), cell(item.build_state), cell(item.deployment_state), cell(item.verification_state), cell(item.blocker), actions);
        const detailRow = node("tr", `row-detail${expanded.has(key) ? "" : " hidden"}`); const detail = node("td"); detail.colSpan = 19; const links = node("p", "card-links"); if (item.workflow_id) links.append(link(`/workflows/${encodeURIComponent(item.workflow_id)}`, "Workflow")); if (item.execution_id) links.append(" · ", link(`/executions/${item.execution_id}`, "Provider execution"));
        detail.append(list([["Workflow", item.workflow_id], ["Queued", localTime(item.queued_at)], ["Started", localTime(item.started_at)], ["Ended", localTime(item.ended_at)], ["Last activity", localTime(item.last_activity_at)], ["Freshness", `${item.freshness}${has(item.age_seconds) ? ` · ${elapsed(item.age_seconds)} old` : ""}`], ["Data source", item.source], ["Timeline", (item.timeline || []).map((step) => `${step.phase}: ${step.status}`).join(" · ") || "No persisted timeline"]]), links); detailRow.append(detail);
        const toggle = () => { expanded.has(key) ? expanded.delete(key) : expanded.add(key); row.setAttribute("aria-expanded", expanded.has(key)); detailRow.classList.toggle("hidden", !expanded.has(key)); };
        row.addEventListener("click", (event) => { if (!event.target.closest("a")) toggle(); }); row.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); toggle(); } }); rows.push(row, detailRow);
      }
      byId("backlog-rows").replaceChildren(...rows); pages = data.pages; byId("backlog-page").textContent = `Page ${page} of ${pages} · ${data.total} items`; byId("backlog-prev").disabled = page <= 1; byId("backlog-next").disabled = page >= pages; byId("backlog-empty").classList.toggle("hidden", data.items.length !== 0); byId("last-refreshed").textContent = `Last refreshed ${localTime(data.refreshed_at)}`; setError("backlog-error");
    } catch (error) { setError("backlog-error", error.message); }
  };
  byId("solve-backlog").addEventListener("click", () => { const project = form.elements.project.value; const item = currentItems.find((value) => value.backlog_campaign_id && (!project || value.project === project)); if (!item) { setError("backlog-error", "No persisted campaign is available for the selected project."); return; } prepareAction({action: "solve_backlog", project: item.project, backlog_campaign_id: item.backlog_campaign_id, issue_filter: null, maximum_risk: "R2", delivery_mode: deliveryMode(), maximum_items: 10, continue_after_blocked_issue: true, include_enhancements: false, correction_limit: 2, concurrency: 1, deployment_concurrency: 1, production: false}, "backlog-error"); });
  form.addEventListener("submit", (event) => { event.preventDefault(); page = 1; persistForm(form); load(); }); byId("backlog-prev").addEventListener("click", () => { if (page > 1) { page -= 1; load(); } }); byId("backlog-next").addEventListener("click", () => { if (page < pages) { page += 1; load(); } }); load(); window.setInterval(load, 5000);
}

function setupWorkflows() {
  const form = byId("workflow-filters"); if (!form) return; restoreForm(form); let cursor = null;
  const load = async () => {
    const query = queryFrom(form); query.set("limit", "50"); if (cursor) query.set("cursor", cursor); for (const key of ["date_from", "date_to"]) if (query.has(key)) query.set(key, new Date(query.get(key)).toISOString());
    try {
      const response = await fetch(`/api/dashboard/workflows?${query}`, {cache: "no-store"}); if (!response.ok) throw new Error("Workflow data is temporarily unavailable."); const data = await response.json();
      byId("workflow-rows").replaceChildren(...data.items.map((item) => { const row = node("tr"); const id = node("td"); id.append(link(`/workflows/${encodeURIComponent(item.workflow_id)}`, item.workflow_id)); const state = node("td"); state.append(status(item.normalized_status)); row.append(id, cell(item.project), cell(item.issue_number ? `#${item.issue_number}` : item.campaign_id), cell(item.workflow_type), state, cell(item.current_phase), cell(`${show(item.active_provider)} / ${show(item.active_role)}`), cell(item.worker), timestampCell(item.started_at), timestampCell(item.last_activity_at), cell(elapsed(item.duration_seconds)), cell(item.retry_count), cell(item.blocker)); return row; }));
      byId("workflow-empty").classList.toggle("hidden", data.items.length !== 0); byId("workflow-next").disabled = !data.next_cursor; byId("workflow-next").dataset.cursor = data.next_cursor || ""; byId("workflow-refreshed").textContent = `Observed ${localTime(data.observed_at)} · ${data.source}`; setError("workflow-error");
    } catch (error) { setError("workflow-error", error.message); }
  };
  form.addEventListener("submit", (event) => { event.preventDefault(); cursor = null; persistForm(form); load(); }); byId("workflow-reset").addEventListener("click", () => { cursor = null; load(); }); byId("workflow-next").addEventListener("click", (event) => { cursor = event.currentTarget.dataset.cursor || null; load(); }); load(); window.setInterval(() => { if (!cursor) load(); }, 5000);
}

function setupWorkflowDetail() {
  const root = byId("workflow-detail"); if (!root) return; const id = root.dataset.workflowId;
  const load = () => fetch(`/api/dashboard/workflows/${encodeURIComponent(id)}`, {cache: "no-store"}).then((response) => { if (!response.ok) throw new Error("Workflow detail is unavailable."); return response.json(); }).then((item) => {
    const heading = node("div", "work-card-heading"); heading.append(node("h3", "breakable", item.workflow_id), status(item.normalized_status)); byId("workflow-identity").replaceChildren(heading, list([["Temporal run", item.temporal_run_id], ["Project", item.project], ["Repository", item.repository], ["Issue", item.issue_number], ["Campaign", item.campaign_id], ["ChangeSet", item.change_set_id], ["Deployment", item.deployment_id], ["Raw status", item.raw_status], ["Phase", item.current_phase], ["Provider / role", `${show(item.active_provider)} / ${show(item.active_role)}`], ["Host / worker", `${show(item.host)} / ${show(item.worker)}`], ["Task queue", item.task_queue], ["Source", item.source], ["Freshness", item.freshness]]));
    byId("workflow-timeline").replaceChildren(...(item.timeline || []).map((step) => { const card = node("article", "timeline-step"); const title = node("div", "work-card-heading"); title.append(node("h4", "", step.phase), status(step.status)); card.append(title, list([["Raw", step.raw_status], ["Start", localTime(step.started_at)], ["End", localTime(step.ended_at)], ["Duration", elapsed(step.duration_seconds)], ["Attempt", step.attempt], ["Provider", step.provider], ["Evidence", step.evidence_reference], ["Blocker", step.blocker]])); return card; }));
    const table = node("table"); const head = node("thead"); const headRow = node("tr"); ["Execution", "Provider", "Role", "Phase", "Status", "Host / worker", "Task queue", "CLI", "Attempt", "Heartbeat", "Transcript"].forEach((label) => headRow.append(node("th", "", label))); head.append(headRow); const body = node("tbody"); for (const execution of item.executions || []) { const row = node("tr"); const executionId = node("td"); executionId.append(link(`/executions/${execution.execution_id}`, execution.execution_id)); row.append(executionId, cell(execution.provider), cell(execution.role), cell(execution.phase), cell(execution.normalized_status), cell(`${show(execution.host)} / ${show(execution.worker_kind)}`), cell(execution.task_queue), cell(execution.cli_version), cell(`${execution.attempt}/${execution.max_attempts}`), timestampCell(execution.last_heartbeat), cell(execution.transcript_complete ? "complete" : "incomplete")); body.append(row); } table.append(head, body); byId("workflow-executions").replaceChildren(table);
    const active = !["completed", "failed", "cancelled", "skipped"].includes(item.normalized_status); const backlogCampaign = item.workflow_id.startsWith("backlog-loop-");
    const running = active && !["paused", "blocked"].includes(item.normalized_status); const actionStates = {pause: running, pause_after_current: running && backlogCampaign, resume: ["paused", "blocked"].includes(item.normalized_status), nudge: active, retry: ["paused", "blocked", "failed"].includes(item.normalized_status), stop_after_current: running && backlogCampaign, cancel: active};
    document.querySelectorAll("[data-workflow-action]").forEach((button) => { button.disabled = !actionStates[button.dataset.workflowAction]; button.title = button.disabled ? `Unavailable while workflow is ${item.normalized_status}` : `Prepare typed ${button.dataset.workflowAction} candidate`; });
    const printable = (value) => typeof value === "object" ? JSON.stringify(value) : value; byId("workflow-outcomes").replaceChildren(node("h4", "", "Outcomes"), list(Object.entries(item.outcomes || {}).map(([key, value]) => [key.replaceAll("_", " "), printable(value)]))); byId("workflow-blocker").replaceChildren(node("h4", "", "Blocker explanation"), list(Object.entries(item.blocker_explanation || {}).map(([key, value]) => [key.replaceAll("_", " "), printable(value)]))); setError("workflow-detail-error");
  }).catch((error) => setError("workflow-detail-error", error.message));
  document.querySelectorAll("[data-workflow-action]").forEach((button) => button.addEventListener("click", () => prepareAction({action: button.dataset.workflowAction, workflow_id: id}, "workflow-detail-error")));
  load(); window.setInterval(load, 5000);
}

function setupLogs() {
  const form = byId("activity-filters"); if (!form) return; restoreForm(form); let events = []; let paused = false; let tab = "all"; let renderScheduled = false; const shell = byId("activity-shell");
  const render = () => {
    const values = Object.fromEntries(new FormData(form).entries()); const filtered = events.filter((item) => { const meta = item.metadata || {}; const provider = String(meta.provider || "").toLowerCase(); const observed = item.timestamp ? new Date(item.timestamp).getTime() : 0; const from = values.date_from ? new Date(values.date_from).getTime() : null; const to = values.date_to ? new Date(values.date_to).getTime() : null; const text = `${item.event_type || ""} ${item.message || ""} ${item.current_phase || ""}`.toLowerCase(); const tabOk = tab === "all" || (tab === "hermes" && item.source === "hermes") || (tab === "services" && !provider && item.source !== "hermes") || provider === tab; return tabOk && (!values.project || item.project === values.project) && (!values.issue || String(item.issue_number) === values.issue) && (!values.workflow || String(item.workflow_id || "").toLowerCase().includes(values.workflow.toLowerCase())) && (!values.campaign || String(item.campaign_id || "").includes(values.campaign)) && (!values.provider || provider === values.provider) && (!values.role || String(meta.role || "").toLowerCase().includes(values.role.toLowerCase())) && (!values.worker || String(item.worker_kind || item.source || "").toLowerCase().includes(values.worker.toLowerCase())) && (!values.host || String(item.host || "").toLowerCase().includes(values.host.toLowerCase())) && (!values.phase || String(item.current_phase || "").toLowerCase().includes(values.phase.toLowerCase())) && (!values.level || item.level === values.level) && (!from || observed >= from) && (!to || observed <= to) && (!values.search || text.includes(values.search.toLowerCase())); });
    byId("activity-rows").replaceChildren(...filtered.map((item) => { const row = node("tr"); row.append(timestampCell(item.timestamp), cell(item.level), cell(item.event_type), cell(`${item.source}${item.worker_kind ? ` / ${item.worker_kind}` : ""}`), cell(`${show(item.project)}${item.issue_number ? ` #${item.issue_number}` : item.campaign_id ? ` ${item.campaign_id}` : ""}`), cell(item.workflow_id), cell(item.current_phase), cell(item.message)); return row; })); byId("activity-empty").classList.toggle("hidden", filtered.length !== 0); if (!paused) shell.scrollTop = shell.scrollHeight;
  };
  const scheduleRender = () => { if (renderScheduled) return; renderScheduled = true; window.requestAnimationFrame(() => { renderScheduled = false; render(); }); };
  const connectStream = (afterId) => { const stream = new EventSource(`/api/dashboard/activity/stream?after_id=${encodeURIComponent(afterId)}`); const streamState = (label, kind) => { byId("stream-state").textContent = label; byId("stream-state").className = `connection ${kind}`; }; stream.addEventListener("open", () => streamState("Connected", "connected")); stream.addEventListener("error", () => streamState(stream.readyState === EventSource.CLOSED ? "Disconnected" : "Reconnecting", stream.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting")); stream.addEventListener("activity", (event) => { try { events.push(JSON.parse(event.data)); events = events.slice(-1000); scheduleRender(); } catch (_) { setError("activity-error", "An invalid event was ignored."); } }); };
  fetch("/api/dashboard/activity/recent?limit=500", {cache: "no-store"}).then((response) => response.ok ? response.json() : Promise.reject()).then((data) => { events = data.items.slice(-500); render(); connectStream(data.last_event_id || events.at(-1)?.id || 0); }).catch(() => { setError("activity-error", "Recent events are temporarily unavailable."); connectStream(0); });
  form.addEventListener("input", () => { persistForm(form); render(); }); byId("autoscroll").addEventListener("click", (event) => { paused = !paused; event.currentTarget.setAttribute("aria-pressed", paused); event.currentTarget.textContent = paused ? "Resume autoscroll" : "Pause autoscroll"; }); document.querySelectorAll("[data-log-tab]").forEach((button) => button.addEventListener("click", () => { tab = button.dataset.logTab; document.querySelectorAll("[data-log-tab]").forEach((candidate) => candidate.setAttribute("aria-selected", candidate === button)); render(); loadExecutions(); }));
  const loadExecutions = async () => { try { const values = Object.fromEntries(new FormData(form).entries()); const query = new URLSearchParams({limit: "50"}); const mappings = {project: "project", issue: "issue_number", campaign: "campaign_id", workflow: "workflow_id", provider: "provider", role: "role", worker: "worker", host: "host", phase: "phase", search: "search"}; for (const [field, parameter] of Object.entries(mappings)) if (values[field]) query.set(parameter, values[field]); for (const field of ["date_from", "date_to"]) if (values[field]) query.set(field, new Date(values[field]).toISOString()); if (["codex", "claude"].includes(tab)) query.set("provider", tab); const response = await fetch(`/api/dashboard/executions?${query}`, {cache: "no-store"}); if (!response.ok) throw new Error(); const data = await response.json(); byId("execution-rows").replaceChildren(...data.items.map((item) => { const row = node("tr"); const identity = node("td"); identity.append(link(`/executions/${item.execution_id}`, item.execution_id)); const transcript = node("td"); transcript.append(badge(item.transcript_complete ? "complete" : "incomplete", item.transcript_complete ? "" : "warning")); if (item.redaction_count) transcript.append(badge(`${item.redaction_count} redacted`, "redacted")); if (item.output_truncated) transcript.append(badge("truncated", "warning")); row.append(identity, cell(`${item.project}${item.issue_number ? ` #${item.issue_number}` : item.campaign_id ? ` ${item.campaign_id}` : ""}`), cell(item.provider), cell(item.role), cell(item.phase), cell(item.normalized_status), cell(`${show(item.host)} / ${show(item.worker_kind)}`), timestampCell(item.started_at), cell(elapsed(item.duration_seconds)), cell(`${localTime(item.last_heartbeat)}${item.stale ? " · stale" : ""}`), cell(`${item.attempt}/${item.max_attempts}`), transcript); return row; })); setError("execution-list-error"); } catch (_) { setError("execution-list-error", "Execution summaries are temporarily unavailable."); } }; form.addEventListener("input", loadExecutions); loadExecutions(); window.setInterval(loadExecutions, 5000);
}

function setupExecution() {
  const root = byId("execution-detail"); if (!root) return; const id = root.dataset.executionId; let chunks = []; let before = null; let follow = true;
  const render = () => { const search = byId("transcript-search").value.toLowerCase(); const rows = chunks.filter((item) => !search || item.text.toLowerCase().includes(search)).slice(-1500).map((item) => { const row = node("li", `transcript-line stream-${item.stream}`); row.dataset.sequence = item.sequence; const label = node("label"); const selected = document.createElement("input"); selected.type = "checkbox"; selected.setAttribute("aria-label", `Select line ${item.sequence}`); label.append(selected, node("time", "", localTime(item.timestamp)), badge(item.stream), node("span", "transcript-text", item.text)); if (item.redaction_count) label.append(badge(`${item.redaction_count} redacted`, "redacted")); if (item.is_truncated) label.append(badge("truncated", "warning")); row.append(label); return row; }); const output = byId("transcript-lines"); output.replaceChildren(...rows); if (follow) output.scrollTop = output.scrollHeight; };
  const merge = (items, older = false) => { const unique = new Map(chunks.map((item) => [item.sequence, item])); for (const item of items) unique.set(item.sequence, item); const ordered = [...unique.values()].sort((a, b) => a.sequence - b.sequence); chunks = older ? ordered.slice(0, 1500) : ordered.slice(-1500); if (items.length) before = Math.min(...chunks.map((item) => item.sequence)); render(); };
  const loadIdentity = () => fetch(`/api/dashboard/executions/${id}`, {cache: "no-store"}).then((response) => { if (!response.ok) throw new Error("Execution is unavailable."); return response.json(); }).then((item) => { const heading = node("div", "work-card-heading"); heading.append(node("h3", "breakable", `${item.provider} · ${item.role}`), status(item.normalized_status)); byId("execution-identity").replaceChildren(heading, list([["Execution", item.execution_id], ["Workflow", item.workflow_id], ["Temporal run", item.temporal_run_id], ["Activity", item.activity_id], ["Project", item.project], ["Repository", item.repository], ["Issue / campaign", item.issue_number || item.campaign_id], ["Host / worker", `${show(item.host)} / ${show(item.worker_kind)}`], ["Task queue", item.task_queue], ["CLI version", item.cli_version], ["Selection reason", item.selection_reason], ["Attempt", `${item.attempt}/${item.max_attempts}`], ["Started", localTime(item.started_at)], ["Ended", localTime(item.ended_at)], ["Heartbeat", localTime(item.last_heartbeat)], ["Exit code", item.exit_code], ["Blocker", item.blocker_code], ["Source", item.source], ["Freshness", item.freshness]])); const values = [badge("CLI-visible output"), badge("redacted", "redacted")]; if (item.output_truncated) values.push(badge("truncated", "warning")); if (item.transcript_incomplete) values.push(badge("incomplete", "warning")); byId("transcript-badges").replaceChildren(...values); }).catch((error) => setError("execution-error", error.message)); loadIdentity(); window.setInterval(loadIdentity, 5000);
  const load = async (older = false) => { const query = new URLSearchParams({limit: "300"}); if (older && before) query.set("before_sequence", before); const response = await fetch(`/api/dashboard/executions/${id}/logs?${query}`, {cache: "no-store"}); if (!response.ok) throw new Error("Transcript data is unavailable."); const data = await response.json(); merge(data.items, older); return data.last_sequence || 0; };
  load().then(() => { const stream = new EventSource(`/api/dashboard/executions/${id}/stream`); const streamState = (label, kind) => { byId("transcript-state").textContent = label; byId("transcript-state").className = `connection ${kind}`; }; stream.addEventListener("open", () => streamState("Connected", "connected")); stream.addEventListener("error", () => streamState(stream.readyState === EventSource.CLOSED ? "Disconnected" : "Reconnecting", stream.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting")); stream.addEventListener("transcript", (event) => { try { merge([JSON.parse(event.data)]); } catch (_) { setError("execution-error", "An invalid transcript event was ignored."); } }); }).catch((error) => setError("execution-error", error.message));
  byId("transcript-older").addEventListener("click", () => load(true).catch((error) => setError("execution-error", error.message))); byId("transcript-search").addEventListener("input", render); byId("transcript-follow").addEventListener("click", (event) => { follow = !follow; event.currentTarget.setAttribute("aria-pressed", follow); event.currentTarget.textContent = follow ? "Following tail" : "Follow tail paused"; if (follow) render(); }); byId("transcript-copy").addEventListener("click", async () => { const selected = [...document.querySelectorAll(".transcript-line input:checked")].map((input) => input.closest("li").querySelector(".transcript-text").textContent); if (selected.length) await navigator.clipboard.writeText(selected.join("\n")); });
}

function setupAudits() {
  const form = byId("audit-filters"); if (!form) return; restoreForm(form); const expanded = new Set();
  const load = async () => { try { const response = await fetch(`/api/dashboard/audits?${queryFrom(form)}`, {cache: "no-store"}); if (!response.ok) throw new Error("Audit data is temporarily unavailable."); const data = await response.json(); const cards = data.items.map((item) => { const details = node("details", "audit"); details.open = expanded.has(item.campaign_id); details.addEventListener("toggle", () => details.open ? expanded.add(item.campaign_id) : expanded.delete(item.campaign_id)); const summary = node("summary"); summary.append(node("span", "", `${item.project} · ${item.preset || item.name} · ${item.campaign_id}`), status(item.normalized_status)); details.append(summary); if (item.total_cells > 0) { const progress = node("progress"); progress.max = 100; progress.value = item.completion_percentage; progress.setAttribute("aria-label", `${item.completion_percentage}% complete`); details.append(progress); } else details.append(node("p", "muted", "Progress is indeterminate: no coverage cells are persisted.")); const discover = node("button", "compact", "Discover enhancements"); discover.type = "button"; discover.addEventListener("click", () => prepareAction({action: "enhancement_discover", project: item.project, campaign_id: item.campaign_id, dry_run: true}, "audit-error")); details.append(node("p", "", `${item.completion_percentage}% · ${item.total_cells} cells · last activity ${localTime(item.last_activity_at)}`), discover, list([["Workflow", item.workflow_id], ["Environment", item.environment], ["Raw status", item.raw_status], ["Freshness", `${item.freshness}${has(item.age_seconds) ? ` · ${elapsed(item.age_seconds)} old` : ""}`], ["Data source", item.source], ["Runnable", item.runnable_cells], ["Queued", item.queued_cells], ["Running", item.running_cells], ["Passed", item.passed_cells], ["Confirmed findings", item.confirmed_findings], ["Blocked by root", item.blocked_by_root], ["Independently blocked", item.independently_blocked], ["Failed", item.failed_cells], ["Skipped", item.skipped_cells], ["Surface", item.repository_surface], ["Domain", item.domain], ["Route / operation", item.route_operation], ["Persona", item.persona], ["Flow", item.flow], ["Lens", item.lens], ["Provider / role", `${show(item.provider)} / ${show(item.provider_role)}`], ["Worker", item.worker], ["Execution", item.execution_id], ["Cell start", localTime(item.cell_started_at)], ["Cell end", localTime(item.cell_ended_at)], ["Result", item.cell_result], ["Blocker", item.blocker], ["Persona lifecycle", item.persona_lifecycle ? JSON.stringify(item.persona_lifecycle) : null], ["Root blockers", (item.root_blockers || []).map((root) => `${root.code}: ${root.dependent_cell_count} dependent cells`).join(" · ")]])); return details; }); byId("audit-list").replaceChildren(...cards); byId("audit-empty").classList.toggle("hidden", cards.length !== 0); byId("audit-refreshed").textContent = `Last refreshed ${localTime(data.refreshed_at)}`; setError("audit-error"); } catch (error) { setError("audit-error", error.message); } };
  form.addEventListener("submit", (event) => { event.preventDefault(); persistForm(form); load(); }); load(); window.setInterval(load, 5000);
  const actionForm = byId("audit-action-form");
  const auditCommand = (action, presetOverride = null) => ({action, project: actionForm.elements.action_project.value, preset: presetOverride || actionForm.elements.preset.value, environment: actionForm.elements.environment.value, personas: [], lenses: [], documentation_level: "all", publish_findings: action === "audit_start" ? actionForm.elements.publish_findings.checked : false, ...(action === "audit_start" ? {production: false} : {})});
  byId("plan-audit").addEventListener("click", () => prepareAction(auditCommand("audit_plan"), "audit-error"));
  byId("start-audit").addEventListener("click", () => prepareAction(auditCommand("audit_start"), "audit-error"));
  byId("documentation-audit").addEventListener("click", () => prepareAction(auditCommand("audit_plan", "documentation"), "audit-error"));
}

function setupCommands() {
  const root = byId("command-groups"); if (!root) return;
  fetch("/api/dashboard/commands", {cache: "no-store"}).then((response) => response.ok ? response.json() : Promise.reject()).then((data) => {
    const groups = new Map(); for (const item of data.items) { if (!groups.has(item.category)) groups.set(item.category, []); groups.get(item.category).push(item); }
    root.replaceChildren(...[...groups.entries()].map(([category, items]) => { const section = node("section", "panel command-group"); section.append(node("h3", "", category)); for (const item of items) { const form = node("form", "command-form"); const heading = node("div", "command-heading"); heading.append(node("strong", "", item.label), badge(item.effect), status(item.status)); form.append(heading); for (const field of item.fields) { const label = node("label", "", field.name.replaceAll("_", " ")); let input; if (field.type === "select") { input = node("select"); for (const option of field.values) { const value = node("option", "", option); value.value = option; input.append(value); } } else { input = node("input"); input.type = field.type === "uuid" ? "text" : field.type; if (field.maxlength) input.maxLength = field.maxlength; if (field.value !== undefined) { if (field.type === "checkbox") input.checked = Boolean(field.value); else input.value = field.value; } } input.name = field.name; label.append(input); form.append(label); } if (item.href) { const readLink = link(item.href, item.label); readLink.className = "button-link"; form.append(readLink); } else { const button = node("button", "", item.label); button.type = "submit"; button.disabled = item.status !== "AVAILABLE"; form.append(button); form.addEventListener("submit", (event) => { event.preventDefault(); const command = {action: item.action}; for (const field of item.fields) { const input = form.elements[field.name]; command[field.name] = field.type === "checkbox" ? input.checked : input.value; } prepareAction(command, "commands-error"); }); } if (item.blockers.length) form.append(node("p", "muted", item.blockers.join(" · "))); section.append(form); } return section; }));
  }).catch(() => setError("commands-error", "Command registry is temporarily unavailable."));
}

function setupRecordList(kind) {
  const form = byId(`${kind}-filters`); if (!form) return; const body = byId(`${kind}-rows`); const empty = byId(`${kind}-empty`); const refreshed = byId(`${kind}-refreshed`); const errorId = `${kind}-error`;
  const load = async () => { try { const response = await fetch(`/api/dashboard/${kind}?${queryFrom(form)}`, {cache: "no-store"}); if (!response.ok) throw new Error(); const data = await response.json(); const fields = kind === "findings" ? ["finding_id", "project", "domain", "persona", "severity", "risk_class", "disposition", "affected_component", "recommended_action", "updated_at"] : ["enhancement_id", "project", "campaign_id", "persona", "affected_repository", "risk_class", "status", "summary", "updated_at"]; body.replaceChildren(...data.items.map((item) => { const row = node("tr"); for (const field of fields) row.append(field.endsWith("_at") ? timestampCell(item[field]) : cell(item[field])); return row; })); empty.classList.toggle("hidden", data.items.length !== 0); refreshed.textContent = `Observed ${localTime(data.observed_at)} · ${data.source}`; setError(errorId); } catch (_) { setError(errorId, `Persisted ${kind} are temporarily unavailable.`); } };
  form.addEventListener("submit", (event) => { event.preventDefault(); load(); }); load();
}

function setupConnections() {
  const root = byId("connection-cards"); if (!root) return;
  const load = () => fetch("/api/dashboard/connections", {cache: "no-store"}).then((response) => response.ok ? response.json() : Promise.reject()).then((data) => { root.replaceChildren(...data.items.map((item) => { const card = node("article", `health-card${item.status === "STALE" ? " stale" : ""}`); card.append(node("h3", "", item.component), status(item.status), list([["Target", item.logical_target], ["Checked", localTime(item.checked_at)], ["Freshness", elapsed(item.freshness_seconds)], ["Latency", has(item.latency_ms) ? `${item.latency_ms}ms` : null], ["Last success", localTime(item.last_success_at)], ["Last failure", localTime(item.last_failure_at)], ["Source", item.source], ["Error", item.safe_error], ["Next action", item.next_action]])); return card; })); byId("connections-empty").classList.toggle("hidden", data.items.length !== 0); byId("connections-freshness").textContent = `${data.items.length} redacted connection snapshots`; setError("connections-error"); }).catch(() => setError("connections-error", "Connection snapshots are temporarily unavailable."));
  byId("recheck-connections").addEventListener("click", () => prepareAction({action: "refresh_connections", scope: "all"}, "connections-error")); load(); window.setInterval(load, 10000);
}

document.addEventListener("DOMContentLoaded", () => {
  setupActionDialog();
  const page = document.body.dataset.page;
  if (page === "overview") setupOverview();
  if (page === "backlog") setupBacklog();
  if (page === "workflows") { setupWorkflows(); setupWorkflowDetail(); }
  if (page === "logs") { setupLogs(); setupExecution(); }
  if (page === "audits") setupAudits();
  if (page === "commands") setupCommands();
  if (page === "findings") setupRecordList("findings");
  if (page === "enhancements") setupRecordList("enhancements");
  if (page === "connections") setupConnections();
});
