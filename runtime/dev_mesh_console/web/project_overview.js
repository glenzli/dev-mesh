const SVG_NS = "http://www.w3.org/2000/svg";

function activeTotal(active) {
  return Object.values(active ?? {}).reduce((total, value) => total + Number(value), 0);
}

export function projectHasSignal(project) {
  return Number(project.event_count) > 0
    || activeTotal(project.active) > 0
    || Number(project.diagnostic_count) > 0
    || Boolean(project.collection_error)
    || Boolean(project.not_observed_since);
}

export function selectableProjects(projects, current = "") {
  return projects.filter(
    (project) => projectHasSignal(project) || project.workspace_id === current,
  );
}

function svgElement(name, attributes = {}) {
  const node = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function connectedComponents(nodes, edges) {
  const adjacency = new Map(nodes.map((node) => [node.workspace_id, new Set()]));
  edges.forEach((edge) => {
    adjacency.get(edge.source_workspace_id)?.add(edge.target_workspace_id);
    adjacency.get(edge.target_workspace_id)?.add(edge.source_workspace_id);
  });
  const unseen = new Set(adjacency.keys());
  const result = [];
  while (unseen.size) {
    const first = unseen.values().next().value;
    const stack = [first];
    const component = [];
    unseen.delete(first);
    while (stack.length) {
      const identifier = stack.pop();
      component.push(identifier);
      adjacency.get(identifier)?.forEach((peer) => {
        if (!unseen.has(peer)) return;
        unseen.delete(peer);
        stack.push(peer);
      });
    }
    result.push(component);
  }
  return result;
}

export function projectGraphLayout(
  projection,
  { left = 24, top = 48, nodeWidth = 158, nodeHeight = 52, nodeGap = 104, rowGap = 38 } = {},
) {
  const rawNodes = projection?.nodes ?? [];
  const rawEdges = projection?.edges ?? [];
  const nodeById = new Map(rawNodes.map((node) => [node.workspace_id, node]));
  const edges = rawEdges.filter(
    (edge) => nodeById.has(edge.source_workspace_id)
      && nodeById.has(edge.target_workspace_id)
      && edge.source_workspace_id !== edge.target_workspace_id,
  );
  const relatedIds = new Set(edges.flatMap(
    (edge) => [edge.source_workspace_id, edge.target_workspace_id],
  ));
  const nodes = rawNodes.filter((node) => relatedIds.has(node.workspace_id));
  const degree = new Map(nodes.map((node) => [node.workspace_id, 0]));
  edges.forEach((edge) => {
    degree.set(edge.source_workspace_id, (degree.get(edge.source_workspace_id) ?? 0) + 1);
    degree.set(edge.target_workspace_id, (degree.get(edge.target_workspace_id) ?? 0) + 1);
  });
  const components = connectedComponents(nodes, edges)
    .map((identifiers) => identifiers.sort((leftId, rightId) => (
      (degree.get(rightId) ?? 0) - (degree.get(leftId) ?? 0)
      || String(nodeById.get(leftId)?.name).localeCompare(String(nodeById.get(rightId)?.name))
    )))
    .sort((leftIds, rightIds) => rightIds.length - leftIds.length);

  const positionedNodes = [];
  const positions = new Map();
  let y = top;
  let maximumWidth = 620;
  components.forEach((identifiers) => {
    identifiers.forEach((identifier, index) => {
      const x = left + index * (nodeWidth + nodeGap);
      const position = { x, y, width: nodeWidth, height: nodeHeight };
      positions.set(identifier, position);
      positionedNodes.push({ ...nodeById.get(identifier), ...position });
      maximumWidth = Math.max(maximumWidth, x + nodeWidth + left);
    });
    y += nodeHeight + rowGap;
  });

  const positionedEdges = edges.map((edge) => {
    const source = positions.get(edge.source_workspace_id);
    const target = positions.get(edge.target_workspace_id);
    const protocol = Number(edge.collaboration_count) > 0;
    const direct = protocol;
    const direction = direct && edge.directions?.length
      ? [...edge.directions].sort((leftValue, rightValue) => rightValue.count - leftValue.count)[0]
      : null;
    const fromId = direction?.source_workspace_id ?? edge.source_workspace_id;
    const toId = direction?.target_workspace_id ?? edge.target_workspace_id;
    const from = positions.get(fromId) ?? source;
    const to = positions.get(toId) ?? target;
    const leftToRight = from.x <= to.x;
    const startX = leftToRight ? from.x + from.width : from.x;
    const endX = leftToRight ? to.x : to.x + to.width;
    const startY = from.y + from.height / 2;
    const endY = to.y + to.height / 2;
    const midpointX = (startX + endX) / 2;
    const sameRow = startY === endY;
    const arc = Math.min(34, 14 + Math.abs(endX - startX) / 18);
    const path = sameRow
      ? `M ${startX} ${startY} Q ${midpointX} ${startY - arc} ${endX} ${endY}`
      : `M ${startX} ${startY} C ${midpointX} ${startY}, ${midpointX} ${endY}, ${endX} ${endY}`;
    return {
      ...edge,
      direct,
      protocol,
      path,
      labelX: midpointX,
      labelY: sameRow ? startY - arc - 5 : (startY + endY) / 2 - 7,
    };
  });
  return {
    nodes: positionedNodes,
    edges: positionedEdges,
    width: maximumWidth,
    height: Math.max(142, y - rowGap + 30),
  };
}

function emptyState(translate) {
  const node = document.createElement("div");
  node.className = "empty-state compact-empty";
  const title = document.createElement("strong");
  title.textContent = translate("projectOverview.emptyTitle");
  const body = document.createElement("span");
  body.textContent = translate("projectOverview.emptyBody");
  node.append(title, body);
  return node;
}

function relationLabel(edge, translate, formatNumber) {
  const values = [];
  if (Number(edge.collaboration_count) > 0) {
    values.push(`${translate("projectOverview.crossTask")} ${formatNumber(edge.collaboration_count)}`);
  }
  if (Number(edge.open_collaboration_count) > 0) {
    values.push(`${translate("projectOverview.open")} ${formatNumber(edge.open_collaboration_count)}`);
  }
  if (Number(edge.same_run_hint_count) > 0) {
    values.push(`${translate("projectOverview.sameTaskHint")} ${formatNumber(edge.same_run_hint_count)}`);
  }
  return values.join(" · ");
}

export function renderProjectOverview(
  container,
  projection,
  { current = "", translate, formatNumber, onSelect },
) {
  const layout = projectGraphLayout(projection);
  if (!layout.edges.length) {
    container.replaceChildren(emptyState(translate));
    return { projectCount: 0, relationCount: 0 };
  }

  const legend = document.createElement("div");
  legend.className = "project-graph-legend";
  const protocolLegend = document.createElement("span");
  protocolLegend.className = "protocol";
  protocolLegend.textContent = translate("projectOverview.protocolRelation");
  const hintLegend = document.createElement("span");
  hintLegend.className = "hint";
  hintLegend.textContent = translate("projectOverview.sameTaskHintRelation");
  legend.append(protocolLegend, hintLegend);

  const scroll = document.createElement("div");
  scroll.className = "project-graph-scroll";
  const svg = svgElement("svg", {
    viewBox: `0 0 ${layout.width} ${layout.height}`,
    width: layout.width,
    height: layout.height,
    preserveAspectRatio: "xMinYMin meet",
    role: "img",
    "aria-label": translate("projectOverview.graphLabel"),
  });
  svg.classList.add("project-graph");
  const defs = svgElement("defs");
  const protocolMarker = svgElement("marker", {
    id: "project-arrow-protocol",
    viewBox: "0 0 10 10",
    refX: 9,
    refY: 5,
    markerWidth: 5,
    markerHeight: 5,
    orient: "auto-start-reverse",
  });
  protocolMarker.classList.add("protocol");
  protocolMarker.append(svgElement("path", { d: "M 2 2 L 8 5 L 2 8" }));
  defs.append(protocolMarker);
  svg.append(defs);

  layout.edges.forEach((edge) => {
    const group = svgElement("g");
    group.classList.add(
      "project-relation",
      edge.protocol ? "protocol" : "same-run-hint",
    );
    const path = svgElement("path", { d: edge.path });
    if (edge.protocol) path.setAttribute("marker-end", "url(#project-arrow-protocol)");
    const label = svgElement("text", {
      x: edge.labelX,
      y: edge.labelY,
      "text-anchor": "middle",
    });
    label.textContent = relationLabel(edge, translate, formatNumber);
    const title = svgElement("title");
    title.textContent = edge.samples?.map(
      (sample) => [sample.owner, sample.run_id, sample.collaboration_id].filter(Boolean).join(" · "),
    ).join("\n") || label.textContent;
    group.append(title, path, label);
    svg.append(group);
  });

  layout.nodes.forEach((node) => {
    const group = svgElement("g", {
      role: "button",
      tabindex: "0",
      "aria-label": node.name,
    });
    group.classList.add("project-graph-node");
    if (current === node.workspace_id) group.classList.add("is-selected");
    const card = svgElement("rect", {
      x: node.x,
      y: node.y,
      width: node.width,
      height: node.height,
      rx: 10,
    });
    const name = svgElement("text", { x: node.x + 13, y: node.y + 23 });
    name.classList.add("project-node-name");
    name.textContent = node.name.length > 22 ? `${node.name.slice(0, 21)}…` : node.name;
    const detail = svgElement("text", { x: node.x + 13, y: node.y + 40 });
    detail.classList.add("project-node-detail");
    detail.textContent = translate("projectOverview.relatedProject");
    group.append(card, name, detail);
    const activate = () => onSelect(node.workspace_id);
    group.addEventListener("click", activate);
    group.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      activate();
    });
    svg.append(group);
  });
  scroll.append(svg);
  container.replaceChildren(legend, scroll);
  return { projectCount: layout.nodes.length, relationCount: layout.edges.length };
}
