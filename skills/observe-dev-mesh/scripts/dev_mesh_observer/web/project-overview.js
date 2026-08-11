"use strict";

(() => {
  const preferences = window.DevMeshPreferences;
  const $ = (id) => document.getElementById(id);
  const t = (key, variables) => preferences.t(key, variables);

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function shortPath(value) {
    if (!value) return t("workspace.unknown");
    const parts = String(value).split("/").filter(Boolean);
    return parts.slice(-2).join("/") || String(value);
  }

  function formatNumber(value) {
    return new Intl.NumberFormat(preferences.locale).format(Number(value || 0));
  }

  function formatTime(value) {
    if (!value) return t("projectOverview.noActivity");
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(preferences.locale, {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }

  function projectStatus(project, available) {
    if (!available) return "unavailable";
    if (Number(project.stalled_contentions || 0) > 0) return "stalled";
    if (Number(project.active_contentions || 0) > 0) return "coordinating";
    if (Number(project.active_runs || 0) > 0) return "active";
    if (Number(project.events_in_window || 0) > 0) return "observed";
    return "quiet";
  }

  function metric(label, value, warning = false) {
    const item = node("div", warning ? "project-metric warning" : "project-metric");
    item.append(node("strong", "", formatNumber(value)), node("span", "", label));
    return item;
  }

  function render(overview = {}, workspaces = [], onSelect = () => {}) {
    const target = $("project-overview-grid");
    const availability = new Map(
      workspaces.map((workspace) => [workspace.workspace_id, Boolean(workspace.available)]),
    );
    const projects = Array.isArray(overview.projects) ? overview.projects : [];
    target.replaceChildren();
    if (!projects.length) {
      target.append(node("p", "empty project-overview-empty", t("projectOverview.empty")));
      return;
    }
    projects.forEach((project) => {
      const available = availability.get(project.workspace_id) !== false;
      const status = projectStatus(project, available);
      const card = node("button", `project-card status-${status}`);
      card.type = "button";
      card.setAttribute(
        "aria-label",
        t("projectOverview.openAria", { project: shortPath(project.workspace_root) }),
      );
      const header = node("div", "project-card-header");
      const identity = node("div", "project-identity");
      identity.append(
        node("strong", "", shortPath(project.workspace_root)),
        node("span", "", project.workspace_root),
      );
      header.append(
        identity,
        node("span", `project-status status-${status}`, t(`projectOverview.status.${status}`)),
      );
      const metrics = node("div", "project-metrics");
      metrics.append(
        metric(t("projectOverview.events"), project.events_in_window),
        metric(t("projectOverview.activeRuns"), project.active_runs),
        metric(t("projectOverview.pendingHandoffs"), project.pending_handoffs),
        metric(
          t("projectOverview.contentions"),
          project.active_contentions,
          Number(project.stalled_contentions || 0) > 0,
        ),
        metric(t("projectOverview.transactions"), project.transactions_observed),
        metric(t("projectOverview.signals"), project.collaboration_signals),
      );
      const footer = node("div", "project-card-footer");
      footer.append(
        node("span", "", t("projectOverview.lastActivity", {
          time: formatTime(project.last_activity_at),
        })),
        node("strong", "", t("projectOverview.openGraph")),
      );
      card.append(header, metrics, footer);
      card.addEventListener("click", () => onSelect(project.workspace_id));
      target.append(card);
    });
  }

  window.DevMeshProjectOverview = { render };
})();
