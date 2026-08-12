import { eventLabel, language, t } from "/i18n.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const semantics = {
  branch: new Set([
    "transaction-created",
    "transaction-prepared",
    "transaction-validated",
    "transaction-refreshed",
    "transaction-conflicted",
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

const branchTrackEvents = new Set([
  "transaction-prepared",
  "transaction-validated",
  "transaction-refreshed",
  "transaction-conflicted",
  "transaction-aborted",
]);

const stoppedEvents = new Set([
  "transaction-aborted",
  "handoff-rejected",
  "handoff-withdrawn",
]);

const offeredEvents = new Set([
  "handoff-offered",
]);

const compactEvents = new Set([
  "message-sent",
  "message-acknowledged",
  "transaction-prepared",
  "transaction-validated",
  "transaction-refreshed",
  "direct-commit-started",
]);

const trackedWorkEvents = new Set([
  "claim-created",
  "claim-requested",
  "claim-released",
  "claim-paused",
  "claim-resumed",
  "work-suspended",
  "work-resumed",
  "direct-commit-started",
  "direct-commit-completed",
  ...semantics.branch,
]);

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

function workKey(event) {
  if (!event.scope || !trackedWorkEvents.has(event.event)) return null;
  return `${event.workspace_id}\u0000${event.scope}`;
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

function decisionLabel(value) {
  if (!value) return "—";
  const key = `decision.${value}`;
  const label = t(key);
  return label === key ? value : label;
}

function decisionKey(event) {
  return [event.workspace_id, event.contention_id, event.details?.revision].join("\u0000");
}

function isSelfResponse(event, proposal) {
  return Boolean(
    proposal
    && event.owner === proposal.owner
    && event.run_id === proposal.run_id,
  );
}

function responseLabel(event) {
  return event.details?.accepted ? t("flow.accepted") : t("flow.rejected");
}

function branchOffset(event) {
  return branchTrackEvents.has(event.event) ? 15 : 0;
}

function connectorPath(startX, startY, endX, endY) {
  if (startY === endY) return `M ${startX} ${startY} H ${endX}`;
  const bend = Math.min(18, Math.max(8, (endX - startX) / 3));
  return `M ${startX} ${startY} C ${startX + bend} ${startY}, ${endX - bend} ${endY}, ${endX} ${endY}`;
}

function eventNode(event, x, y, type, selfResponse) {
  if (event.event === "contention-decision-proposed" || offeredEvents.has(event.event)) {
    return element("path", { d: `M ${x - 6} ${y - 7} L ${x + 7} ${y} L ${x - 6} ${y + 7} Z` });
  }
  if (stoppedEvents.has(event.event)) {
    return element("path", { d: `M ${x - 6} ${y - 6} L ${x + 6} ${y + 6} M ${x + 6} ${y - 6} L ${x - 6} ${y + 6}` });
  }
  if (event.event === "contention-decision-responded") {
    return element("circle", { cx: x, cy: y, r: selfResponse ? 4 : 6 });
  }
  if (event.event === "work-suspended" || event.event === "claim-paused") {
    return element("rect", { x: x - 5, y: y - 5, width: 10, height: 10, rx: 2 });
  }
  if (type === "conflict") {
    return element("rect", { x: x - 6, y: y - 6, width: 12, height: 12, transform: `rotate(45 ${x} ${y})` });
  }
  return element("circle", {
    cx: x,
    cy: y,
    r: event.event === "agent-joined" ? 7 : compactEvents.has(event.event) ? 4.5 : 6,
  });
}

function setTooltip(
  tooltip,
  event,
  projectName,
  point,
  proposal = null,
  responses = [],
  workNumber = null,
) {
  tooltip.replaceChildren();
  const title = document.createElement("strong");
  const selfResponse = event.event === "contention-decision-responded" && isSelfResponse(event, proposal);
  title.textContent = selfResponse ? t("flow.selfConfirmationTitle") : eventLabel(event.event);
  const meta = document.createElement("span");
  meta.textContent = `${timestamp(event.at)} · ${projectName}`;
  const identity = document.createElement("span");
  if (event.event === "contention-decision-proposed") {
    identity.textContent = `${t("flow.proposer")} ${event.owner} · ${decisionLabel(event.details?.decision)}`;
  } else if (selfResponse) {
    identity.textContent = `${event.owner} · ${t("flow.selfConfirmed")} · ${responseLabel(event)}`;
  } else if (event.event === "contention-decision-responded") {
    identity.textContent = `${t("flow.responder")} ${event.owner} · ${responseLabel(event)}`;
  } else if (event.event === "contention-opened") {
    identity.textContent = `${t("flow.initiator")} ${event.owner}`;
  } else {
    identity.textContent = [event.owner, workNumber ? null : event.scope, event.details?.status].filter(Boolean).join(" · ") || event.event;
  }
  tooltip.append(title, meta, identity);
  if (workNumber && event.scope) {
    const work = document.createElement("span");
    work.textContent = `${t("flow.work")} ${workNumber} · ${event.scope}`;
    tooltip.append(work);
  }
  if (semantics.branch.has(event.event)) {
    const transaction = document.createElement("span");
    transaction.textContent = `${t("flow.transaction")} ${short(event.transaction_id, 28)}`;
    tooltip.append(transaction);
    if (event.details?.branch) {
      const branch = document.createElement("span");
      branch.textContent = `${t("flow.branch")} ${short(event.details.branch, 34)}`;
      tooltip.append(branch);
    }
    if (event.details?.canonical_branch) {
      const canonical = document.createElement("span");
      canonical.textContent = `${t("flow.mainBranch")} ${short(event.details.canonical_branch, 24)}`;
      tooltip.append(canonical);
    }
    if (event.details?.actual_path_count !== undefined) {
      const paths = document.createElement("span");
      paths.textContent = `${t("flow.changedPaths")} ${event.details.actual_path_count}`;
      tooltip.append(paths);
    }
  }
  if (event.event === "contention-decision-responded" && proposal && !selfResponse) {
    const proposalIdentity = document.createElement("span");
    proposalIdentity.textContent = `${t("flow.proposer")} ${proposal.owner} · ${decisionLabel(proposal.details?.decision)}`;
    tooltip.append(proposalIdentity);
  }
  if (event.event === "contention-decision-proposed") {
    const latestResponses = new Map();
    responses.forEach((response) => latestResponses.set(laneKey(response), response));
    const self = latestResponses.get(laneKey(event));
    if (self) {
      const confirmation = document.createElement("span");
      confirmation.textContent = `${t("flow.selfConfirmed")} · ${responseLabel(self)}`;
      tooltip.append(confirmation);
    }
    (event.details?.contention_participants ?? [])
      .filter((participant) => `${participant.owner}\u0000${participant.run_id}` !== laneKey(event))
      .forEach((participant) => {
        const key = `${participant.owner}\u0000${participant.run_id}`;
        const response = latestResponses.get(key);
        const status = document.createElement("span");
        status.textContent = `${short(participant.owner, 20)} · ${response ? responseLabel(response) : t("flow.awaitingResponse")}`;
        tooltip.append(status);
      });
  }
  const otherParticipants = (event.details?.contention_participants ?? []).filter(
    (participant) => participant.owner !== event.owner || participant.run_id !== event.run_id,
  );
  if (otherParticipants.length) {
    const conflict = document.createElement("span");
    conflict.textContent = `${t("flow.conflictsWith")} ${otherParticipants
      .map((participant) => `${short(participant.owner, 18)} · ${short(participant.scope, 20)}`)
      .join(" / ")}`;
    tooltip.append(conflict);
  }
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
  if (!events.length) return { laneCount: 0, eventCount: 0, workCount: 0 };

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
  marker(defs, "decision", "decision");
  marker(defs, "accepted", "accepted");
  marker(defs, "rejected", "rejected");
  svg.append(defs);

  const positions = new Map();
  events.forEach((event, index) => positions.set(event.event_id, left + index * step));
  const workNumbers = new Map();
  events.forEach((event) => {
    const key = workKey(event);
    if (key && !workNumbers.has(key)) workNumbers.set(key, workNumbers.size + 1);
  });
  const proposals = new Map(
    events
      .filter((event) => event.event === "contention-decision-proposed")
      .map((event) => [decisionKey(event), event]),
  );
  const responses = new Map();
  events
    .filter((event) => event.event === "contention-decision-responded")
    .forEach((event) => {
      const key = decisionKey(event);
      if (!responses.has(key)) responses.set(key, []);
      responses.get(key).push(event);
    });

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
      const type = semantics.branch.has(previous.event) && semantics.branch.has(event.event)
        ? "branch"
        : semantics.waiting.has(previous.event) || semantics.waiting.has(event.event)
          ? "waiting"
          : "normal";
      const startY = y + branchOffset(previous);
      const endY = y + branchOffset(event);
      const path = element("path", {
        d: connectorPath(startX, startY, endX, endY),
        "marker-end": `url(#arrow-${type})`,
      });
      path.classList.add("flow-edge", type);
      svg.append(path);
    });
  });

  events.forEach((event) => {
    const x = positions.get(event.event_id);
    const laneY = top + laneIndex.get(laneKey(event)) * laneHeight;
    const y = laneY + branchOffset(event);
    const type = semantic(event.event);
    const proposal = event.event === "contention-decision-responded"
      ? proposals.get(decisionKey(event))
      : null;
    const selfResponse = event.event === "contention-decision-responded" && isSelfResponse(event, proposal);
    const workNumber = workNumbers.get(workKey(event)) ?? null;
    const node = eventNode(event, x, y, type, selfResponse);
    node.classList.add("flow-node", type);
    node.classList.add(`event-${event.event}`);
    node.setAttribute(
      "aria-label",
      [
        eventLabel(event.event),
        event.owner,
        event.scope,
        event.transaction_id,
        workNumber ? `${t("flow.work")} ${workNumber}` : null,
      ].filter(Boolean).join(" · "),
    );
    if (event.event === "contention-decision-proposed") node.classList.add("decision-proposal");
    if (event.event === "contention-decision-responded") {
      node.classList.add(
        selfResponse
          ? "decision-self-response"
          : event.details?.accepted
            ? "decision-accepted"
            : "decision-rejected",
      );
    }
    if (event.event === "agent-joined") node.classList.add("start");
    if (event.authority_effect === "terminal" || event.authority_effect === "release") node.classList.add("terminal");
    node.tabIndex = 0;
    const projectName = projectNames.get(event.workspace_id) ?? event.workspace_id;
    const show = () => setTooltip(
      tooltip,
      event,
      projectName,
      { x, y },
      proposal,
      event.event === "contention-decision-proposed" ? responses.get(decisionKey(event)) ?? [] : [],
      workNumber,
    );
    node.addEventListener("mouseenter", show);
    node.addEventListener("focus", show);
    node.addEventListener("mouseleave", () => { tooltip.hidden = true; });
    node.addEventListener("blur", () => { tooltip.hidden = true; });
    svg.append(node);

    if (workNumber) {
      const label = String(workNumber);
      const badgeX = x + 7;
      const badgeY = y - 9;
      const badge = element("g", { "aria-hidden": "true" });
      badge.classList.add("work-number-badge");
      badge.append(
        element("circle", { cx: badgeX, cy: badgeY, r: label.length > 2 ? 8 : 7 }),
      );
      const text = element("text", { x: badgeX, y: badgeY + 2.5, "text-anchor": "middle" });
      text.textContent = label;
      badge.append(text);
      svg.append(badge);
    }

    if (semantics.handoff.has(event.event)) {
      const target = event.details?.target_owner;
      const targetLane = target
        ? lanes.find((lane) => lane.key.startsWith(`${target}\u0000`))
        : null;
      if (targetLane) {
        const targetY = top + laneIndex.get(targetLane.key) * laneHeight;
        const direction = targetY > y ? 1 : -1;
        const cross = element("path", {
          d: `M ${x} ${y + direction * 7} V ${targetY - direction * 7}`,
          "marker-end": "url(#arrow-handoff)",
        });
        cross.classList.add("flow-edge", "handoff", "cross-lane");
        svg.insertBefore(cross, node);
      }
    }

    if (event.event === "contention-decision-proposed") {
      const linkedLanes = new Set();
      (event.details?.contention_participants ?? []).forEach((participant) => {
        const targetKey = `${participant.owner}\u0000${participant.run_id}`;
        if (targetKey === laneKey(event) || linkedLanes.has(targetKey) || !laneIndex.has(targetKey)) {
          return;
        }
        linkedLanes.add(targetKey);
        const targetY = top + laneIndex.get(targetKey) * laneHeight;
        const direction = targetY > y ? 1 : -1;
        const cross = element("path", {
          d: `M ${x} ${y + direction * 8} V ${targetY - direction * 8}`,
          "marker-end": "url(#arrow-decision)",
        });
        cross.classList.add("flow-edge", "decision", "cross-lane");
        svg.insertBefore(cross, node);
      });
    }

    if (event.event === "contention-decision-responded" && proposal) {
      const targetKey = laneKey(proposal);
      if (targetKey !== laneKey(event) && laneIndex.has(targetKey)) {
        const targetY = top + laneIndex.get(targetKey) * laneHeight;
        const direction = targetY > y ? 1 : -1;
        const response = event.details?.accepted ? "accepted" : "rejected";
        const cross = element("path", {
          d: `M ${x} ${y + direction * 8} V ${targetY - direction * 8}`,
          "marker-end": `url(#arrow-${response})`,
        });
        cross.classList.add("flow-edge", response, "cross-lane");
        svg.insertBefore(cross, node);
      }
    }

    if (event.event !== "contention-opened") return;
    const linkedLanes = new Set();
    (event.details?.contention_participants ?? []).forEach((participant) => {
      const targetKey = `${participant.owner}\u0000${participant.run_id}`;
      if (targetKey === laneKey(event) || linkedLanes.has(targetKey) || !laneIndex.has(targetKey)) {
        return;
      }
      linkedLanes.add(targetKey);
      const targetY = top + laneIndex.get(targetKey) * laneHeight;
      const direction = targetY > y ? 1 : -1;
      const cross = element("path", {
        d: `M ${x} ${y + direction * 8} V ${targetY - direction * 8}`,
      });
      cross.classList.add("flow-edge", "conflict", "cross-lane");
      const anchor = element("rect", {
        x: x - 4,
        y: targetY - 4,
        width: 8,
        height: 8,
        transform: `rotate(45 ${x} ${targetY})`,
      });
      anchor.classList.add("conflict-link-node");
      svg.insertBefore(cross, node);
      svg.insertBefore(anchor, node);
    });
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

  return { laneCount: lanes.length, eventCount: events.length, workCount: workNumbers.size };
}
