"use strict";

(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const preferences = window.DevMeshPreferences;
  const $ = (id) => document.getElementById(id);
  const t = (key, variables) => preferences.t(key, variables);
  const NODE_RADIUS = 6;
  const COLUMN_STEP = 36;
  const LANE_HEIGHT = 36;
  const TOP_OFFSET = 28;
  const TOOLTIP_WIDTH = 214;
  let currentStory = { lanes: [], nodes: [], links: [], summary: {} };
  let selectedId = null;
  let renderedWorkspace = null;

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

  function translate(prefix, value, fallback = value) {
    const key = `${prefix}.${value || "observed"}`;
    const translated = t(key);
    return translated === key ? String(fallback || value || "") : translated;
  }

  function statusLabel(status) {
    return translate("storyline.status", status, translate("graph.status", status, status));
  }

  function typeLabel(type) {
    return translate("storyline.type", type, type);
  }

  function laneLabel(lane) {
    if (lane.kind === "coordination") return t("storyline.lane.coordination");
    if (lane.kind === "system") return t("storyline.lane.system");
    return lane.label;
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
      hour12: false,
    }).format(date);
  }

  function truncate(value, maximum) {
    const text = String(value || "");
    return text.length > maximum ? `${text.slice(0, maximum - 1)}…` : text;
  }

  function displayLabel(item) {
    const details = item.details || {};
    if (item.type === "handoff") {
      const source = details.source_owner || "?";
      const target = details.target_owner || "?";
      return `${source} → ${target}`;
    }
    if (item.type === "contention") return t("storyline.intersection");
    if (item.type === "decision") return t("storyline.decision", { mode: item.label });
    if (item.type === "transaction") return truncate(item.label, 18);
    if (item.type === "publish") return t("storyline.publish", { commit: item.label });
    return item.label;
  }

  function displayOwner(item) {
    if (item.owner === "__coordination__") return t("storyline.lane.coordination");
    if (item.owner === "__system__") return t("storyline.lane.system");
    return item.owner;
  }

  function hideTooltip() {
    const tooltip = $("storyline-tooltip");
    tooltip.hidden = true;
    tooltip.removeAttribute("data-node-id");
  }

  function showTooltip(item, position, canvasWidth) {
    const tooltip = $("storyline-tooltip");
    const heading = htmlNode("div", "storyline-tooltip-heading");
    heading.append(
      htmlNode("span", "", typeLabel(item.type)),
      htmlNode("span", `status-${item.status || "observed"}`, statusLabel(item.status)),
    );
    tooltip.className = `storyline-tooltip type-${item.type} status-${item.status || "observed"}`;
    tooltip.replaceChildren(
      heading,
      htmlNode("strong", "", displayLabel(item)),
      htmlNode("span", "storyline-tooltip-owner", displayOwner(item)),
      htmlNode("time", "", formatTime(item.started_at)),
    );
    tooltip.dataset.nodeId = item.id;
    tooltip.style.left = `${Math.max(8, Math.min(position.x + 14, canvasWidth - TOOLTIP_WIDTH - 8))}px`;
    tooltip.style.top = `${position.y > 112 ? position.y - 94 : position.y + 16}px`;
    tooltip.hidden = false;
  }

  function detailRow(label, value) {
    const row = htmlNode("div", "inspector-row");
    row.append(htmlNode("dt", "", label), htmlNode("dd", "", value));
    return row;
  }

  function renderEmptyInspector() {
    const target = $("storyline-inspector");
    target.replaceChildren(
      htmlNode("p", "kicker", t("storyline.inspectorKicker")),
      htmlNode("h3", "", t("storyline.selectNode")),
      htmlNode("p", "", t("storyline.selectHint")),
    );
  }

  function renderInspector(item) {
    const target = $("storyline-inspector");
    const owner = item.owner === "__coordination__"
      ? t("storyline.lane.coordination")
      : item.owner === "__system__"
        ? t("storyline.lane.system")
        : item.owner;
    target.replaceChildren();
    target.append(
      htmlNode("p", "kicker", t("storyline.inspectorKicker")),
      htmlNode("h3", "", displayLabel(item)),
      htmlNode("p", "inspector-subtitle", `${typeLabel(item.type)} · ${statusLabel(item.status)}`),
    );
    const details = htmlNode("dl", "inspector-details");
    details.append(
      detailRow(t("storyline.detail.owner"), owner),
      detailRow(t("storyline.detail.time"), item.started_at === item.last_at
        ? formatTime(item.started_at)
        : `${formatTime(item.started_at)} → ${formatTime(item.last_at)}`),
      detailRow(t("storyline.detail.events"), item.event_count || 0),
    );
    const values = item.details || {};
    ["runs", "scopes", "paths", "semantic_resources", "event_types", "missing_responses"].forEach((key) => {
      const entries = Array.isArray(values[key]) ? values[key] : [];
      if (entries.length) details.append(detailRow(t(`storyline.detail.${key}`), entries.join(" · ")));
    });
    [
      "parent_owner", "source_owner", "target_owner", "intent", "outcome", "summary",
      "recommendation", "recommendation_reason", "coordinator", "lease_until", "reason",
      "decision_revision", "transaction_id", "candidate", "request_id", "request_status",
    ].forEach((key) => {
      if (values[key] !== undefined && values[key] !== null && values[key] !== "") {
        details.append(detailRow(t(`storyline.detail.${key}`), values[key]));
      }
    });
    target.append(details);
  }

  function selectNode(identifier) {
    selectedId = identifier;
    document.querySelectorAll(".story-node").forEach((element) => {
      element.classList.toggle("selected", element.dataset.nodeId === identifier);
    });
    const item = currentStory.nodes.find((candidate) => candidate.id === identifier);
    if (item) renderInspector(item);
  }

  function renderLaneLabels(lanes) {
    const target = $("storyline-lanes");
    target.replaceChildren();
    target.style.paddingTop = `${TOP_OFFSET}px`;
    lanes.forEach((lane) => {
      const row = htmlNode("div", `storyline-lane-label status-${lane.status || "observed"}`);
      row.style.height = `${LANE_HEIGHT}px`;
      row.append(
        htmlNode("strong", "", laneLabel(lane)),
        htmlNode("span", "", t("storyline.laneItems", { count: lane.node_count || 0 })),
      );
      target.append(row);
    });
  }

  function layout(story) {
    const laneIndex = new Map(story.lanes.map((lane, index) => [lane.id, index]));
    const ordered = [...story.nodes].sort((left, right) => {
      const time = String(left.started_at || "").localeCompare(String(right.started_at || ""));
      return time || String(left.id).localeCompare(String(right.id));
    });
    const positions = new Map();
    ordered.forEach((item, index) => {
      positions.set(item.id, {
        x: 24 + index * COLUMN_STEP,
        y: TOP_OFFSET + (laneIndex.get(item.owner) || 0) * LANE_HEIGHT + LANE_HEIGHT / 2,
      });
    });
    return {
      positions,
      width: Math.max(820, 48 + Math.max(0, ordered.length - 1) * COLUMN_STEP),
      height: Math.max(260, TOP_OFFSET + story.lanes.length * LANE_HEIGHT + 20),
    };
  }

  function drawBackground(svg, story, width) {
    const layer = svgNode("g", { class: "story-lane-guides" });
    story.lanes.forEach((lane, index) => {
      const y = TOP_OFFSET + index * LANE_HEIGHT;
      layer.append(svgNode("rect", {
        x: 0,
        y,
        width,
        height: LANE_HEIGHT,
        class: index % 2 ? "lane-even" : "lane-odd",
      }));
      layer.append(svgNode("line", { x1: 0, y1: y + LANE_HEIGHT, x2: width, y2: y + LANE_HEIGHT }));
    });
    svg.append(layer);
  }

  function drawTimeRuler(svg, story, positions, width) {
    const layer = svgNode("g", { class: "story-time-ruler" });
    layer.append(svgNode("line", { x1: 24, y1: 24, x2: width - 24, y2: 24 }));
    story.nodes.forEach((item, index) => {
      const position = positions.get(item.id);
      if (!position) return;
      layer.append(svgNode("line", { x1: position.x, y1: 20, x2: position.x, y2: 28 }));
      if (index % 4 === 0 || story.nodes.length < 12) {
        const label = svgNode("text", { x: position.x + 5, y: 17 });
        label.textContent = formatTime(item.started_at);
        layer.append(label);
      }
    });
    svg.append(layer);
  }

  function drawLinks(svg, story, positions) {
    const layer = svgNode("g", { class: "story-links" });
    story.links.forEach((item) => {
      const source = positions.get(item.source);
      const target = positions.get(item.target);
      if (!source || !target) return;
      const direction = target.x >= source.x ? 1 : -1;
      const startX = source.x + NODE_RADIUS * direction;
      const startY = source.y;
      const endX = target.x - NODE_RADIUS * direction;
      const endY = target.y;
      const delta = Math.max(18, Math.abs(endX - startX) * 0.4);
      const path = svgNode("path", {
        class: `story-link link-${item.type}`,
        d: `M ${startX} ${startY} C ${startX + delta * direction} ${startY}, ${endX - delta * direction} ${endY}, ${endX} ${endY}`,
        "marker-end": "url(#story-arrow)",
      });
      layer.append(path);
    });
    svg.append(layer);
  }

  function drawNodes(svg, story, positions, canvasWidth) {
    const layer = svgNode("g", { class: "story-nodes" });
    story.nodes.forEach((item) => {
      const position = positions.get(item.id);
      if (!position) return;
      const label = displayLabel(item);
      const group = svgNode("g", {
        class: `story-node node-${item.type} status-${item.status || "observed"}`,
        transform: `translate(${position.x} ${position.y})`,
        tabindex: "0",
        role: "button",
        "aria-label": `${typeLabel(item.type)} ${label}, ${statusLabel(item.status)}`,
        "aria-describedby": "storyline-tooltip",
      });
      group.dataset.nodeId = item.id;
      group.append(
        svgNode("circle", { class: "story-node-hit", r: 14 }),
        svgNode("circle", { class: "story-node-ring", r: 10 }),
        svgNode("circle", { class: "story-node-dot", r: NODE_RADIUS }),
      );
      group.addEventListener("mouseenter", () => showTooltip(item, position, canvasWidth));
      group.addEventListener("mouseleave", () => {
        if (document.activeElement !== group) hideTooltip();
      });
      group.addEventListener("focus", () => showTooltip(item, position, canvasWidth));
      group.addEventListener("blur", hideTooltip);
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

  function render(story = {}) {
    const workspaceChanged = renderedWorkspace !== story.workspace_id;
    renderedWorkspace = story.workspace_id || null;
    currentStory = {
      workspace_id: story.workspace_id || null,
      lanes: story.lanes || [],
      nodes: story.nodes || [],
      links: story.links || [],
      summary: story.summary || {},
    };
    const svg = $("collaboration-storyline");
    const empty = $("storyline-empty");
    hideTooltip();
    svg.replaceChildren();
    renderLaneLabels(currentStory.lanes);
    const summary = currentStory.summary;
    $("graph-count").textContent = summary.truncated
      ? t("storyline.truncated", { visible: summary.visible_nodes || 0, total: summary.total_nodes || 0 })
      : t("storyline.count", {
        slices: summary.visible_nodes || 0,
        intersections: summary.intersections || 0,
      });
    if (!currentStory.nodes.length) {
      selectedId = null;
      renderEmptyInspector();
      svg.hidden = true;
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    svg.hidden = false;
    const { positions, width, height } = layout(currentStory);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    const definitions = svgNode("defs");
    const marker = svgNode("marker", {
      id: "story-arrow",
      viewBox: "0 0 10 10",
      refX: "9",
      refY: "5",
      markerWidth: "5",
      markerHeight: "5",
      orient: "auto-start-reverse",
    });
    marker.append(svgNode("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "story-arrow" }));
    definitions.append(marker);
    svg.append(definitions);
    drawBackground(svg, currentStory, width);
    drawTimeRuler(svg, currentStory, positions, width);
    drawLinks(svg, currentStory, positions);
    drawNodes(svg, currentStory, positions, width);
    if (workspaceChanged) {
      const focus = currentStory.nodes.find((item) => item.type === "contention" && item.status === "stalled")
        || currentStory.nodes.find((item) => item.status === "active")
        || currentStory.nodes[currentStory.nodes.length - 1];
      const focusPosition = focus ? positions.get(focus.id) : null;
      if (focusPosition) {
        window.requestAnimationFrame(() => {
          const scroll = $("storyline-scroll");
          scroll.scrollLeft = Math.max(0, focusPosition.x - scroll.clientWidth * 0.34);
        });
      }
    }
    if (selectedId && currentStory.nodes.some((item) => item.id === selectedId)) {
      selectNode(selectedId);
    } else {
      selectedId = null;
      renderEmptyInspector();
    }
  }

  $("storyline-scroll").addEventListener("scroll", (event) => {
    $("storyline-lanes").scrollTop = event.currentTarget.scrollTop;
  });

  window.DevMeshStorylineView = { render };
})();
