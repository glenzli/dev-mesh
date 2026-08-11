"use strict";

(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const preferences = window.DevMeshPreferences;
  const $ = (id) => document.getElementById(id);
  const t = (key, variables) => preferences.t(key, variables);
  let selectedId = null;
  let currentGraph = { nodes: [], edges: [], summary: {} };

  function htmlNode(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function svgNode(tag, attributes = {}) {
    const element = document.createElementNS(SVG_NS, tag);
    Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, String(value)));
    return element;
  }

  function shortPath(value) {
    if (!value) return t("workspace.unknown");
    const parts = String(value).split("/").filter(Boolean);
    return parts.slice(-2).join("/") || String(value);
  }

  function statusLabel(status) {
    const key = `graph.status.${status || "observed"}`;
    const translated = t(key);
    return translated === key ? String(status || t("graph.status.observed")) : translated;
  }

  function typeLabel(type) {
    const key = `graph.type.${type}`;
    const translated = t(key);
    return translated === key ? String(type) : translated;
  }

  function detailRow(label, value) {
    const row = htmlNode("div", "inspector-row");
    row.append(htmlNode("dt", "", label), htmlNode("dd", "", value));
    return row;
  }

  function renderInspector(item) {
    const target = $("graph-inspector");
    target.replaceChildren();
    target.append(
      htmlNode("p", "kicker", t("graph.inspectorKicker")),
      htmlNode("h3", "", item.label),
      htmlNode("p", "inspector-subtitle", `${typeLabel(item.type)} · ${statusLabel(item.status)}`),
    );
    const details = htmlNode("dl", "inspector-details");
    details.append(
      detailRow(t("graph.detail.workspace"), shortPath(item.workspace_root)),
      detailRow(t("graph.detail.status"), statusLabel(item.status)),
      detailRow(t("graph.detail.events"), item.event_count || 0),
    );
    const values = item.details || {};
    ["runs", "claims", "paths", "semantic_resources", "tasks", "missing_responses"].forEach((key) => {
      const entries = Array.isArray(values[key]) ? values[key] : [];
      if (entries.length) details.append(detailRow(t(`graph.detail.${key}`), entries.join(" · ")));
    });
    ["lease_until", "recommendation_reason"].forEach((key) => {
      if (values[key]) details.append(detailRow(t(`graph.detail.${key}`), values[key]));
    });
    target.append(details);
  }

  function renderEmptyInspector() {
    const target = $("graph-inspector");
    target.replaceChildren(
      htmlNode("p", "kicker", t("graph.inspectorKicker")),
      htmlNode("h3", "", t("graph.selectNode")),
      htmlNode("p", "", t("graph.selectHint")),
    );
  }

  function selectNode(identifier) {
    selectedId = identifier;
    document.querySelectorAll(".graph-node").forEach((element) => {
      element.classList.toggle("selected", element.dataset.nodeId === identifier);
    });
    const item = currentGraph.nodes.find((candidate) => candidate.id === identifier);
    if (item) renderInspector(item);
  }

  function layout(nodes) {
    const columnFor = { agent: 0, handoff: 1, contention: 1, transaction: 2, commit: 3 };
    const columns = [[], [], [], []];
    nodes.forEach((item) => columns[columnFor[item.type] ?? 1].push(item));
    columns.forEach((column) => column.sort((left, right) => {
      const statusOrder = { stalled: 0, conflicted: 0, active: 1, offered: 1 };
      const delta = (statusOrder[left.status] ?? 2) - (statusOrder[right.status] ?? 2);
      return delta || String(left.label).localeCompare(String(right.label));
    }));
    const positions = new Map();
    const width = 1160;
    const largest = Math.max(...columns.map((column) => column.length), 1);
    const height = Math.max(390, largest * 78 + 46);
    columns.forEach((column, columnIndex) => {
      const step = height / (column.length + 1);
      column.forEach((item, index) => {
        positions.set(item.id, { x: 24 + columnIndex * 285, y: step * (index + 1) - 28 });
      });
    });
    return { positions, width, height };
  }

  function drawEdges(svg, graph, positions) {
    const layer = svgNode("g", { class: "graph-edges" });
    graph.edges.forEach((item) => {
      const source = positions.get(item.source);
      const target = positions.get(item.target);
      if (!source || !target) return;
      const startX = source.x + 214;
      const startY = source.y + 27;
      let endX = target.x;
      const endY = target.y + 27;
      const sameColumn = Math.abs(startX - endX) < 230;
      let pathData;
      if (sameColumn) {
        endX = target.x + 214;
        pathData = `M ${startX} ${startY} C ${startX + 74} ${startY}, ${endX + 74} ${endY}, ${endX} ${endY}`;
      } else {
        const curve = Math.max(55, Math.abs(endX - startX) * 0.48);
        pathData = `M ${startX} ${startY} C ${startX + curve} ${startY}, ${endX - curve} ${endY}, ${endX} ${endY}`;
      }
      const path = svgNode("path", {
        class: `graph-edge edge-${item.type}`,
        d: pathData,
        "marker-end": "url(#graph-arrow)",
      });
      layer.append(path);
      if (Number(item.count || 0) > 1) {
        const label = svgNode("text", {
          class: "edge-count",
          x: (startX + endX) / 2,
          y: (startY + endY) / 2 - 5,
          "text-anchor": "middle",
        });
        label.textContent = `×${item.count}`;
        layer.append(label);
      }
    });
    svg.append(layer);
  }

  function drawNodes(svg, graph, positions) {
    const layer = svgNode("g", { class: "graph-nodes" });
    graph.nodes.forEach((item) => {
      const position = positions.get(item.id);
      if (!position) return;
      const group = svgNode("g", {
        class: `graph-node node-${item.type} status-${item.status || "observed"}`,
        transform: `translate(${position.x} ${position.y})`,
        tabindex: "0",
        role: "button",
        "aria-label": `${typeLabel(item.type)} ${item.label}, ${statusLabel(item.status)}`,
      });
      group.dataset.nodeId = item.id;
      group.append(svgNode("rect", { width: 214, height: 54, rx: 3 }));
      const type = svgNode("text", { class: "node-type", x: 12, y: 15 });
      type.textContent = typeLabel(item.type).toUpperCase();
      const label = svgNode("text", { class: "node-label", x: 12, y: 34 });
      label.textContent = String(item.label).length > 29 ? `${String(item.label).slice(0, 28)}…` : item.label;
      const status = svgNode("text", { class: "node-status", x: 202, y: 15, "text-anchor": "end" });
      status.textContent = statusLabel(item.status);
      const subtitle = svgNode("text", { class: "node-subtitle", x: 12, y: 48 });
      subtitle.textContent = item.subtitle || shortPath(item.workspace_root);
      group.append(type, label, status, subtitle);
      group.addEventListener("click", () => selectNode(item.id));
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectNode(item.id);
        }
      });
      layer.append(group);
    });
    svg.append(layer);
  }

  function render(graph = {}) {
    currentGraph = { nodes: graph.nodes || [], edges: graph.edges || [], summary: graph.summary || {} };
    const svg = $("collaboration-graph");
    const empty = $("graph-empty");
    svg.replaceChildren();
    const summary = currentGraph.summary;
    $("graph-count").textContent = summary.truncated
      ? t("graph.truncated", { visible: summary.visible_nodes || 0, total: summary.total_nodes || 0 })
      : t("graph.count", { nodes: summary.visible_nodes || 0, edges: summary.visible_edges || 0 });
    if (!currentGraph.nodes.length) {
      selectedId = null;
      renderEmptyInspector();
      svg.hidden = true;
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    svg.hidden = false;
    const { positions, width, height } = layout(currentGraph.nodes);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("height", height);
    const definitions = svgNode("defs");
    const marker = svgNode("marker", {
      id: "graph-arrow",
      viewBox: "0 0 10 10",
      refX: "9",
      refY: "5",
      markerWidth: "6",
      markerHeight: "6",
      orient: "auto-start-reverse",
    });
    marker.append(svgNode("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "graph-arrow" }));
    definitions.append(marker);
    svg.append(definitions);
    drawEdges(svg, currentGraph, positions);
    drawNodes(svg, currentGraph, positions);
    if (selectedId && currentGraph.nodes.some((item) => item.id === selectedId)) selectNode(selectedId);
    else selectNode(currentGraph.nodes[0].id);
  }

  window.DevMeshGraphView = { render };
})();
