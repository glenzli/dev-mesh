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

  function formatNumber(value) {
    return new Intl.NumberFormat(preferences.locale).format(Number(value || 0));
  }

  function shortPath(value) {
    if (!value) return t("workspace.unknown");
    const parts = String(value).split("/").filter(Boolean);
    return parts.slice(-2).join("/") || String(value);
  }

  function setNumber(id, value) {
    $(id).textContent = formatNumber(value);
  }

  function empty(message) {
    return node("p", "empty", message);
  }

  function compactRow(title, subtitle, value, tone = "") {
    const row = node("div", "diagnostic-row");
    const main = node("div", "row-main");
    main.append(node("span", "row-title", title), node("span", "row-subtitle", subtitle));
    row.append(main, node("span", `row-value${tone ? ` ${tone}` : ""}`, value));
    return row;
  }

  function renderConflictHotspots(conflicts) {
    const target = $("conflict-hotspot-list");
    const rows = conflicts?.resource_hotspots || [];
    target.replaceChildren();
    if (!rows.length) {
      target.append(empty(t("conflicts.noHotspots")));
      return;
    }
    rows.slice(0, 5).forEach((item) => {
      target.append(compactRow(
        item.resource,
        item.refresh_conflicts
          ? t("conflicts.refresh")
          : item.contentions
            ? t("conflicts.contentions")
            : t("conflicts.blocked"),
        t("conflicts.signalCount", { count: formatNumber(item.signals) }),
        "warning-text",
      ));
    });
  }

  function renderTransactions(transactions) {
    const target = $("transaction-list");
    const rows = transactions?.recent || [];
    target.replaceChildren();
    if (!rows.length) {
      target.append(empty(t("transactions.noData")));
      return;
    }
    rows.slice(0, 4).forEach((item) => {
      const status = t(`transaction.status.${item.status || "observed"}`);
      target.append(compactRow(
        item.transaction_id,
        shortPath(item.workspace_root),
        status,
        item.conflicted || item.attention ? "warning-text" : "",
      ));
    });
  }

  function renderSoloRuns(protocolUse) {
    const target = $("solo-run-list");
    const rows = protocolUse?.solo_runs || [];
    target.replaceChildren();
    if (!rows.length) {
      target.append(empty(t("protocolUse.noSolo")));
      return;
    }
    rows.slice(0, 4).forEach((item) => {
      target.append(compactRow(
        item.run_id,
        `${shortPath(item.workspace_root)} · ${item.owner || "—"}`,
        t("protocolUse.eventCount", { count: formatNumber(item.protocol_events) }),
      ));
    });
  }

  function render(analytics = {}) {
    const conflicts = analytics.conflicts || {};
    const conflictSummary = conflicts.summary || {};
    setNumber("conflict-signal-count", conflictSummary.signals);
    setNumber("conflict-contention-count", conflictSummary.contentions);
    setNumber("conflict-refresh-count", conflictSummary.refresh_conflicts);
    setNumber("conflict-blocked-count", conflictSummary.queue_blocked);
    setNumber("conflict-attention-count", conflictSummary.attention);
    renderConflictHotspots(conflicts);

    const transactions = analytics.transactions || {};
    const transactionSummary = transactions.summary || {};
    setNumber("transaction-observed-count", transactionSummary.observed);
    setNumber("transaction-published-count", transactionSummary.published);
    setNumber("transaction-conflicted-count", transactionSummary.conflicted);
    setNumber("transaction-aborted-count", transactionSummary.aborted);
    renderTransactions(transactions);

    const protocolUse = analytics.protocol_use || {};
    const protocolSummary = protocolUse.summary || {};
    setNumber("protocol-solo-count", protocolSummary.solo_protocol);
    setNumber("protocol-collaborative-count", protocolSummary.collaborative);
    setNumber("protocol-lifecycle-count", protocolSummary.lifecycle_only);
    setNumber("protocol-open-count", protocolSummary.open_unclassified);
    renderSoloRuns(protocolUse);
  }

  window.DevMeshAnalyticsView = { render };
})();
