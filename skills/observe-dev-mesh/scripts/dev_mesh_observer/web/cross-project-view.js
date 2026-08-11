"use strict";

(() => {
  const preferences = window.DevMeshPreferences;
  const $ = (id) => document.getElementById(id);
  const t = (key, variables) => preferences.t(key, variables);
  const LEFT = 26;
  const RIGHT = 30;
  const LANE_HEIGHT = 58;

  function htmlNode(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function svgNode(tag, attributes = {}) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([name, value]) => {
      if (value !== undefined && value !== null) element.setAttribute(name, String(value));
    });
    return element;
  }

  function shortPath(value) {
    if (!value) return t("workspace.unknown");
    const parts = String(value).split("/").filter(Boolean);
    return parts.slice(-2).join("/") || String(value);
  }

  function formatTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value || "");
    return new Intl.DateTimeFormat(preferences.locale, {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }

  function formatDuration(seconds) {
    const amount = Math.max(0, Number(seconds || 0));
    if (amount < 60) return t("crossProject.seconds", { count: Math.round(amount) });
    if (amount < 3600) return t("crossProject.minutes", { count: Math.round(amount / 60) });
    return t("crossProject.hours", { count: (amount / 3600).toFixed(1) });
  }

  function computeLayout(data) {
    const projects = Array.isArray(data.projects) ? data.projects : [];
    const episodes = Array.isArray(data.episodes) ? data.episodes : [];
    const relations = Array.isArray(data.relations) ? data.relations : [];
    const timestamps = episodes.flatMap((episode) => [
      new Date(episode.started_at).getTime(),
      new Date(episode.last_at).getTime(),
    ]).filter(Number.isFinite);
    let minimum = timestamps.length ? Math.min(...timestamps) : Date.now();
    let maximum = timestamps.length ? Math.max(...timestamps) : minimum + 1;
    if (minimum === maximum) {
      minimum -= 30_000;
      maximum += 30_000;
    }
    const width = Math.max(760, $("cross-project-scroll")?.clientWidth || 0);
    const usable = width - LEFT - RIGHT;
    const xAt = (value) => {
      const timestamp = new Date(value).getTime();
      if (!Number.isFinite(timestamp)) return LEFT;
      return LEFT + ((timestamp - minimum) / (maximum - minimum)) * usable;
    };
    const laneByWorkspace = new Map();
    projects.forEach((project, index) => {
      laneByWorkspace.set(project.workspace_id, {
        y: index * LANE_HEIGHT,
        center: index * LANE_HEIGHT + LANE_HEIGHT / 2,
      });
    });
    const episodeGeometry = new Map();
    episodes.forEach((episode) => {
      const lane = laneByWorkspace.get(episode.workspace_id);
      if (!lane) return;
      const x1 = xAt(episode.started_at);
      const observedX2 = xAt(episode.last_at);
      episodeGeometry.set(episode.id, {
        x1,
        x2: Math.max(x1 + 10, observedX2),
        observedX2,
        y: lane.center,
      });
    });
    const relationGeometry = new Map();
    relations.forEach((relation) => {
      const ids = Array.isArray(relation.episode_ids) ? relation.episode_ids : [];
      const source = episodeGeometry.get(ids[0]);
      const target = episodeGeometry.get(ids[1]);
      if (!source || !target) return;
      const overlapX1 = Math.max(source.x1, target.x1);
      const overlapX2 = Math.min(source.observedX2, target.observedX2);
      relationGeometry.set(relation.id, {
        source: {
          x: relation.temporal_relation === "overlap" ? target.x1 : source.x2,
          y: source.y,
        },
        target: { x: target.x1, y: target.y },
        overlap: relation.temporal_relation === "overlap" && overlapX2 > overlapX1
          ? {
            x1: overlapX1,
            x2: overlapX2,
            y1: Math.min(source.y, target.y) - 8,
            y2: Math.max(source.y, target.y) + 8,
          }
          : null,
      });
    });
    return {
      width,
      height: Math.max(150, projects.length * LANE_HEIGHT),
      minimum,
      maximum,
      projects,
      episodes,
      relations,
      laneByWorkspace,
      episodeGeometry,
      relationGeometry,
      xAt,
    };
  }

  function relationPath(source, target) {
    if (Math.abs(source.x - target.x) < 1) {
      return `M ${source.x} ${source.y} V ${target.y}`;
    }
    const middleX = source.x + (target.x - source.x) / 2;
    return `M ${source.x} ${source.y} H ${middleX} V ${target.y} H ${target.x}`;
  }

  function showEpisodeTooltip(episode, geometry) {
    const tooltip = $("cross-project-tooltip");
    const runs = Array.isArray(episode.run_ids) ? episode.run_ids : [];
    const tasks = Array.isArray(episode.tasks) ? episode.tasks : [];
    tooltip.replaceChildren(
      htmlNode("span", "cross-tooltip-kicker", t("crossProject.inferredIdentity")),
      htmlNode("strong", "", episode.owner),
      htmlNode("span", "", shortPath(episode.workspace_root)),
      htmlNode("time", "", `${formatTime(episode.started_at)} → ${formatTime(episode.last_at)}`),
      htmlNode("small", "", t("crossProject.tooltipEvidence")),
      ...(runs.length ? [htmlNode("small", "", t("crossProject.tooltipRuns", { runs: runs.join(", ") }))] : []),
      ...(tasks.length ? [htmlNode("small", "", tasks.join(" · "))] : []),
    );
    const scroll = $("cross-project-scroll");
    const left = Math.min(
      Math.max(8, geometry.x1 - scroll.scrollLeft + 12),
      Math.max(8, scroll.clientWidth - 274),
    );
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${Math.max(8, geometry.y - scroll.scrollTop + 14)}px`;
    tooltip.hidden = false;
  }

  function showRelationTooltip(relation, geometry) {
    const tooltip = $("cross-project-tooltip");
    const temporal = relation.temporal_relation === "overlap"
      ? t("crossProject.overlap", { duration: formatDuration(relation.overlap_seconds) })
      : t("crossProject.sequence", { duration: formatDuration(relation.gap_seconds) });
    tooltip.replaceChildren(
      htmlNode("span", "cross-tooltip-kicker", t("crossProject.inferredIdentity")),
      htmlNode("strong", "", relation.owner),
      htmlNode("span", "", temporal),
      htmlNode("small", "", t("crossProject.noCausality")),
    );
    const scroll = $("cross-project-scroll");
    const centerX = geometry.overlap
      ? (geometry.overlap.x1 + geometry.overlap.x2) / 2
      : (geometry.source.x + geometry.target.x) / 2;
    const centerY = geometry.overlap
      ? (geometry.overlap.y1 + geometry.overlap.y2) / 2
      : (geometry.source.y + geometry.target.y) / 2;
    tooltip.style.left = `${Math.max(8, centerX - scroll.scrollLeft + 10)}px`;
    tooltip.style.top = `${Math.max(8, centerY - scroll.scrollTop + 10)}px`;
    tooltip.hidden = false;
  }

  function hideTooltip() {
    $("cross-project-tooltip").hidden = true;
  }

  function renderLanes(layout, onSelect) {
    const lanes = $("cross-project-lanes");
    lanes.replaceChildren();
    layout.projects.forEach((project) => {
      const button = htmlNode("button", "cross-project-lane");
      button.type = "button";
      button.style.height = `${LANE_HEIGHT}px`;
      button.append(
        htmlNode("strong", "", shortPath(project.workspace_root)),
        htmlNode("span", "", t("crossProject.laneEpisodes", { count: project.episode_count })),
      );
      button.addEventListener("click", () => onSelect(project.workspace_id));
      lanes.append(button);
    });
  }

  function render(data = {}, onSelect = () => {}) {
    const svg = $("cross-project-graph");
    const empty = $("cross-project-empty");
    const layout = computeLayout(data);
    hideTooltip();
    svg.replaceChildren();
    renderLanes(layout, onSelect);
    svg.setAttribute("viewBox", `0 0 ${layout.width} ${layout.height}`);
    svg.setAttribute("width", layout.width);
    svg.setAttribute("height", layout.height);

    if (!layout.episodes.length) {
      svg.hidden = true;
      empty.hidden = false;
      return;
    }
    svg.hidden = false;
    empty.hidden = true;

    const guides = svgNode("g", { class: "cross-project-guides" });
    layout.projects.forEach((project, index) => {
      const lane = layout.laneByWorkspace.get(project.workspace_id);
      guides.append(
        svgNode("rect", {
          x: 0,
          y: lane.y,
          width: layout.width,
          height: LANE_HEIGHT,
          class: index % 2 ? "lane-even" : "lane-odd",
        }),
        svgNode("line", {
          x1: 0,
          y1: lane.y + LANE_HEIGHT,
          x2: layout.width,
          y2: lane.y + LANE_HEIGHT,
        }),
      );
    });
    [layout.minimum, (layout.minimum + layout.maximum) / 2, layout.maximum].forEach((moment) => {
      const x = layout.xAt(new Date(moment).toISOString());
      const label = svgNode("text", { x: x + 3, y: 11, class: "cross-project-time" });
      label.textContent = formatTime(new Date(moment).toISOString());
      guides.append(label);
    });
    svg.append(guides);

    const overlapAreas = svgNode("g", { class: "cross-project-overlaps" });
    layout.relations.forEach((relation) => {
      const geometry = layout.relationGeometry.get(relation.id);
      if (!geometry?.overlap) return;
      const overlap = geometry.overlap;
      const group = svgNode("g", {
        class: `cross-project-overlap confidence-${relation.confidence || "moderate"}`,
        tabindex: "0",
        role: "button",
        "aria-label": t("crossProject.overlapArea", {
          owner: relation.owner,
          duration: formatDuration(relation.overlap_seconds),
        }),
      });
      group.append(
        svgNode("rect", {
          class: "identity-overlap-area",
          x: overlap.x1,
          y: overlap.y1,
          width: overlap.x2 - overlap.x1,
          height: overlap.y2 - overlap.y1,
          rx: 4,
        }),
        svgNode("rect", {
          class: "identity-overlap-hit",
          x: overlap.x1,
          y: overlap.y1,
          width: overlap.x2 - overlap.x1,
          height: overlap.y2 - overlap.y1,
          rx: 4,
        }),
      );
      group.addEventListener("mouseenter", () => showRelationTooltip(relation, geometry));
      group.addEventListener("focus", () => showRelationTooltip(relation, geometry));
      group.addEventListener("mouseleave", hideTooltip);
      group.addEventListener("blur", hideTooltip);
      overlapAreas.append(group);
    });
    svg.append(overlapAreas);

    const links = svgNode("g", { class: "cross-project-links" });
    layout.relations.forEach((relation) => {
      if (relation.temporal_relation === "overlap") return;
      const geometry = layout.relationGeometry.get(relation.id);
      if (!geometry) return;
      const group = svgNode("g", {
        class: `cross-project-link confidence-${relation.confidence || "moderate"}`,
        tabindex: "0",
      });
      const pathData = relationPath(geometry.source, geometry.target);
      group.append(
        svgNode("path", { class: "identity-line", d: pathData }),
        svgNode("path", { class: "identity-hit", d: pathData }),
      );
      group.addEventListener("mouseenter", () => showRelationTooltip(relation, geometry));
      group.addEventListener("focus", () => showRelationTooltip(relation, geometry));
      group.addEventListener("mouseleave", hideTooltip);
      group.addEventListener("blur", hideTooltip);
      links.append(group);
    });
    svg.append(links);

    const activity = svgNode("g", { class: "cross-project-activity" });
    layout.episodes.forEach((episode) => {
      const geometry = layout.episodeGeometry.get(episode.id);
      if (!geometry) return;
      const group = svgNode("g", {
        class: "cross-project-episode",
        tabindex: "0",
        role: "button",
      });
      group.append(
        svgNode("line", {
          class: "episode-span",
          x1: geometry.x1,
          y1: geometry.y,
          x2: geometry.x2,
          y2: geometry.y,
        }),
        svgNode("circle", {
          class: "episode-node",
          cx: geometry.x1,
          cy: geometry.y,
          r: 5.5,
        }),
        svgNode("circle", {
          class: "episode-end",
          cx: geometry.x2,
          cy: geometry.y,
          r: 2.5,
        }),
      );
      group.addEventListener("mouseenter", () => showEpisodeTooltip(episode, geometry));
      group.addEventListener("focus", () => showEpisodeTooltip(episode, geometry));
      group.addEventListener("mouseleave", hideTooltip);
      group.addEventListener("blur", hideTooltip);
      group.addEventListener("click", () => onSelect(episode.workspace_id));
      group.addEventListener("keydown", (event) => {
        if (["Enter", " "].includes(event.key)) {
          event.preventDefault();
          onSelect(episode.workspace_id);
        }
      });
      activity.append(group);
    });
    svg.append(activity);
  }

  $("cross-project-scroll").addEventListener("scroll", (event) => {
    $("cross-project-lanes").scrollTop = event.currentTarget.scrollTop;
    hideTooltip();
  });
  window.DevMeshCrossProjectView = { render };
})();
