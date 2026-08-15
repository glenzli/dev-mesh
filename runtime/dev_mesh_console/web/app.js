import { applyTranslations, diagnosticLabel, eventLabel, language, t, toggleLanguage } from "/i18n.js";
import { renderFlow } from "/graph.js";
import {
  projectHasSignal,
  renderProjectOverview,
  selectableProjects,
} from "/project_overview.js";

const state = {
  dashboard: null,
  workspace: new URLSearchParams(location.search).get("workspace") || "",
  window: Number(new URLSearchParams(location.search).get("window") || 48),
  loading: false,
  collecting: false,
  flowScrollToLatest: true,
  runClosePreview: null,
};

const nodes = Object.fromEntries([
  "connection-status", "refresh", "workspace", "window", "generated-at", "protocol-version",
  "metrics", "insights", "projects", "project-count", "active-details", "active-count", "flow-summary",
  "project-collaboration", "project-collaboration-count",
  "flow", "flow-scroll", "flow-scrollbar", "flow-scrollbar-control", "flow-owner-rail", "flow-tooltip", "flow-empty", "diagnostics", "diagnostic-count",
  "events", "event-count", "language", "theme", "add-root", "root-dialog", "root-form",
  "root-path", "root-list", "dialog-error", "close-dialog", "cancel-root",
  "run-close-dialog", "run-close-form", "run-close-facts", "run-close-warning",
  "run-close-outcome", "run-close-reviewer", "run-close-reason", "run-close-evidence",
  "run-close-confirm", "run-close-error", "close-run-dialog", "cancel-run-close", "confirm-run-close",
].map((id) => [id, document.getElementById(id)]));

const reviewableRunDiagnostics = new Set([
  "run.join-only",
  "run.open-without-claim",
  "run.unclosed",
  "run.stale",
]);

const pendingDiagnosticCodes = new Set([
  "message.ack-stale",
  "claim.heartbeat-aging",
  "claim.finalization-pending",
  "claim.baseline-acknowledgement-pending",
  "claim.completion-pending",
  "contention.finalization-pending",
  "work.finalization-pending",
]);

const actionableDiagnosticCodes = new Set([
  "run.join-only",
  "run.open-without-claim",
  "run.unclosed",
  "run.stale",
  "claim.heartbeat-stale",
  "claim.owner-run-missing",
  "claim.pending-without-contention",
  "claim.pending-after-cancel",
  "contention.live-stalled",
  "contention.orphaned",
  "handoff.source-terminal",
  "transaction.owner-run-missing",
  "transaction.initialization-needs-attention",
  "transaction.refresh-needs-attention",
  "direct-commit.needs-attention",
  "direct-commit.owner-run-missing",
  "cleanup.needs-attention",
  "cleanup.archive-pending",
  "observer.collection-incomplete",
  "observer.workspace-not-observed",
]);

function formatNumber(value) {
  return new Intl.NumberFormat(language() === "zh" ? "zh-CN" : "en").format(value ?? 0);
}

function formatTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(language() === "zh" ? "zh-CN" : "en", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatCompactAge(value) {
  if (!value) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return language() === "zh" ? "刚刚" : "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}${language() === "zh" ? "分" : "m"}`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}${language() === "zh" ? "时" : "h"}`;
  return `${Math.floor(seconds / 86400)}${language() === "zh" ? "天" : "d"}`;
}

function short(value, length = 36) {
  if (!value) return "—";
  return value.length > length ? `${value.slice(0, length - 1)}…` : value;
}

function div(className, text) {
  const node = document.createElement("div");
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function empty(titleKey, bodyKey) {
  const node = div("empty-state compact-empty");
  const title = document.createElement("strong");
  title.textContent = t(titleKey);
  const body = document.createElement("span");
  body.textContent = t(bodyKey);
  node.append(title, body);
  return node;
}

function activeTotal(active) {
  return Object.values(active ?? {}).reduce((total, value) => total + Number(value), 0);
}

function visibleProjects() {
  const scoped = state.dashboard.projects.filter(
    (project) => !state.workspace || project.workspace_id === state.workspace,
  );
  return state.workspace ? scoped : scoped.filter(projectHasSignal);
}

function projectNames() {
  return new Map((state.dashboard?.projects ?? []).map((project) => [project.workspace_id, project.name]));
}

function updateStatus(kind, key) {
  nodes["connection-status"].className = `status-pill is-${kind}`;
  nodes["connection-status"].textContent = t(key);
}

function diagnosticDisposition(item) {
  if (pendingDiagnosticCodes.has(item.code)) return "pending";
  if (item.severity === "critical" || actionableDiagnosticCodes.has(item.code)) return "action";
  return "audit";
}

function partitionDiagnostics(values) {
  return values.reduce((groups, item) => {
    groups[diagnosticDisposition(item)].push(item);
    return groups;
  }, {action: [], pending: [], audit: []});
}

function updateTopbarActions() {
  const labels = [
    ["add-root", "actions.addRoot"],
    ["refresh", state.collecting ? "actions.refreshing" : "actions.refresh"],
    ["language", "actions.language"],
    ["theme", "actions.theme"],
  ];
  labels.forEach(([id, key]) => {
    const node = nodes[id];
    const label = t(key);
    node.title = label;
    node.setAttribute("aria-label", label);
  });
  nodes.language.textContent = language() === "zh" ? "EN" : "中";
  nodes.refresh.classList.toggle("is-loading", state.collecting);
  nodes.refresh.setAttribute("aria-busy", String(state.collecting));
}

function renderMetrics() {
  const dashboard = state.dashboard;
  const operational = dashboard.operational;
  const eventCount = dashboard.projects.reduce((sum, item) => sum + item.event_count, 0);
  const diagnostics = operational.diagnostics ?? [];
  const grouped = partitionDiagnostics(diagnostics);
  const hasCritical = grouped.action.some((item) => item.severity === "critical") || dashboard.collector.last_error;
  const diagnosticTone = hasCritical ? "danger" : grouped.action.length ? "attention" : "neutral";
  const values = [
    ["workspaces", t("metrics.workspaces"), visibleProjects().length, "neutral"],
    ["active", t("metrics.active"), activeTotal(operational.active), activeTotal(operational.active) ? "attention" : "neutral"],
    ["results", t("metrics.results"), operational.work_results?.recorded ?? 0, "good"],
    ["events", t("metrics.events"), eventCount, "neutral"],
    ["diagnostics", t("metrics.diagnostics"), grouped.action.length, diagnosticTone],
  ];
  nodes.metrics.replaceChildren(...values.map(([key, label, value, tone]) => {
    const card = div(`metric-card ${tone}`);
    card.dataset.metric = key;
    const text = document.createElement("span");
    text.textContent = label;
    const number = document.createElement("strong");
    number.textContent = typeof value === "number" ? formatNumber(value) : value;
    card.append(text, number);
    return card;
  }));
}

function insightCard(title, primary, facts, detail = "") {
  const card = div("insight-card");
  const heading = div("insight-heading");
  const label = document.createElement("span");
  label.textContent = title;
  const value = document.createElement("strong");
  value.textContent = primary;
  heading.append(label, value);
  const factRow = div("insight-facts");
  facts.forEach(([number, text, tone = ""]) => {
    const fact = div(`insight-fact ${tone}`.trim());
    const count = document.createElement("b");
    count.textContent = formatNumber(number);
    const caption = document.createElement("span");
    caption.textContent = text;
    fact.append(count, caption);
    factRow.append(fact);
  });
  card.append(heading, factRow);
  if (detail) card.append(div("insight-detail", detail));
  return card;
}

function renderInsights() {
  const operational = state.dashboard.operational;
  const contention = operational.contention;
  const transactions = operational.transaction_outcomes;
  const direct = operational.direct_commit;
  const pending = operational.pending_acknowledgements ?? {
    count: 0,
    requested: 0,
    acknowledged: 0,
    lifecycle_resolved: 0,
    historical: 0,
    oldest_at: null,
  };
  const hot = (contention.hot_paths ?? []).slice(0, 3)
    .map((item) => `${short(item.path, 22)} ×${formatNumber(item.count)}`)
    .join(" · ");
  nodes.insights.replaceChildren(
    insightCard(
      t("insights.contention"),
      `${formatNumber(contention.resolved)} / ${formatNumber(contention.opened)}`,
      [
        [contention.active, t("insights.active"), contention.active ? "attention" : ""],
        [contention.conflicts, t("insights.conflicts"), contention.conflicts ? "danger" : ""],
      ],
      hot || t("insights.noHotPaths"),
    ),
    insightCard(
      t("insights.transactions"),
      formatNumber(transactions.published),
      [
        [transactions.aborted, t("insights.aborted"), transactions.aborted ? "attention" : ""],
        [transactions.conflicted, t("insights.conflicted"), transactions.conflicted ? "danger" : ""],
        [direct.completed, t("insights.directCommits")],
      ],
    ),
    insightCard(
      t("insights.pending"),
      formatNumber(pending.count),
      [
        [pending.acknowledged, t("insights.acknowledged")],
        [pending.lifecycle_resolved, t("insights.lifecycleResolved")],
        [pending.historical, t("insights.historical")],
      ],
      pending.oldest_at ? `${t("insights.oldest")} ${formatCompactAge(pending.oldest_at)}` : "",
    ),
    insightCard(
      t("insights.protocolOnly"),
      formatNumber(operational.non_collaborative_runs.length),
      [
        [operational.event_count, t("insights.totalEvents")],
        [activeTotal(operational.active), t("insights.currentAuthority")],
      ],
    ),
  );
}

function renderWorkspaceOptions() {
  const current = state.workspace;
  nodes.workspace.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = t("filters.allWorkspaces");
  nodes.workspace.append(all);
  selectableProjects(state.dashboard.projects, current).forEach((project) => {
      const option = document.createElement("option");
      option.value = project.workspace_id;
      option.textContent = project.name;
      nodes.workspace.append(option);
    });
  if (state.dashboard.projects.some((item) => item.workspace_id === current)) {
    nodes.workspace.value = current;
  } else {
    state.workspace = "";
    nodes.workspace.value = "";
  }
}

function selectWorkspace(workspaceId) {
  state.workspace = state.workspace === workspaceId ? "" : workspaceId;
  nodes.workspace.value = state.workspace;
  loadDashboard();
}

function renderProjectCollaboration() {
  const result = renderProjectOverview(
    nodes["project-collaboration"],
    state.dashboard.project_collaboration,
    {
      current: state.workspace,
      translate: t,
      formatNumber,
      onSelect: selectWorkspace,
    },
  );
  nodes["project-collaboration-count"].textContent = formatNumber(result.relationCount);
}

function renderProjects() {
  const projects = visibleProjects()
    .sort((left, right) => {
    const leftScore = activeTotal(left.active) * 10000 + left.diagnostic_count * 1000 + left.event_count;
    const rightScore = activeTotal(right.active) * 10000 + right.diagnostic_count * 1000 + right.event_count;
    return rightScore - leftScore || left.name.localeCompare(right.name);
  });
  nodes["project-count"].textContent = formatNumber(projects.length);
  if (!projects.length) {
    nodes.projects.replaceChildren(empty("empty.quietProjectsTitle", "empty.quietProjectsBody"));
    return;
  }
  nodes.projects.replaceChildren(...projects.map((project) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `project-row${state.workspace === project.workspace_id ? " is-selected" : ""}`;
    const identity = div("project-identity");
    const title = document.createElement("strong");
    title.textContent = project.name;
    const root = document.createElement("span");
    root.textContent = project.root;
    identity.append(title, root);
    const facts = div("project-facts");
    const projectDiagnostics = state.dashboard.operational.diagnostics.filter(
      (item) => item.workspace_id === project.workspace_id,
    );
    const issueTone = projectDiagnostics.some((item) => item.severity === "critical")
      ? "is-danger"
      : projectDiagnostics.length
        ? "is-attention"
        : "";
    const factsData = [
      [project.event_count, t("project.events")],
      [activeTotal(project.active), t("project.active")],
      [project.diagnostic_count, project.diagnostic_count ? t("project.issues") : t("project.clean")],
    ];
    factsData.forEach(([value, label], index) => {
      const fact = div(index === 2 && project.diagnostic_count ? `fact ${issueTone}` : "fact");
      const number = document.createElement("b");
      number.textContent = formatNumber(value);
      const text = document.createElement("span");
      text.textContent = label;
      fact.append(number, text);
      facts.append(fact);
    });
    button.append(identity, facts);
    button.addEventListener("click", () => selectWorkspace(project.workspace_id));
    return button;
  }));
}

function renderActive() {
  const details = state.dashboard.active_details;
  nodes["active-count"].textContent = formatNumber(details.length);
  if (!details.length) {
    nodes["active-details"].replaceChildren(empty("empty.activeTitle", "empty.activeBody"));
    return;
  }
  const names = projectNames();
  nodes["active-details"].replaceChildren(...details.map((item) => {
    const row = div("active-row");
    const icon = div(`active-icon kind-${item.kind}`, item.kind.slice(0, 1).toUpperCase());
    const identity = div("active-identity");
    const title = document.createElement("strong");
    title.textContent = t(`active.${item.kind}`);
    const meta = document.createElement("span");
    meta.textContent = [names.get(item.workspace_id), short(item.owner), short(item.scope || item.object_id)].filter(Boolean).join(" · ");
    identity.append(title, meta);
    const status = document.createElement("span");
    status.className = "object-status";
    const statusKey = `objectStatus.${item.status || "active"}`;
    status.textContent = t(statusKey) === statusKey ? (item.status || "active") : t(statusKey);
    row.append(icon, identity, status);
    return row;
  }));
}

function renderDiagnostics() {
  const values = state.dashboard.operational.diagnostics;
  const groups = partitionDiagnostics(values);
  nodes["diagnostic-count"].textContent = formatNumber(groups.action.length);
  nodes["diagnostic-count"].title = t("diagnostic.summary", {
    action: formatNumber(groups.action.length),
    pending: formatNumber(groups.pending.length),
    audit: formatNumber(groups.audit.length),
  });
  if (!values.length) {
    nodes.diagnostics.replaceChildren(empty("empty.diagnosticsTitle", "empty.diagnosticsBody"));
    return;
  }
  const names = projectNames();
  const renderItem = (item, disposition) => {
    const pending = disposition === "pending";
    const audit = disposition === "audit";
    const row = div(`diagnostic-row severity-${item.severity}${pending ? " is-pending" : ""}${audit ? " is-audit" : ""}`);
    const badge = document.createElement("span");
    badge.className = "severity-badge";
    badge.textContent = pending
      ? t("diagnostic.pendingBadge")
      : audit ? t("diagnostic.auditBadge") : t(`severity.${item.severity}`);
    const identity = div("diagnostic-identity");
    const code = document.createElement("strong");
    code.textContent = diagnosticLabel(item.code);
    const meta = document.createElement("span");
    meta.textContent = pending
      ? [
          names.get(item.workspace_id),
          item.source_owner && item.target_owner ? `${short(item.source_owner)} → ${short(item.target_owner)}` : "",
          item.at ? `${t("diagnostic.waitingFor")} ${formatCompactAge(item.at)}` : "",
          item.topic,
        ].filter(Boolean).join(" · ")
      : [names.get(item.workspace_id), short(item.object_id)].filter(Boolean).join(" · ");
    meta.title = [item.code, item.object_id].filter(Boolean).join(" · ");
    identity.append(code, meta);
    row.append(badge, identity);
    if (reviewableRunDiagnostics.has(item.code)) {
      const action = document.createElement("button");
      action.type = "button";
      action.className = "button secondary diagnostic-action";
      action.textContent = t("recovery.review");
      action.addEventListener("click", () => {
        openRunCloseReview(item).catch((error) => {
          updateStatus("error", "status.error");
          console.error(error);
        });
      });
      row.append(action);
    }
    return row;
  };
  const children = [];
  [
    ["action", "diagnostic.group.action"],
    ["pending", "diagnostic.group.pending"],
    ["audit", "diagnostic.group.audit"],
  ].forEach(([kind, label]) => {
    if (!groups[kind].length) return;
    const heading = div(`diagnostic-group-heading is-${kind}`);
    heading.textContent = `${t(label)} · ${formatNumber(groups[kind].length)}`;
    children.push(heading, ...groups[kind].map((item) => renderItem(item, kind)));
  });
  nodes.diagnostics.replaceChildren(...children);
}

async function openRunCloseReview(item) {
  const preview = await request("/api/actions/run-close/preview", {
    method: "POST",
    body: JSON.stringify({workspace_id: item.workspace_id, run_id: item.object_id}),
  });
  state.runClosePreview = preview;
  const names = projectNames();
  const facts = [
    [t("recovery.project"), names.get(preview.workspace_id) || preview.workspace_root],
    [t("recovery.owner"), preview.owner],
    [t("recovery.run"), preview.run_id],
    [t("recovery.task"), preview.task || "—"],
    [t("recovery.blockers"), preview.blockers.reference_count || t("recovery.noBlockers")],
  ];
  nodes["run-close-facts"].replaceChildren(...facts.flatMap(([label, value]) => {
    const key = document.createElement("b");
    key.textContent = label;
    const text = document.createElement("span");
    text.textContent = String(value);
    text.title = String(value);
    return [key, text];
  }));
  nodes["run-close-warning"].hidden = !preview.authority_preserved;
  nodes["run-close-warning"].textContent = preview.authority_preserved
    ? t("recovery.authorityWarning")
    : "";
  nodes["run-close-outcome"].replaceChildren(...preview.allowed_outcomes.map((outcome) => {
    const option = document.createElement("option");
    option.value = outcome;
    option.textContent = t(`recovery.outcome.${outcome}`);
    return option;
  }));
  nodes["run-close-outcome"].value = preview.authority_preserved ? "abandoned" : "completed";
  nodes["run-close-reviewer"].value = localStorage.getItem("dev-mesh-reviewer") || "local-operator";
  nodes["run-close-reason"].value = "operator-reviewed";
  nodes["run-close-evidence"].value = "";
  nodes["run-close-confirm"].value = "";
  nodes["run-close-error"].hidden = true;
  nodes["run-close-dialog"].showModal();
}

function renderEvents() {
  const events = [...state.dashboard.events].reverse();
  nodes["event-count"].textContent = formatNumber(events.length);
  if (!events.length) {
    nodes.events.replaceChildren(empty("empty.eventsTitle", "empty.eventsBody"));
    return;
  }
  const names = projectNames();
  nodes.events.replaceChildren(...events.slice(0, 80).map((event) => {
    const row = div("event-row");
    const dot = div(`event-dot effect-${event.authority_effect}`);
    const identity = div("event-identity");
    const title = document.createElement("strong");
    title.textContent = eventLabel(event.event);
    const meta = document.createElement("span");
    meta.textContent = [names.get(event.workspace_id), short(event.owner), short(event.scope)].filter(Boolean).join(" · ");
    identity.append(title, meta);
    const time = document.createElement("time");
    time.dateTime = event.at;
    time.textContent = formatTime(event.at);
    row.append(dot, identity, time);
    return row;
  }));
}

function renderGraph() {
  const result = renderFlow(
    nodes.flow,
    nodes["flow-owner-rail"],
    nodes["flow-tooltip"],
    state.dashboard,
    projectNames(),
  );
  nodes["flow-empty"].hidden = result.eventCount !== 0;
  nodes["flow-scroll"].classList.toggle("is-empty", result.eventCount === 0);
  nodes["flow-scrollbar"].hidden = result.eventCount === 0;
  nodes["flow-owner-rail"].hidden = result.eventCount === 0;
  if (state.flowScrollToLatest && result.eventCount) {
    requestAnimationFrame(() => scrollFlowToLatest());
    state.flowScrollToLatest = false;
  } else {
    updateFlowScrollbar();
  }
  const pieces = [
    `${formatNumber(result.laneCount)} ${t("flow.owners")}`,
    `${formatNumber(result.runCount)} ${t("flow.runSegments")}`,
    `${formatNumber(result.eventCount)} ${t("flow.events")}`,
  ];
  if (result.workCount) pieces.push(`${formatNumber(result.workCount)} ${t("flow.works")}`);
  if (state.dashboard.selection.events_truncated) pieces.push(t("flow.truncated"));
  nodes["flow-summary"].textContent = pieces.join(" · ");
}

function updateFlowScrollbar() {
  const flow = nodes["flow-scroll"];
  const control = nodes["flow-scrollbar-control"];
  const maximum = Math.max(0, flow.scrollWidth - flow.clientWidth);
  control.max = String(maximum);
  control.value = String(Math.min(maximum, flow.scrollLeft));
  control.disabled = maximum === 0;
  const visible = flow.scrollWidth > 0 ? (flow.clientWidth / flow.scrollWidth) * 100 : 100;
  control.style.setProperty("--flow-scroll-thumb", `${Math.max(12, Math.min(100, visible))}%`);
}

function scrollFlowToLatest() {
  const target = Math.max(0, nodes["flow-scroll"].scrollWidth - nodes["flow-scroll"].clientWidth);
  nodes["flow-scroll"].scrollLeft = target;
  updateFlowScrollbar();
}

function render() {
  applyTranslations();
  updateTopbarActions();
  renderWorkspaceOptions();
  renderMetrics();
  renderInsights();
  renderProjectCollaboration();
  renderProjects();
  renderActive();
  renderGraph();
  renderDiagnostics();
  renderEvents();
  nodes["generated-at"].textContent = formatTime(state.dashboard.generated_at);
  nodes["protocol-version"].textContent = state.dashboard.protocol_version;
  const diagnostics = state.dashboard.operational.diagnostics ?? [];
  const groups = partitionDiagnostics(diagnostics);
  const needsAttention = groups.action.length > 0 || state.dashboard.collector.last_error;
  const pending = groups.pending.length > 0 && !needsAttention;
  updateStatus(
    needsAttention || pending ? "degraded" : "ready",
    needsAttention ? "status.review" : pending ? "status.pending" : "status.ready",
  );
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.message || `HTTP ${response.status}`);
  return value;
}

