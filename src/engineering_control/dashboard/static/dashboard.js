"use strict";

const byId = (id) => document.getElementById(id);
const textCell = (value) => {
  const cell = document.createElement("td");
  cell.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  return cell;
};
const localTime = (value) => {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "medium"}).format(parsed);
};
const setError = (id, message) => {
  const node = byId(id);
  if (!node) return;
  node.textContent = message || "";
  node.classList.toggle("hidden", !message);
};
const statusNode = (status) => {
  const span = document.createElement("span");
  span.className = `status status-${status}`;
  span.textContent = status;
  return span;
};
const queryFromForm = (form) => {
  const query = new URLSearchParams();
  for (const [key, value] of new FormData(form).entries()) if (String(value)) query.set(key, String(value));
  return query;
};

function setupBacklog() {
  const form = byId("backlog-filters");
  if (!form) return;
  let page = 1;
  let pages = 1;
  const load = async () => {
    const query = queryFromForm(form);
    query.set("page", String(page));
    query.set("page_size", "50");
    try {
      const response = await fetch(`/api/dashboard/backlog?${query}`, {cache: "no-store"});
      if (!response.ok) throw new Error("Backlog data is temporarily unavailable.");
      const data = await response.json();
      const body = byId("backlog-rows");
      const rows = data.items.map((item) => {
        const row = document.createElement("tr");
        const issue = document.createElement("td");
        if (item.issue_url && item.issue_number) {
          const link = document.createElement("a");
          link.href = item.issue_url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = `#${item.issue_number}`;
          issue.append(link);
        } else issue.textContent = item.issue_number ? `#${item.issue_number}` : "—";
        const status = document.createElement("td");
        status.append(statusNode(item.normalized_status));
        row.append(issue, textCell(item.title), textCell(item.project), status, textCell(item.raw_status), textCell(item.current_phase), textCell(localTime(item.queued_at)), textCell(localTime(item.started_at)), textCell(localTime(item.ended_at)), textCell(localTime(item.last_activity_at)), textCell(item.blocker), textCell(item.workflow_id));
        return row;
      });
      body.replaceChildren(...rows);
      pages = data.pages;
      page = Math.min(page, pages);
      byId("backlog-page").textContent = `Page ${page} of ${pages} · ${data.total} items`;
      byId("backlog-prev").disabled = page <= 1;
      byId("backlog-next").disabled = page >= pages;
      byId("backlog-empty").classList.toggle("hidden", rows.length !== 0);
      byId("last-refreshed").textContent = `Last refreshed ${localTime(data.refreshed_at)}`;
      setError("backlog-error", "");
    } catch (error) { setError("backlog-error", error.message); }
  };
  form.addEventListener("submit", (event) => { event.preventDefault(); page = 1; load(); });
  byId("backlog-prev").addEventListener("click", () => { if (page > 1) { page -= 1; load(); } });
  byId("backlog-next").addEventListener("click", () => { if (page < pages) { page += 1; load(); } });
  load();
  window.setInterval(load, 5000);
}

