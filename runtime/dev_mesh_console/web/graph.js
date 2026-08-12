import { eventLabel, language, t } from "/i18n.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const semantics = {
  branch: new Set([
    "transaction-created",
    "transaction-prepared",
    "transaction-validated",
    "transaction-refreshed",
    "transaction-published",
    "transaction-aborted",
  ]),
  handoff: new Set([
    "message-sent",
    "message-acknowledged",
    "handoff-offered",
    "handoff-accepted",
    "handoff-rejected",
    "handoff-withdrawn",
  ]),
  waiting: new Set(["work-suspended", "work-resumed", "claim-paused", "claim-resumed"]),
  conflict: new Set([
    "claim-requested",
    "contention-opened",
    "contention-decision-proposed",
    "contention-decision-responded",
    "contention-completed",
    "contention-cancelled",
  ]),
};

function element(name, attributes = {}) {
  const node = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function semantic(eventName) {
  for (const [name, values] of Object.entries(semantics)) {
    if (values.has(eventName)) return name;
  }
  return "normal";
}

function laneKey(event) {
  if (event.owner || event.run_id) return `${event.owner ?? "unknown"}\u0000${event.run_id ?? "unknown"}`;
  const source = event.details?.source_owner;
  return `${source ?? "unattributed"}\u0000unattributed`;
}

function short(value, length = 24) {
  if (!value) return "—";
  return value.length > length ? `${value.slice(0, length - 1)}…` : value;
}

function timestamp(value) {
  const date = new Date(value);
  return new Intl.DateTimeFormat(language() === "zh" ? "zh-CN" : "en", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function setTooltip(tooltip, event, projectName, point) {
  tooltip.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = eventLabel(event.event);
  const meta = document.createElement("span");
  meta.textContent = `${timestamp(event.at)} · ${projectName}`;
  const identity = document.createElement("span");
  identity.textContent = [event.owner, event.scope, event.details?.status].filter(Boolean).join(" · ") || event.event;
  tooltip.append(title, meta, identity);
  tooltip.hidden = false;
  tooltip.style.left = `${Math.min(point.x + 16, tooltip.parentElement.clientWidth - 250)}px`;
  tooltip.style.top = `${point.y + 12}px`;
}

function marker(defs, name, colorClass) {
  const value = element("marker", {
    id: `arrow-${name}`,
    viewBox: "0 0 10 10",
    refX: 9,
    refY: 5,
    markerWidth: 6,
    markerHeight: 6,
    orient: "auto-start-reverse",
  });
  value.classList.add(`marker-${colorClass}`);
  value.append(element("path", { d: "M 0 0 L 10 5 L 0 10 z" }));
  defs.append(value);
}

export function renderFlow(svg, tooltip, dashboard, projectNames) {
  svg.replaceChildren();
  tooltip.hidden = true;
  const events = dashboard.events;
  if (!events.length) return { laneCount: 0, eventCount: 0 };

  const grouped = new Map();
  events.forEach((event) => {
    const key = laneKey(event);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(event);
  });
  const lanes = [...grouped.entries()]
    .map(([key, values]) => ({ key, values, first: values[0].at }))
    .sort((a, b) => b.first.localeCompare(a.first));
  const laneIndex = new Map(lanes.map((lane, index) => [lane.key, index]));

  const left = 178;
  const top = 48;
  const laneHeight = 72;
  const step = events.length > 120 ? 25 : events.length > 60 ? 31 : 42;
  const width = Math.max(980, left + 100 + events.length * step);
  const height = top + lanes.length * laneHeight + 34;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);

  const defs = element("defs");
  marker(defs, "normal", "normal");
  marker(defs, "branch", "branch");
  marker(defs, "handoff", "handoff");
  marker(defs, "waiting", "waiting");
  svg.append(defs);

  const positions = new Map();
  events.forEach((event, index) => positions.set(event.event_id, left + index * step));

  lanes.forEach((lane, index) => {
    const y = top + index * laneHeight;
    const [owner, runId] = lane.key.split("\u0000");
    const band = element("rect", { x: 0, y: y - 27, width, height: laneHeight });
    band.classList.add("flow-band");
    svg.append(band);
    const ownerText = element("text", { x: 16, y: y - 2 });
    ownerText.classList.add("lane-owner");
    ownerText.textContent = short(owner, 22);
    const runText = element("text", { x: 16, y: y + 18 });
    runText.classList.add("lane-run");
    runText.textContent = short(runId, 25);
    svg.append(ownerText, runText);

    lane.values.forEach((event, eventIndex) => {
      if (eventIndex === 0) return;
      const previous = lane.values[eventIndex - 1];
      const startX = positions.get(previous.event_id);
      const endX = positions.get(event.event_id);
      const type = semantics.branch.has(previous.event) || semantics.branch.has(event.event)
        ? "branch"
        : semantics.waiting.has(previous.event) || semantics.waiting.has(event.event)
          ? "waiting"
          : "normal";
      const branchOffset = type === "branch" ? 15 : 0;
      const path = element("path", {
        d: branchOffset
          ? `M ${startX} ${y} Q ${startX + 10} ${y + branchOffset} ${startX + 22} ${y + branchOffset} H ${Math.max(startX + 22, endX - 22)} Q ${endX - 10} ${y + branchOffset} ${endX} ${y}`
          : `M ${startX} ${y} H ${endX}`,
        "marker-end": `url(#arrow-${type})`,
      });
      path.classList.add("flow-edge", type);
      svg.append(path);
    });
  });

  events.forEach((event) => {
    const x = positions.get(event.event_id);
    const y = top + laneIndex.get(laneKey(event)) * laneHeight;
    const type = semantic(event.event);
    const node = type === "conflict"
      ? element("rect", { x: x - 6, y: y - 6, width: 12, height: 12, transform: `rotate(45 ${x} ${y})` })
      : element("circle", { cx: x, cy: y, r: event.event === "agent-joined" ? 7 : 6 });
    node.classList.add("flow-node", type);
    if (event.event === "agent-joined") node.classList.add("start");
    if (event.authority_effect === "terminal" || event.authority_effect === "release") node.classList.add("terminal");
    node.tabIndex = 0;
    const projectName = projectNames.get(event.workspace_id) ?? event.workspace_id;
    const show = () => setTooltip(tooltip, event, projectName, { x, y });
    node.addEventListener("mouseenter", show);
    node.addEventListener("focus", show);
    node.addEventListener("mouseleave", () => { tooltip.hidden = true; });
    node.addEventListener("blur", () => { tooltip.hidden = true; });
    svg.append(node);

    if (!semantics.handoff.has(event.event)) return;
    const target = event.details?.target_owner;
    if (!target) return;
    const targetLane = lanes.find((lane) => lane.key.startsWith(`${target}\u0000`));
    if (!targetLane) return;
    const targetY = top + laneIndex.get(targetLane.key) * laneHeight;
    const direction = targetY > y ? 1 : -1;
    const cross = element("path", {
      d: `M ${x} ${y + direction * 7} V ${targetY - direction * 7}`,
      "marker-end": "url(#arrow-handoff)",
    });
    cross.classList.add("flow-edge", "handoff", "cross-lane");
    svg.insertBefore(cross, node);
  });

  const first = events[0];
  const last = events[events.length - 1];
  const firstText = element("text", { x: left, y: 20 });
  firstText.classList.add("time-label");
  firstText.textContent = timestamp(first.at);
  const lastText = element("text", { x: positions.get(last.event_id), y: 20, "text-anchor": "end" });
  lastText.classList.add("time-label");
  lastText.textContent = timestamp(last.at);
  svg.append(firstText, lastText);

  return { laneCount: lanes.length, eventCount: events.length };
}