async function loadDashboard() {
  if (state.loading) return;
  state.loading = true;
  updateStatus("loading", "status.loading");
  const query = new URLSearchParams({ window: String(state.window), limit: "240" });
  if (state.workspace) query.set("workspace", state.workspace);
  history.replaceState(null, "", `?${query}`);
  try {
    state.dashboard = await request(`/api/dashboard?${query}`);
    render();
  } catch (error) {
    updateStatus("error", "status.error");
    nodes.metrics.replaceChildren(empty("status.error", error.message));
  } finally {
    state.loading = false;
  }
}

async function collect() {
  state.flowScrollToLatest = true;
  nodes.refresh.disabled = true;
  state.collecting = true;
  updateTopbarActions();
  try {
    await request("/api/collect", { method: "POST", body: "{}" });
    await loadDashboard();
  } catch (error) {
    updateStatus("error", "status.error");
  } finally {
    nodes.refresh.disabled = false;
    state.collecting = false;
    updateTopbarActions();
  }
}

async function openRootDialog() {
  nodes["dialog-error"].hidden = true;
  const value = await request("/api/roots");
  nodes["root-list"].replaceChildren(...value.roots.map((root) => div("root-chip", root)));
  nodes["root-path"].value = "";
  nodes["root-dialog"].showModal();
}