function setupActivity() {
  const form = byId("activity-filters");
  if (!form) return;
  const shell = byId("activity-shell");
  let events = [];
  let paused = false;
  const render = () => {
    const values = Object.fromEntries(new FormData(form).entries());
    const filtered = events.filter((item) => {
      const source = `${item.source || ""} ${item.worker_kind || ""}`.toLowerCase();
      const haystack = `${item.event_type || ""} ${item.message || ""} ${item.current_phase || ""}`.toLowerCase();
      return (!values.project || item.project === values.project) && (!values.level || item.level === values.level) && (!values.source || source.includes(values.source.toLowerCase())) && (!values.workflow || (item.workflow_id || "").toLowerCase().includes(values.workflow.toLowerCase())) && (!values.search || haystack.includes(values.search.toLowerCase()));
    });
    const rows = filtered.map((item) => {
      const row = document.createElement("tr");
      row.append(textCell(localTime(item.timestamp)), textCell(item.level), textCell(item.event_type), textCell(`${item.source}${item.worker_kind ? ` / ${item.worker_kind}` : ""}`), textCell(item.project), textCell(item.workflow_id), textCell(item.current_phase), textCell(item.message));
      return row;
    });
    byId("activity-rows").replaceChildren(...rows);
    byId("activity-empty").classList.toggle("hidden", rows.length !== 0);
    if (!paused) shell.scrollTop = shell.scrollHeight;
  };
  const append = (item) => { events.push(item); if (events.length > 1000) events = events.slice(-1000); render(); };
  const state = (label, className) => { const node = byId("stream-state"); node.textContent = label; node.className = `connection ${className}`; };
  fetch("/api/dashboard/activity/recent?limit=500", {cache: "no-store"}).then((response) => { if (!response.ok) throw new Error(); return response.json(); }).then((data) => { events = data.items.slice(-500); render(); setError("activity-error", ""); }).catch(() => setError("activity-error", "Recent activity is temporarily unavailable."));
  const stream = new EventSource("/api/dashboard/activity/stream");
  stream.addEventListener("open", () => state("Connected", "connected"));
  stream.addEventListener("error", () => state(stream.readyState === EventSource.CLOSED ? "Disconnected" : "Reconnecting", stream.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting"));
  stream.addEventListener("activity", (event) => { try { append(JSON.parse(event.data)); } catch (_) { setError("activity-error", "An invalid activity event was ignored."); } });
  form.addEventListener("input", render);
  byId("autoscroll").addEventListener("click", (event) => { paused = !paused; event.currentTarget.setAttribute("aria-pressed", String(paused)); event.currentTarget.textContent = paused ? "Resume autoscroll" : "Pause autoscroll"; if (!paused) shell.scrollTop = shell.scrollHeight; });
}

function setupAudits() {
  const form = byId("audit-filters");
  if (!form) return;
  const load = async () => {
    try {
      const response = await fetch(`/api/dashboard/audits?${queryFromForm(form)}`, {cache: "no-store"});
      if (!response.ok) throw new Error("Audit data is temporarily unavailable.");
      const data = await response.json();
      const cards = data.items.map((item) => {
        const details = document.createElement("details");
        details.className = "audit";
        const summary = document.createElement("summary");
        const identity = document.createElement("span"); identity.textContent = `${item.project} · ${item.preset || item.name} · ${item.campaign_id}`;
        summary.append(identity, statusNode(item.normalized_status));
        const progress = document.createElement("progress"); progress.max = 100; progress.value = item.completion_percentage; progress.setAttribute("aria-label", `${item.completion_percentage}% complete`);
        const caption = document.createElement("p"); caption.textContent = `${item.completion_percentage}% · ${item.total_cells} coverage cells · last activity ${localTime(item.last_activity_at)}`;
        const grid = document.createElement("dl"); grid.className = "audit-grid";
        const fields = [["Workflow", item.workflow_id], ["Environment", item.environment], ["Raw status", item.raw_status], ["Queued", localTime(item.queued_at)], ["Started", localTime(item.started_at)], ["Ended", localTime(item.ended_at)], ["Queued cells", item.queued_cells], ["Running cells", item.running_cells], ["Passed cells", item.passed_cells], ["Blocked cells", item.blocked_cells], ["Failed cells", item.failed_cells], ["Skipped cells", item.skipped_cells], ["Surface", item.repository_surface], ["Domain", item.domain], ["Persona", item.persona], ["Flow", item.flow], ["Lens", item.lens], ["Candidate findings", item.candidate_findings], ["Confirmed findings", item.confirmed_findings], ["Published findings", item.published_findings], ["Blocker", item.blocker]];
        for (const [label, value] of fields) { const wrapper = document.createElement("div"); const term = document.createElement("dt"); term.textContent = label; const description = document.createElement("dd"); description.textContent = value === null || value === undefined || value === "" ? "—" : String(value); wrapper.append(term, description); grid.append(wrapper); }
        details.append(summary, progress, caption, grid);
        return details;
      });
      byId("audit-list").replaceChildren(...cards);
      byId("audit-empty").classList.toggle("hidden", cards.length !== 0);
      byId("audit-refreshed").textContent = `Last refreshed ${localTime(data.refreshed_at)}`;
      setError("audit-error", "");
    } catch (error) { setError("audit-error", error.message); }
  };
  form.addEventListener("submit", (event) => { event.preventDefault(); load(); });
  load();
  window.setInterval(load, 5000);
}

document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  if (page === "backlog") setupBacklog();
  if (page === "activity") setupActivity();
  if (page === "audits") setupAudits();
});
