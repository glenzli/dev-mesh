"use strict";

(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const preferences = window.DevMeshPreferences;
  const $ = (id) => document.getElementById(id);
  const t = (key, variables) => preferences.t(key, variables);
  const TOOLTIP_WIDTH = 244;
  let currentStory = { lanes: [], spans: [], markers: [], relations: [], summary: {}, focus: {} };
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
    Object.entries(attributes).forEach(([name, value]) => {
      if (value !== undefined && value !== null) element.setAttribute(name, String(value));
    });
    return element;
  }

  function arrowMarker(id, className) {
    const marker = svgNode("marker", {
      id,
      viewBox: "0 0 10 10",
      refX: "9",
      refY: "5",
      markerWidth: "5",
      markerHeight: "5",
      orient: "auto-start-reverse",
    });
    marker.append(svgNode("path", { d: "M 0 0 L 10 5 L 0 10 z", class: className }));
    return marker;
  }

  function abortGlyph(className, x, y, size = 4.2) {
    return svgNode("path", {
      class: className,
      d: `M ${x - size} ${y - size} L ${x + size} ${y + size} M ${x + size} ${y - size} L ${x - size} ${y + size}`,
    });
  }

  function translate(prefix, value, fallback = value) {
    const key = `${prefix}.${value || "observed"}`;
    const translated = t(key);
    return translated === key ? String(fallback || value || "") : translated;
  }

  function statusLabel(value) {
    return translate("storyline.status", value, translate("graph.status", value, value));
  }

  function kindLabel(value) {
    return translate("storyline.type", value, value);
  }

  function laneLabel(lane, actorNumber = null) {
    if (lane.kind === "canonical") return t("storyline.lane.canonical", { branch: lane.label });
    if (lane.kind === "system") return t("storyline.lane.system");
    return t("storyline.lane.actor", { index: String(actorNumber || 0).padStart(2, "0") });
  }

  function formatTime(value, seconds = false) {
    if (!value) return t("time.unknown");
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(preferences.locale, {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: seconds ? "2-digit" : undefined,
      hour12: false,
    }).format(date);
  }

  function latestRecordedAt(story) {
    const values = [
      ...(story.spans || []).flatMap((item) => [item.started_at, item.ended_at]),
      ...(story.markers || []).flatMap((item) => [item.at, item.last_at]),
      ...(story.relations || []).flatMap((item) => [item.at, item.last_at]),
    ].filter(Boolean);
    return values.sort((left, right) => String(left).localeCompare(String(right))).at(-1) || null;
  }

  function earliestRecordedAt(story) {
    const values = [
      ...(story.spans || []).flatMap((item) => [item.started_at, item.ended_at]),
      ...(story.markers || []).map((item) => item.at),
      ...(story.relations || []).map((item) => item.at),
    ].filter(Boolean);
    return values.sort((left, right) => String(left).localeCompare(String(right)))[0] || null;
  }

  function itemTimeLabel(item) {
    const start = item.started_at || item.at;
    const terminal = item.ended_at
      || (item.last_at && item.last_at !== start ? item.last_at : null);
    return terminal
      ? `${formatTime(start, true)} → ${formatTime(terminal, true)}`
      : formatTime(start, true);
  }

  function displayLabel(item) {
    if (item.kind === "fork") return t("storyline.relation.fork");
    if (item.kind === "rejoin") return t("storyline.relation.rejoin");
    if (item.kind === "waits-for" || item.kind === "blocked") return t("storyline.relation.waiting");
    if (item.kind === "diverts-to") return t("storyline.relation.diverted");
    if (item.kind === "reassigned" || item.kind === "handoff") return t("storyline.relation.handoff");
    if (item.kind === "message") return t("storyline.relation.message");
    if (item.kind === "contention") return t("storyline.intersection");
    return item.label || item.id;
  }

  function itemOwner(item) {
    if (item.owner) return item.owner;
    if (item.lane) return item.lane;
    if (item.source_owner && item.target_owner) return `${item.source_owner} → ${item.target_owner}`;
    if (item.owners?.length) return item.owners.join(" · ");
    return "__canonical__";
  }

  function displayOwner(item) {
    const owner = itemOwner(item);
    if (owner === "__canonical__") return t("storyline.lane.canonical", { branch: "" });
    if (owner === "__system__") return t("storyline.lane.system");
    return owner;
  }

  function endpointLabel(endpoint) {
    if (!endpoint?.state) return "";
    return t(`storyline.endpoint.${endpoint.state}`);
  }

  function laneMoment(value) {
    const parsed = new Date(value || "").getTime();
    return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
  }

  function orderLanes(lanes, mode) {
    if (mode === "window") return [...lanes];
    const canonical = lanes.filter((lane) => lane.kind === "canonical");
    const agents = lanes.filter((lane) => lane.kind === "agent").sort((left, right) => (
      Number(right.open_runs || 0) - Number(left.open_runs || 0)
      || laneMoment(right.last_at) - laneMoment(left.last_at)
      || laneMoment(right.started_at) - laneMoment(left.started_at)
      || String(left.id).localeCompare(String(right.id))
    ));
    const remaining = lanes.filter((lane) => !["canonical", "agent"].includes(lane.kind));
    return [...canonical, ...agents, ...remaining];
  }

  function allItems() {
    return [...currentStory.spans, ...currentStory.markers, ...currentStory.relations];
  }

  function itemById(identifier) {
    return allItems().find((item) => item.id === identifier);
  }

  function hideTooltip() {
    const tooltip = $("storyline-tooltip");
    tooltip.hidden = true;
    tooltip.removeAttribute("data-item-id");
  }

  function showTooltip(item, position, canvasWidth) {
    const tooltip = $("storyline-tooltip");
    const heading = htmlNode("div", "storyline-tooltip-heading");
    heading.append(
      htmlNode("span", "", kindLabel(item.kind)),
      htmlNode("span", `status-${item.status || "observed"}`, statusLabel(item.status)),
    );
    tooltip.className = `storyline-tooltip type-${item.kind} status-${item.status || "observed"}`;
    const contents = [
      heading,
      htmlNode("strong", "", displayLabel(item)),
      htmlNode("span", "storyline-tooltip-owner", displayOwner(item)),
    ];
    if (item.target_endpoint) {
      contents.push(htmlNode(
        "span",
        "storyline-tooltip-endpoint",
        endpointLabel(item.target_endpoint),
      ));
    }
    contents.push(htmlNode("time", "", itemTimeLabel(item)));
    tooltip.replaceChildren(...contents);
    tooltip.dataset.itemId = item.id;
    tooltip.style.left = `${Math.max(8, Math.min(position.x + 12, canvasWidth - TOOLTIP_WIDTH - 8))}px`;
    tooltip.style.top = `${Math.max(8, position.y > 104 ? position.y - 92 : position.y + 15)}px`;
    tooltip.hidden = false;
  }

  function detailRow(label, value) {
    const row = htmlNode("div", "inspector-row");
    row.append(htmlNode("dt", "", label), htmlNode("dd", "", value));
    return row;
  }

  function renderEmptyInspector() {
    $("storyline-inspector").replaceChildren(
      htmlNode("p", "kicker", t("storyline.inspectorKicker")),
      htmlNode("h3", "", t("storyline.selectNode")),
      htmlNode("p", "", t("storyline.selectHint")),
    );
  }

  function renderInspector(item) {
    const target = $("storyline-inspector");
    target.replaceChildren(
      htmlNode("p", "kicker", t("storyline.inspectorKicker")),
      htmlNode("h3", "", displayLabel(item)),
      htmlNode("p", "inspector-subtitle", `${kindLabel(item.kind)} · ${statusLabel(item.status)}`),
    );
    const details = htmlNode("dl", "inspector-details");
    details.append(
      detailRow(t("storyline.detail.owner"), displayOwner(item)),
      detailRow(t("storyline.detail.time"), itemTimeLabel(item)),
    );
    const displayRun = item.run_id || item.inferred_run_id;
    if (displayRun) {
      details.append(detailRow(t("storyline.detail.run_id"), displayRun));
    }
    if (
      ["session", "claim", "waiting", "diverted"].includes(item.kind)
      && (!item.run_id || item.run_binding === "inferred")
    ) {
      details.append(detailRow(
        t("storyline.detail.run_binding"),
        translate("storyline.runBinding", item.run_binding || "unbound", item.run_binding || "unbound"),
      ));
    }
    if (item.trace_quality) {
      details.append(
        detailRow(
          t("storyline.detail.trace_quality"),
          translate("storyline.quality", item.trace_quality, item.trace_quality),
        ),
      );
    }
    if (item.evidence) details.append(detailRow(t("storyline.detail.evidence"), item.evidence));
    if (item.target_endpoint) {
      details.append(detailRow(
        t("storyline.detail.target_endpoint"),
        endpointLabel(item.target_endpoint),
      ));
      if (item.target_endpoint.run_id) {
        details.append(detailRow(
          t("storyline.detail.target_run_id"),
          item.target_endpoint.run_id,
        ));
      }
      details.append(detailRow(
        t("storyline.detail.target_endpoint_evidence"),
        t(`storyline.endpointEvidence.${item.target_endpoint.evidence}`),
      ));
    }
    if (item.event_count) details.append(detailRow(t("storyline.detail.events"), item.event_count));
    const values = item.details || {};
    Object.entries(values).forEach(([key, value]) => {
      if (value === null || value === undefined || value === "") return;
      const rendered = ["run_inference", "run_inference_authority"].includes(key)
        ? translate(`storyline.${key}`, value, value)
        : Array.isArray(value)
        ? value.map((entry) => typeof entry === "object" ? JSON.stringify(entry) : entry).join(" · ")
        : typeof value === "object" ? JSON.stringify(value) : value;
      if (rendered !== "") details.append(detailRow(t(`storyline.detail.${key}`), rendered));
    });
    target.append(details);
  }

  function selectItem(identifier) {
    selectedId = identifier;
    document.querySelectorAll(".trace-item").forEach((element) => {
      element.classList.toggle("selected", element.dataset.itemId === identifier);
    });
    const item = itemById(identifier);
    if (item) renderInspector(item);
  }

  function makeInteractive(element, item, position, width) {
    element.classList.add("trace-item");
    element.dataset.itemId = item.id;
    element.setAttribute("tabindex", "0");
    element.setAttribute("role", "button");
    const endpoint = item.target_endpoint ? `, ${endpointLabel(item.target_endpoint)}` : "";
    element.setAttribute("aria-label", `${kindLabel(item.kind)} ${displayLabel(item)}, ${statusLabel(item.status)}${endpoint}`);
    element.addEventListener("mouseenter", () => showTooltip(item, position, width));
    element.addEventListener("mouseleave", () => {
      if (document.activeElement !== element) hideTooltip();
    });
    element.addEventListener("focus", () => showTooltip(item, position, width));
    element.addEventListener("blur", hideTooltip);
    element.addEventListener("click", () => selectItem(item.id));
    element.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectItem(item.id);
      }
    });
  }

  function renderLaneLabels(story, layout, mode) {
    const target = $("storyline-lanes");
    target.replaceChildren();
    let actorNumber = 0;
    story.lanes.forEach((lane) => {
      const geometry = layout.laneGeometry.get(lane.id);
      const row = htmlNode("div", `storyline-lane-label lane-${lane.kind} status-${lane.status || "observed"}`);
      row.style.height = `${geometry?.height || 48}px`;
      if (lane.kind === "agent") {
        actorNumber += 1;
      }
      row.append(
        htmlNode("strong", "", laneLabel(lane, actorNumber)),
        htmlNode("span", "", lane.kind === "agent"
          ? t(
            mode === "window" ? "storyline.laneStarted" : "storyline.laneRecent",
            { time: formatTime(mode === "window" ? lane.started_at : lane.last_at) },
          )
          : t("storyline.laneItems", { count: lane.item_count || 0 })),
      );
      target.append(row);
    });
  }

  function drawGuides(svg, story, layout) {
    const layer = svgNode("g", { class: "trace-guides" });
    story.lanes.forEach((lane, index) => {
      const geometry = layout.laneGeometry.get(lane.id);
      if (!geometry) return;
      layer.append(svgNode("rect", {
        x: 0,
        y: geometry.y,
        width: layout.width,
        height: geometry.height,
        class: `lane-background lane-${lane.kind} ${index % 2 ? "lane-even" : "lane-odd"}`,
      }));
      layer.append(svgNode("line", {
        x1: 0,
        y1: geometry.y + geometry.height,
        x2: layout.width,
        y2: geometry.y + geometry.height,
      }));
      if (lane.kind === "canonical") {
        layer.append(svgNode("line", {
          x1: 12,
          y1: geometry.center,
          x2: layout.width - 14,
          y2: geometry.center,
          class: "canonical-rail",
        }));
      }
    });
    const tickInterval = layout.moments.length > 6 ? 3 : 2;
    layout.momentGeometry.forEach(({ moment, x }, index) => {
      if (index % tickInterval !== 0) return;
      const tick = svgNode("text", { x: x + 3, y: 10, class: "trace-time" });
      tick.textContent = formatTime(moment);
      layer.append(tick);
    });
    svg.append(layer);
  }

  function canonicalBranchPath(branch) {
    const span = Math.max(1, branch.x2 - branch.x1);
    const bend = Math.min(9, Math.max(5, span / 6));
    const branchStart = branch.x1 + bend * 1.7;
    const branchDepth = branch.y - branch.railY;
    const forkCurve = `M ${branch.x1} ${branch.railY} C ${branch.x1} ${branch.railY + branchDepth * 0.6} ${branchStart - bend * 0.55} ${branch.y} ${branchStart} ${branch.y}`;
    if (!branch.merged) {
      return `${forkCurve} H ${branch.x2}`;
    }
    const branchEnd = branch.x2 - bend * 1.7;
    return `${forkCurve} H ${branchEnd} C ${branch.x2 - bend} ${branch.y} ${branch.x2 - bend} ${branch.railY} ${branch.x2} ${branch.railY}`;
  }

  function drawCanonicalBranches(svg, layout) {
    const layer = svgNode("g", { class: "canonical-branch-graph" });
    layout.canonicalBranchGeometry.forEach((branch) => {
      const terminal = branch.merged
        ? svgNode("circle", {
            class: "canonical-rejoin-node",
            cx: branch.x2,
            cy: branch.railY,
            r: 3.8,
          })
        : branch.aborted
          ? abortGlyph("canonical-abort-node", branch.x2, branch.y)
          : svgNode("circle", {
              class: "canonical-branch-tip",
              cx: branch.x2,
              cy: branch.y,
              r: 3.2,
            });
      layer.append(
        svgNode("path", {
          class: `canonical-branch-path status-${branch.status || "active"}`,
          d: canonicalBranchPath(branch),
        }),
        svgNode("circle", {
          class: "canonical-fork-node",
          cx: branch.x1,
          cy: branch.railY,
          r: 3.6,
        }),
        terminal,
      );
    });
    svg.append(layer);
  }

  function drawSpans(svg, story, layout) {
    const layer = svgNode("g", { class: "trace-spans" });
    story.spans.forEach((span) => {
      const geometry = layout.spanGeometry.get(span.id);
      if (!geometry) return;
      const binding = span.run_binding || (span.run_id ? "native" : "unbound");
      const compound = geometry.compoundRole ? ` compound-${geometry.compoundRole}` : "";
      const group = svgNode("g", {
        class: `trace-span span-${span.kind} status-${span.status || "observed"} run-${binding}${compound}${geometry.branchContext ? " branch-context" : ""}`,
      });
      if (span.kind === "transaction") {
        group.append(svgNode("line", {
          class: "trace-branch-segment",
          x1: geometry.x1,
          y1: geometry.y,
          x2: geometry.x2,
          y2: geometry.y,
        }));
        if (span.status === "aborted" && span.ended_at) {
          group.append(abortGlyph("local-abort-node", geometry.x2, geometry.y));
        }
      } else if (["waiting", "diverted"].includes(span.kind)) {
        group.append(
          svgNode("line", {
            class: "trace-state-segment",
            x1: geometry.x1,
            y1: geometry.y,
            x2: geometry.x2,
            y2: geometry.y,
            "marker-end": span.kind === "waiting"
              ? "url(#waiting-arrow)"
              : "url(#diverted-arrow)",
          }),
          svgNode("path", {
            class: "trace-work-node",
            d: `M ${geometry.x1} ${geometry.y - 6} L ${geometry.x1 + 6} ${geometry.y} L ${geometry.x1} ${geometry.y + 6} L ${geometry.x1 - 6} ${geometry.y} Z`,
          }),
        );
      } else if (geometry.compoundRole === "claim") {
        group.append(svgNode("circle", {
          class: "trace-work-node trace-compound-badge",
          cx: geometry.x1,
          cy: geometry.y,
          r: 3.8,
        }));
      } else {
        group.append(svgNode("circle", {
          class: "trace-work-node",
          cx: geometry.x1,
          cy: geometry.y,
          r: geometry.compoundRole === "session" ? 7.4 : 6,
        }));
        if (span.kind === "session") {
          group.append(svgNode("path", {
            class: "trace-session-glyph",
            d: `M ${geometry.x1 - 1.5} ${geometry.y - 2.2} L ${geometry.x1 + 2.3} ${geometry.y} L ${geometry.x1 - 1.5} ${geometry.y + 2.2} Z`,
          }));
        }
      }
      group.append(svgNode("circle", {
        class: "trace-span-hit",
        cx: geometry.x1,
        cy: geometry.y,
        r: geometry.compoundRole === "claim" ? 7 : 14,
      }));
      makeInteractive(group, span, { x: geometry.x1, y: geometry.y }, layout.width);
      layer.append(group);
    });
    svg.append(layer);
  }

  function drawProgress(svg, story, layout) {
    const layer = svgNode("g", {
      class: "trace-progress",
      "aria-label": t("storyline.progressAria"),
    });
    layout.runSpineGeometry.forEach((spine) => {
      const path = svgNode("line", {
        class: `run-spine-line status-${spine.status || "observed"}`,
        x1: spine.x1,
        y1: spine.y,
        x2: spine.x2,
        y2: spine.y,
        "marker-end": spine.final ? "url(#progress-arrow)" : undefined,
      });
      layer.append(path);
    });
    layout.attachmentGeometry.forEach((attachment) => {
      const path = svgNode("line", {
        class: `run-attachment binding-${attachment.binding || "native"}`,
        x1: attachment.source.x,
        y1: attachment.source.y,
        x2: attachment.target.x,
        y2: attachment.target.y,
      });
      layer.append(path);
    });
    svg.append(layer);
  }

  function relationPath(source, target) {
    const axisAligned = Math.abs(target.x - source.x) < 0.5 || Math.abs(target.y - source.y) < 0.5;
    if (axisAligned) return `M ${source.x} ${source.y} L ${target.x} ${target.y}`;
    const middleX = source.x + (target.x - source.x) / 2;
    return `M ${source.x} ${source.y} H ${middleX} V ${target.y} H ${target.x}`;
  }

  function communicationApproach(source, target, distance = 7) {
    if (Math.abs(target.x - source.x) < 0.5) {
      return { ...target, y: target.y - Math.sign(target.y - source.y) * distance };
    }
    return { ...target, x: target.x - Math.sign(target.x - source.x) * distance };
  }

  function drawCommunicationEndpoints(group, geometry) {
    const endpoint = geometry.targetEndpoint;
    if (!endpoint || !geometry.source || !geometry.target) return;
    group.append(svgNode("circle", {
      class: "communication-source-node",
      cx: geometry.source.x,
      cy: geometry.source.y,
      r: 2.5,
    }));
    group.append(svgNode("circle", {
      class: `communication-endpoint endpoint-${endpoint.state}`,
      cx: geometry.target.x,
      cy: geometry.target.y,
      r: endpoint.state === "acknowledged" ? 5 : 4.5,
    }));
    if (endpoint.state === "run-context") {
      group.append(svgNode("circle", {
        class: "communication-endpoint-core",
        cx: geometry.target.x,
        cy: geometry.target.y,
        r: 1.8,
      }));
    } else if (endpoint.state === "acknowledged") {
      group.append(svgNode("path", {
        class: "communication-endpoint-check",
        d: `M ${geometry.target.x - 2.3} ${geometry.target.y} l 1.6 1.7 l 3.2 -3.4`,
      }));
    }
  }

  function branchTransitionPath(source, target, kind) {
    const deltaX = target.x - source.x;
    const deltaY = target.y - source.y;
    if (kind === "fork") {
      return `M ${source.x} ${source.y} C ${source.x} ${source.y + deltaY * 0.6} ${target.x - deltaX * 0.35} ${target.y} ${target.x} ${target.y}`;
    }
    return `M ${source.x} ${source.y} C ${source.x + deltaX * 0.7} ${source.y} ${target.x} ${target.y - deltaY * 0.35} ${target.x} ${target.y}`;
  }

  function relationArrow(kind) {
    if (kind === "fork") return "url(#branch-arrow)";
    if (kind === "rejoin") return "url(#publish-arrow)";
    if (["waits-for", "blocked"].includes(kind)) return "url(#waiting-arrow)";
    if (kind === "diverts-to") return "url(#diverted-arrow)";
    if (["handoff", "reassigned", "message"].includes(kind)) return "url(#communication-arrow)";
    return kind === "contention" ? undefined : "url(#trace-arrow)";
  }

  function drawRelations(svg, story, layout) {
    const layer = svgNode("g", { class: "trace-relations" });
    story.relations.forEach((relation) => {
      const geometry = layout.relationGeometry.get(relation.id);
      if (!geometry) return;
      const group = svgNode("g", {
        class: `trace-relation relation-${relation.kind} status-${relation.status || "observed"}${geometry.branchContext ? " branch-context" : ""}`,
      });
      let position = { x: geometry.x, y: 20 };
      if (relation.kind === "contention" && geometry.owners.length) {
        const ys = geometry.owners.map((point) => point.y);
        const top = Math.min(...ys);
        const bottom = Math.max(...ys);
        position = { x: geometry.x, y: (top + bottom) / 2 };
        group.append(
          svgNode("line", { class: "relation-line", x1: geometry.x, y1: top, x2: geometry.x, y2: bottom }),
          svgNode("path", { class: "relation-symbol", d: `M ${geometry.x} ${top - 5} l 5 5 l -5 5 l -5 -5 z` }),
        );
      } else if (geometry.source && geometry.target) {
        const localBranch = geometry.branchContext
          && ["fork", "rejoin", "return"].includes(relation.kind);
        position = {
          x: (geometry.source.x + geometry.target.x) / 2,
          y: (geometry.source.y + geometry.target.y) / 2,
        };
        const pathTarget = geometry.targetEndpoint
          ? communicationApproach(geometry.source, geometry.target)
          : geometry.target;
        group.append(svgNode("path", {
          class: "relation-line",
          d: localBranch
            ? branchTransitionPath(geometry.source, geometry.target, relation.kind)
            : relationPath(geometry.source, pathTarget),
          "marker-end": localBranch ? undefined : relationArrow(relation.kind),
        }));
        drawCommunicationEndpoints(group, geometry);
        if (localBranch) {
          const anchor = relation.kind === "fork" ? geometry.source : geometry.target;
          group.append(svgNode("circle", {
            class: relation.kind === "fork"
              ? "local-fork-node"
              : relation.kind === "return"
                ? "local-return-node"
                : "local-rejoin-node",
            cx: anchor.x,
            cy: anchor.y,
            r: relation.kind === "fork" ? 4.2 : 4.5,
          }));
        }
      }
      group.append(svgNode("circle", { class: "relation-hit", cx: position.x, cy: position.y, r: 10 }));
      makeInteractive(group, relation, position, layout.width);
      layer.append(group);
    });
    svg.append(layer);
  }

  function drawMarkers(svg, story, layout) {
    const layer = svgNode("g", { class: "trace-markers" });
    story.markers.forEach((marker) => {
      const position = layout.markerGeometry.get(marker.id);
      if (!position) return;
      const group = svgNode("g", {
        class: `trace-marker marker-${marker.kind} status-${marker.status || "observed"}`,
        transform: `translate(${position.x} ${position.y})`,
      });
      if (["contention", "decision"].includes(marker.kind)) {
        group.append(svgNode("path", { class: "trace-marker-shape", d: "M 0 -6 L 6 0 L 0 6 L -6 0 Z" }));
      } else {
        group.append(svgNode("circle", { class: "trace-marker-shape", r: marker.kind === "publish" ? 6 : 5 }));
      }
      group.append(svgNode("circle", { class: "trace-marker-hit", r: 14 }));
      makeInteractive(group, marker, position, layout.width);
      layer.append(group);
    });
    svg.append(layer);
  }

  function render(story = {}, context = {}) {
    const workspaceChanged = renderedWorkspace !== story.workspace_id;
    renderedWorkspace = story.workspace_id || null;
    currentStory = {
      workspace_id: story.workspace_id || null,
      lanes: orderLanes(story.lanes || [], context.mode),
      spans: story.spans || [],
      markers: story.markers || [],
      relations: story.relations || [],
      summary: story.summary || {},
      focus: story.focus || {},
    };
    const svg = $("collaboration-storyline");
    const empty = $("storyline-empty");
    hideTooltip();
    svg.replaceChildren();
    const summary = currentStory.summary;
    const earliest = earliestRecordedAt(currentStory);
    const latest = latestRecordedAt(currentStory);
    const countKey = context.mode === "window"
      ? "storyline.countWindow"
      : "storyline.countLatest";
    $("graph-count").textContent = t(countKey, {
      actors: summary.actors || 0,
      nodes: (summary.work_spans || 0) + (summary.markers || 0),
      relations: summary.relations || 0,
    });
    $("storyline-focus-note").textContent = t("storyline.focusNote", {
      owners: summary.owner_labels_in_window || 0,
      runs: summary.joined_runs || 0,
      peak: summary.max_concurrent_runs || 0,
      visible: summary.visible_moments || 0,
      total: summary.total_moments || 0,
    });
    $("storyline-range").textContent = earliest && latest
      ? t("storyline.range", { start: formatTime(earliest, true), end: formatTime(latest, true) })
      : t("storyline.rangeEmpty");
    const attention = Number(summary.attention_moments || 0);
    $("storyline-attention").hidden = attention === 0;
    $("storyline-attention").textContent = attention
      ? t("storyline.attention", { count: attention })
      : "";
    const pagination = summary.pagination || {};
    const page = Number(pagination.page || 1);
    const pages = Number(pagination.pages || 1);
    $("storyline-page-label").textContent = t("storyline.page.label", { page, pages });
    $("storyline-page-older").disabled = !pagination.has_older;
    $("storyline-page-newer").disabled = !pagination.has_newer;
    $("storyline-mode-latest").classList.toggle("active", context.mode !== "window");
    $("storyline-mode-window").classList.toggle("active", context.mode === "window");
    $("storyline-mode-latest").setAttribute("aria-pressed", context.mode === "window" ? "false" : "true");
    $("storyline-mode-window").setAttribute("aria-pressed", context.mode === "window" ? "true" : "false");
    if (!currentStory.spans.length && !currentStory.markers.length && !currentStory.relations.length) {
      selectedId = null;
      renderEmptyInspector();
      $("storyline-lanes").replaceChildren();
      svg.hidden = true;
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    svg.hidden = false;
    const layout = window.DevMeshStorylineLayout.compute(currentStory);
    renderLaneLabels(currentStory, layout, context.mode);
    svg.setAttribute("viewBox", `0 0 ${layout.width} ${layout.height}`);
    svg.setAttribute("width", layout.width);
    svg.setAttribute("height", layout.height);
    const definitions = svgNode("defs");
    definitions.append(
      arrowMarker("trace-arrow", "trace-arrow"),
      arrowMarker("progress-arrow", "progress-arrow"),
      arrowMarker("branch-arrow", "branch-arrow"),
      arrowMarker("publish-arrow", "publish-arrow"),
      arrowMarker("communication-arrow", "communication-arrow"),
      arrowMarker("waiting-arrow", "waiting-arrow"),
      arrowMarker("diverted-arrow", "diverted-arrow"),
    );
    svg.append(definitions);
    drawGuides(svg, currentStory, layout);
    drawCanonicalBranches(svg, layout);
    drawProgress(svg, currentStory, layout);
    drawSpans(svg, currentStory, layout);
    drawRelations(svg, currentStory, layout);
    drawMarkers(svg, currentStory, layout);

    if (workspaceChanged) {
      const focus = itemById(currentStory.focus.relation_id)
        || currentStory.relations.find((item) => ["contention", "waits-for"].includes(item.kind) && ["stalled", "waiting"].includes(item.status))
        || currentStory.spans.find((item) => item.status === "active")
        || currentStory.markers.at(-1)
        || currentStory.spans.at(-1);
      const geometry = focus
        ? layout.relationGeometry.get(focus.id) || layout.markerGeometry.get(focus.id) || layout.spanGeometry.get(focus.id)
        : null;
      const focusX = geometry?.x ?? geometry?.x1;
      const focusY = geometry?.owners?.[0]?.y
        ?? geometry?.source?.y
        ?? geometry?.target?.y
        ?? geometry?.y;
      if (focusX !== undefined || focusY !== undefined) {
        window.requestAnimationFrame(() => {
          const scroll = $("storyline-scroll");
          if (focusX !== undefined) {
            scroll.scrollLeft = Math.max(0, focusX - scroll.clientWidth * 0.42);
          }
          if (focusY !== undefined) {
            scroll.scrollTop = Math.max(0, focusY - scroll.clientHeight * 0.36);
            $("storyline-lanes").scrollTop = scroll.scrollTop;
          }
        });
      }
    }
    if (selectedId && itemById(selectedId)) selectItem(selectedId);
    else {
      selectedId = null;
      renderEmptyInspector();
    }
  }

  $("storyline-scroll").addEventListener("scroll", (event) => {
    $("storyline-lanes").scrollTop = event.currentTarget.scrollTop;
  });

  window.DevMeshStorylineView = { render };
})();