function applyTheme(value) {
  if (value === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = value;
  localStorage.setItem("dev-mesh-theme", value);
}

nodes.refresh.addEventListener("click", collect);
nodes.workspace.addEventListener("change", () => {
  state.workspace = nodes.workspace.value;
  state.flowScrollToLatest = true;
  loadDashboard();
});
nodes.window.addEventListener("change", () => {
  state.window = Number(nodes.window.value);
  state.flowScrollToLatest = true;
  loadDashboard();
});
nodes["flow-scroll"].addEventListener("scroll", () => {
  updateFlowScrollbar();
});
nodes["flow-scrollbar-control"].addEventListener("input", () => {
  nodes["flow-scroll"].scrollLeft = Number(nodes["flow-scrollbar-control"].value);
});
nodes.language.addEventListener("click", () => {
  toggleLanguage();
  if (state.dashboard) render();
});
nodes.theme.addEventListener("click", () => {
  const current = localStorage.getItem("dev-mesh-theme") || "system";
  applyTheme(current === "system" ? "light" : current === "light" ? "dark" : "system");
});
nodes["add-root"].addEventListener("click", () => {
  openRootDialog().catch((error) => {
    updateStatus("error", "status.error");
    console.error(error);
  });
});
nodes["close-dialog"].addEventListener("click", () => nodes["root-dialog"].close());
nodes["cancel-root"].addEventListener("click", () => nodes["root-dialog"].close());
nodes["root-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  nodes["dialog-error"].hidden = true;
  try {
    await request("/api/roots", {
      method: "POST",
      body: JSON.stringify({ path: nodes["root-path"].value }),
    });
    nodes["root-dialog"].close();
    state.flowScrollToLatest = true;
    await loadDashboard();
  } catch (error) {
    nodes["dialog-error"].textContent = error.message;
    nodes["dialog-error"].hidden = false;
  }
});
nodes["close-run-dialog"].addEventListener("click", () => nodes["run-close-dialog"].close());
nodes["cancel-run-close"].addEventListener("click", () => nodes["run-close-dialog"].close());
nodes["run-close-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  const preview = state.runClosePreview;
  if (!preview) return;
  nodes["run-close-error"].hidden = true;
  if (nodes["run-close-confirm"].value !== preview.run_id) {
    nodes["run-close-error"].textContent = t("recovery.confirmMismatch");
    nodes["run-close-error"].hidden = false;
    return;
  }
  nodes["confirm-run-close"].disabled = true;
  try {
    const reviewer = nodes["run-close-reviewer"].value;
    await request("/api/actions/run-close", {
      method: "POST",
      body: JSON.stringify({
        workspace_id: preview.workspace_id,
        run_id: preview.run_id,
        review_token: preview.review_token,
        reviewer,
        outcome: nodes["run-close-outcome"].value,
        reason_code: nodes["run-close-reason"].value,
        evidence: nodes["run-close-evidence"].value,
      }),
    });
    localStorage.setItem("dev-mesh-reviewer", reviewer);
    nodes["run-close-dialog"].close();
    state.runClosePreview = null;
    state.flowScrollToLatest = true;
    await loadDashboard();
  } catch (error) {
    nodes["run-close-error"].textContent = error.message;
    nodes["run-close-error"].hidden = false;
  } finally {
    nodes["confirm-run-close"].disabled = false;
  }
});

nodes.window.value = String(state.window);
applyTheme(localStorage.getItem("dev-mesh-theme") || "system");
applyTranslations();
loadDashboard();
setInterval(() => {
  if (!document.hidden) loadDashboard();
}, 15000);
