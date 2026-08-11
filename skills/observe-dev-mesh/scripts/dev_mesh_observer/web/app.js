"use strict";

const preferences = window.DevMeshPreferences;
const initialParameters = new URLSearchParams(window.location.search);
const supportedWindows = new Set(["1h", "24h", "48h", "7d", "4w"]);
const initialWindow = initialParameters.get("since");
const state = {
  status: null,
  report: null,
  storyline: null,
  issues: [],
  issueTotal: 0,
  since: supportedWindows.has(initialWindow) ? initialWindow : "48h",
  workspaceScope: initialParameters.get("workspace") || "",
  refreshGeneration: 0,
  timer: null,
  observedEventTotal: null,
  recentIngestion: { count: 0, at: 0 },
  timeline: {
    page: 1,
    pageSize: 25,
    anchor: null,
    requestGeneration: 0,
    pagination: null,
  },
};

const $ = (id) => document.getElementById(id);
const t = (key, variables) => preferences.t(key, variables);

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function empty(message) {
  return node("p", "empty", message);
}

function formatNumber(value) {
  return new Intl.NumberFormat(preferences.locale).format(Number(value || 0));
}

function formatTime(value) {
  if (!value) return t("time.unknown");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(preferences.locale, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function shortPath(value) {
  if (!value) return t("workspace.unknown");
  const parts = String(value).split("/").filter(Boolean);
  return parts.slice(-2).join("/") || String(value);
}

function selectedWorkspace() {
  return (state.status?.workspaces || [])
    .find((workspace) => workspace.workspace_id === state.workspaceScope) || null;
}

function syncLocation() {
  const parameters = new URLSearchParams();
  parameters.set("since", state.since);
  if (state.workspaceScope) parameters.set("workspace", state.workspaceScope);
  const query = parameters.toString();
  window.history.replaceState(null, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
}

function validateWorkspaceScope(status) {
  if (
    state.workspaceScope
    && !(status.workspaces || []).some(
      (workspace) => workspace.workspace_id === state.workspaceScope,
    )
  ) {
    state.workspaceScope = "";
    state.storyline = null;
    resetTimelineSnapshot();
    syncLocation();
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  let payload;
  try {
    payload = await response.json();
  } catch (_) {
    payload = { error: `HTTP ${response.status}` };
  }
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function setConnection(kind, label) {
  const container = $("live-state");
  container.classList.remove("online", "error");
  if (kind) container.classList.add(kind);
  $("live-state-label").textContent = label;
}

function renderCollectorStatus() {
  const collector = state.status?.collector || {};
  const container = $("collector-state");
  const label = $("collector-state-label");
  const pending = Number(collector.pending_events || 0);
  const recent = state.recentIngestion;
  const hasRecentIngestion = recent.count > 0 && Date.now() - recent.at < 15000;
  container.classList.remove("online", "warning", "error");

  if (collector.last_error) {
    container.classList.add("error");
    label.textContent = t("collector.error");
    container.title = collector.last_error;
    return;
  }
  container.title = "";
  if (collector.running) {
    container.classList.add("online");
    label.textContent = hasRecentIngestion
      ? t("collector.collectingWithDelta", { count: formatNumber(recent.count) })
      : t("collector.collecting");
    return;
  }
  if (pending > 0) {
    container.classList.add("warning");
    label.textContent = t("collector.pending", { count: formatNumber(pending) });
    return;
  }
  if (!collector.enabled) {
    label.textContent = t("collector.disabled");
    return;
  }
  if (collector.last_success_at) {
    const elapsed = Math.max(
      0,
      Math.floor((Date.now() - new Date(collector.last_success_at).getTime()) / 1000),
    );
    container.classList.add("online");
    label.textContent = hasRecentIngestion
      ? t("collector.liveWithDelta", {
        count: formatNumber(recent.count),
        seconds: formatNumber(elapsed),
      })
      : t("collector.live", { seconds: formatNumber(elapsed) });
    return;
  }
  container.classList.add("warning");
  label.textContent = t("collector.waiting");
}

let toastTimer;
function toast(message, isError = false) {
  const element = $("toast");
  element.textContent = message;
  element.classList.toggle("error", isError);
  element.classList.add("visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => element.classList.remove("visible"), 3600);
}

function renderMetrics() {
  const status = state.status?.summary || {};
  const summary = state.report?.summary || {};
  const workspace = selectedWorkspace();
  $("metric-workspaces").textContent = formatNumber(status.workspaces);
  $("metric-available").textContent = t("metrics.availableGlobal", {
    count: formatNumber(status.available),
  });
  $("metric-events").textContent = formatNumber(summary.events_in_window);
  $("metric-active").textContent = workspace
    ? t("metrics.projectScope", { project: shortPath(workspace.workspace_root) })
    : t("metrics.active", { count: formatNumber(summary.active_workspaces_in_window) });
  $("metric-runs").textContent = formatNumber(summary.runs_open);
  $("metric-run-total").textContent = t("metrics.runs", {
    closed: formatNumber(summary.runs_closed),
    total: formatNumber(summary.runs_joined),
  });
  $("metric-handoffs").textContent = formatNumber(summary.handoffs_pending);
  $("metric-handoff-total").textContent = t("metrics.handoffs", {
    accepted: formatNumber(summary.handoffs_accepted),
    offered: formatNumber(summary.handoffs_offered),
  });
  $("metric-issues").textContent = formatNumber(status.issues);
}

function renderWorkspaces() {
  const target = $("workspace-list");
  const workspaces = state.status?.workspaces || [];
  $("workspace-count").textContent = formatNumber(workspaces.length);
  target.replaceChildren();
  if (!workspaces.length) {
    target.append(empty(t("workspace.none")));
    return;
  }
  workspaces.forEach((workspace) => {
    const row = node("div", "list-row");
    const dot = node("span", `status-dot${workspace.available ? " available" : ""}`);
    const main = node("div", "row-main");
    main.append(
      node("span", "row-title", shortPath(workspace.workspace_root)),
      node("span", "row-subtitle", workspace.workspace_root),
    );
    const value = node("span", "row-value", t("workspace.events", { count: formatNumber(workspace.event_count) }));
    row.append(dot, main, value);
    target.append(row);
  });
}

function renderScanRoots() {
  const target = $("scan-root-list");
  const roots = state.status?.scan_roots || [];
  target.replaceChildren();
  if (!roots.length) {
    target.append(empty(t("workspaceDialog.noRoots")));
    return;
  }
  roots.forEach((root) => target.append(node("div", "root-item", root)));
}

function renderSimpleRows(targetId, rows, keyName, emptyMessage) {
  const target = $(targetId);
  target.replaceChildren();
  if (!rows.length) {
    target.append(empty(emptyMessage));
    return;
  }
  rows.forEach((item) => {
    const row = node("div", "list-row");
    const main = node("div", "row-main");
    main.append(
      node("span", "row-title", item[keyName]),
      node("span", "row-subtitle", shortPath(item.workspace_root)),
    );
    row.append(main);
    target.append(row);
  });
}

function renderInflight() {
  renderSimpleRows("run-list", state.report?.open_runs || [], "run_id", t("inflight.noRuns"));
  renderSimpleRows("handoff-list", state.report?.pending_handoffs || [], "handoff_id", t("inflight.noHandoffs"));
}

function renderBars(targetId, rows, labelKey) {
  const target = $(targetId);
  target.replaceChildren();
  if (!rows.length) {
    target.append(empty(t("activity.none")));
    return;
  }
  const maximum = Math.max(...rows.map((item) => Number(item.events || 0)), 1);
  rows.slice(0, 7).forEach((item) => {
    const block = node("div", "bar-item");
    const label = node("div", "bar-label");
    label.append(
      node("span", "", labelKey === "workspace_root" ? shortPath(item[labelKey]) : item[labelKey]),
      node("strong", "", formatNumber(item.events)),
    );
    const track = node("div", "bar-track");
    const fill = node("div", "bar-fill");
    const percentage = Math.max(10, (Number(item.events || 0) / maximum) * 100);
    fill.dataset.level = String(Math.min(100, Math.ceil(percentage / 10) * 10));
    track.append(fill);
    block.append(label, track);
    target.append(block);
  });
}

function populateSelect(selectId, firstLabel, values, currentValue) {
  const select = $(selectId);
  const previous = currentValue ?? select.value;
  const options = [node("option", "", firstLabel)];
  options[0].value = "";
  values.forEach(({ value, label }) => {
    const option = node("option", "", label);
    option.value = value;
    options.push(option);
  });
  select.replaceChildren(...options);
  select.value = values.some((item) => item.value === previous) ? previous : "";
}

function renderFilterOptions() {
  const workspaces = state.status?.workspaces || [];
  populateSelect(
    "filter-workspace",
    t("filters.allWorkspaces"),
    workspaces.map((workspace) => ({ value: workspace.workspace_id, label: shortPath(workspace.workspace_root) })),
  );
  const eventTypes = Object.keys(state.report?.event_types || {}).sort();
  populateSelect("filter-event", t("filters.allEvents"), eventTypes.map((value) => ({ value, label: value })));
  const owners = (state.report?.owner_activity || []).map((item) => item.owner).sort();
  populateSelect("filter-owner", t("filters.allOwners"), owners.map((value) => ({ value, label: value })));
  populateSelect(
    "scope-workspace",
    t("projectOverview.selector"),
    workspaces.map((workspace) => ({
      value: workspace.workspace_id,
      label: shortPath(workspace.workspace_root),
    })),
    state.workspaceScope,
  );
  const timelineWorkspace = $("filter-workspace");
  timelineWorkspace.disabled = Boolean(state.workspaceScope);
  timelineWorkspace.title = state.workspaceScope ? t("scope.timelineLocked") : "";
  if (state.workspaceScope) timelineWorkspace.value = state.workspaceScope;
}

function selectWorkspaceScope(workspaceId) {
  const nextScope = workspaceId || "";
  if (nextScope === state.workspaceScope) return;
  state.workspaceScope = nextScope;
  state.storyline = null;
  $("scope-workspace").value = state.workspaceScope;
  $("filter-workspace").value = state.workspaceScope;
  $("filter-event").value = "";
  $("filter-owner").value = "";
  $("filter-run").value = "";
  resetTimelineSnapshot();
  syncLocation();
  refreshDashboard();
}

function renderCollaborationView() {
  const overviewVisible = !state.workspaceScope;
  $("project-overview").hidden = !overviewVisible;
  $("storyline-layout").hidden = overviewVisible;
  if (overviewVisible) {
    $("scope-hint").textContent = t("scope.allHint");
    const overview = state.report?.project_overview || {};
    const summary = overview.summary || {};
    $("graph-count").textContent = t("projectOverview.count", {
      projects: formatNumber(summary.projects),
      active: formatNumber(summary.active_projects),
    });
    window.DevMeshProjectOverview.render(
      overview,
      state.status?.workspaces || [],
      selectWorkspaceScope,
    );
    return;
  }
  const project = (state.report?.project_overview?.projects || [])
    .find((item) => item.workspace_id === state.workspaceScope);
  $("scope-hint").textContent = t("scope.projectHint", {
    project: shortPath(project?.workspace_root || selectedWorkspace()?.workspace_root),
  });
  window.DevMeshStorylineView.render(state.storyline || {}, {
    latestProjectAt: project?.last_activity_at || null,
  });
}

function renderIssues() {
  const target = $("issue-list");
  $("issue-count").textContent = formatNumber(state.issueTotal);
  target.replaceChildren();
  if (!state.issues.length) {
    target.append(empty(t("issues.none")));
    return;
  }
  state.issues.forEach((issue) => {
    const row = node("div", "list-row");
    const main = node("div", "row-main");
    main.append(
      node("span", "issue-kind", issue.kind),
      node("span", "row-subtitle", `${shortPath(issue.workspace_root)} · ${issue.detail}`),
    );
    row.append(main, node("span", "row-value", `${formatNumber(issue.occurrences)}×`));
    target.append(row);
  });
}

function resetTimelineSnapshot() {
  state.timeline.page = 1;
  state.timeline.anchor = null;
  state.timeline.pagination = null;
}

function renderEventPagination() {
  const pagination = state.timeline.pagination;
  if (!pagination) return;
  const controls = $("event-pagination");
  controls.hidden = false;
  $("event-page-summary").textContent = t("events.pageSummary", {
    page: formatNumber(pagination.page),
    pages: formatNumber(pagination.pages),
    count: formatNumber(pagination.total),
  });
  $("event-page-previous").disabled = !pagination.has_previous;
  $("event-page-next").disabled = !pagination.has_next;
  $("event-page-size").value = String(pagination.page_size);
  const newer = $("event-newer");
  newer.hidden = !pagination.newer;
  newer.textContent = pagination.newer
    ? t("events.newer", { count: formatNumber(pagination.newer) })
    : "";
}

async function loadEvents(expectedGeneration = state.refreshGeneration) {
  const requestGeneration = ++state.timeline.requestGeneration;
  const parameters = new URLSearchParams({
    limit: String(state.timeline.pageSize),
    page: String(state.timeline.page),
  });
  if (state.timeline.anchor !== null) {
    parameters.set("anchor", String(state.timeline.anchor));
  }
  const filters = {
    workspace_id: state.workspaceScope || $("filter-workspace").value,
    event: $("filter-event").value,
    owner: $("filter-owner").value,
    run_id: $("filter-run").value.trim(),
  };
  Object.entries(filters).forEach(([key, value]) => {
    if (value) parameters.set(key, value);
  });
  const payload = await api(`/api/v1/events?${parameters}`);
  if (
    expectedGeneration !== state.refreshGeneration
    || requestGeneration !== state.timeline.requestGeneration
  ) return;
  state.timeline.pagination = payload.pagination;
  state.timeline.page = payload.pagination.page;
  state.timeline.pageSize = payload.pagination.page_size;
  state.timeline.anchor = payload.pagination.anchor;
  renderEventPagination();
  const target = $("event-list");
  target.replaceChildren();
  if (!payload.events.length) {
    target.append(empty(t("events.none")));
    return;
  }
  payload.events.forEach((event) => {
    const button = node("button", "event-row");
    button.type = "button";
    button.append(
      node("span", "event-time", formatTime(event.event_at || event.ingested_at)),
      node("span", "event-name", event.event_type || t("events.unknown")),
      node("span", "event-meta", event.owner || event.scope || "—"),
      node("span", "event-meta", event.run_id || event.transaction_id || shortPath(event.workspace_root)),
    );
    button.addEventListener("click", () => showEvent(event));
    target.append(button);
  });
}

async function showEvent(event) {
  try {
    const parameters = new URLSearchParams({ workspace_id: event.workspace_id, source_name: event.source_name });
    const detail = await api(`/api/v1/event?${parameters}`);
    const meta = $("detail-meta");
    meta.replaceChildren();
    [detail.event_type, detail.owner, detail.scope, detail.run_id, shortPath(detail.workspace_root)]
      .filter(Boolean)
      .forEach((value) => meta.append(node("span", "chip", value)));
    $("detail-json").textContent = JSON.stringify(detail.payload, null, 2);
    $("event-dialog").showModal();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderDashboard() {
  renderCollectorStatus();
  renderMetrics();
  window.DevMeshAnalyticsView.render(
    state.report?.coordination_analytics || {},
    state.report?.coordination_state || {},
  );
  renderFilterOptions();
  renderCollaborationView();
  renderWorkspaces();
  renderScanRoots();
  renderInflight();
  const eventTypeActivity = Object.entries(state.report?.event_types || {})
    .map(([eventType, events]) => ({ event_type: eventType, events }));
  $("activity-primary-title").textContent = state.workspaceScope
    ? t("activity.eventTypes")
    : t("activity.workspace");
  renderBars(
    "workspace-activity",
    state.workspaceScope ? eventTypeActivity : state.report?.workspace_activity || [],
    state.workspaceScope ? "event_type" : "workspace_root",
  );
  renderBars("owner-activity", state.report?.owner_activity || [], "owner");
  renderBars("resource-activity", state.report?.resource_activity || [], "resource");
  renderIssues();
  $("last-updated").textContent = t("footer.updated", { time: formatTime(new Date().toISOString()) });
}

async function refreshDashboard({ quiet = false } = {}) {
  const generation = ++state.refreshGeneration;
  if (!quiet) setConnection("", t("connection.refreshing"));
  try {
    const status = await api("/api/v1/status");
    if (generation !== state.refreshGeneration) return;
    validateWorkspaceScope(status);
    const reportParameters = new URLSearchParams({ since: state.since, limit: "10" });
    const issueParameters = new URLSearchParams({ limit: "100" });
    if (state.workspaceScope) {
      reportParameters.set("workspace_id", state.workspaceScope);
      issueParameters.set("workspace_id", state.workspaceScope);
    }
    let storylineRequest = Promise.resolve(null);
    if (state.workspaceScope) {
      const parameters = new URLSearchParams({
        since: state.since,
        workspace_id: state.workspaceScope,
      });
      parameters.set("limit", "28");
      storylineRequest = api(`/api/v1/storyline?${parameters}`);
    }
    const [report, storyline, issues] = await Promise.all([
      api(`/api/v1/report?${reportParameters}`),
      storylineRequest,
      api(`/api/v1/issues?${issueParameters}`),
    ]);
    if (generation !== state.refreshGeneration) return;
    const eventTotal = Number(status.summary?.events || 0);
    if (state.observedEventTotal !== null && eventTotal > state.observedEventTotal) {
      state.recentIngestion = {
        count: eventTotal - state.observedEventTotal,
        at: Date.now(),
      };
    }
    state.observedEventTotal = eventTotal;
    state.status = status;
    state.report = report;
    state.storyline = storyline;
    state.issues = issues.issues;
    state.issueTotal = Number(issues.total ?? issues.issues.length);
    renderDashboard();
    await loadEvents(generation);
    if (generation !== state.refreshGeneration) return;
    setConnection("online", t("connection.online"));
  } catch (error) {
    if (generation !== state.refreshGeneration) return;
    setConnection("error", t("connection.failed"));
    if (!quiet) toast(error.message, true);
  }
}

async function collectNow() {
  const button = $("collect-button");
  button.disabled = true;
  setConnection("", t("connection.collecting"));
  try {
    const result = await api("/api/v1/collect", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dev-Mesh-Console": "1" },
      body: "{}",
    });
    const count = result.collection?.inserted || 0;
    toast(t("toast.collectComplete", { count: formatNumber(count) }));
    await refreshDashboard({ quiet: true });
  } catch (error) {
    toast(error.message, true);
    setConnection("error", t("connection.collectFailed"));
  } finally {
    button.disabled = false;
  }
}

function openWorkspaceDialog() {
  renderScanRoots();
  $("workspace-dialog").showModal();
  window.requestAnimationFrame(() => $("workspace-root").focus());
}

function closeWorkspaceDialog() {
  $("workspace-dialog").close();
}

async function addWorkspace(event) {
  event.preventDefault();
  const submit = $("workspace-submit");
  submit.disabled = true;
  submit.textContent = t("workspaceDialog.submitting");
  try {
    const result = await api("/api/v1/workspaces", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dev-Mesh-Console": "1" },
      body: JSON.stringify({
        root: $("workspace-root").value.trim(),
        max_depth: Number($("workspace-depth").value),
      }),
    });
    const discovered = result.discovery?.discovered || 0;
    const inserted = result.collection?.inserted || 0;
    toast(discovered
      ? t("toast.workspaceAdded", { count: formatNumber(discovered), events: formatNumber(inserted) })
      : t("toast.rootAdded"));
    $("workspace-root").value = "";
    closeWorkspaceDialog();
    await refreshDashboard({ quiet: true });
  } catch (error) {
    toast(error.message, true);
  } finally {
    submit.disabled = false;
    submit.textContent = t("workspaceDialog.submit");
  }
}

preferences.translateDocument();
$("locale-select").value = preferences.locale;
$("theme-select").value = preferences.theme;
$("since-select").value = state.since;

$("locale-select").addEventListener("change", (event) => {
  preferences.setLocale(event.target.value);
  refreshDashboard();
});
$("theme-select").addEventListener("change", (event) => preferences.setTheme(event.target.value));
$("since-select").addEventListener("change", (event) => {
  state.since = event.target.value;
  syncLocation();
  refreshDashboard();
});
$("scope-workspace").addEventListener("change", (event) => {
  selectWorkspaceScope(event.target.value);
});
$("refresh-button").addEventListener("click", () => {
  resetTimelineSnapshot();
  refreshDashboard();
});
$("collect-button").addEventListener("click", collectNow);
$("event-filters").addEventListener("submit", (event) => {
  event.preventDefault();
  resetTimelineSnapshot();
  loadEvents().catch((error) => toast(error.message, true));
});
$("event-page-previous").addEventListener("click", () => {
  state.timeline.page -= 1;
  loadEvents().catch((error) => toast(error.message, true));
});
$("event-page-next").addEventListener("click", () => {
  state.timeline.page += 1;
  loadEvents().catch((error) => toast(error.message, true));
});
$("event-page-size").addEventListener("change", (event) => {
  state.timeline.pageSize = Number(event.target.value);
  state.timeline.page = 1;
  loadEvents().catch((error) => toast(error.message, true));
});
$("event-newer").addEventListener("click", () => {
  resetTimelineSnapshot();
  loadEvents().catch((error) => toast(error.message, true));
});
$("dialog-close").addEventListener("click", () => $("event-dialog").close());
$("event-dialog").addEventListener("click", (event) => {
  if (event.target === $("event-dialog")) $("event-dialog").close();
});
$("add-workspace-button").addEventListener("click", openWorkspaceDialog);
$("workspace-form").addEventListener("submit", addWorkspace);
$("workspace-dialog-close").addEventListener("click", closeWorkspaceDialog);
$("workspace-cancel").addEventListener("click", closeWorkspaceDialog);
$("workspace-dialog").addEventListener("click", (event) => {
  if (event.target === $("workspace-dialog")) closeWorkspaceDialog();
});

syncLocation();
refreshDashboard();
state.timer = window.setInterval(() => {
  if (!document.hidden) refreshDashboard({ quiet: true });
}, 5000);
