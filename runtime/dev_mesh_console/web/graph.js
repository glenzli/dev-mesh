import { eventLabel, language, t } from "/i18n.js";
import {
  activeRunKeys,
  buildFlowLayout,
  eventLaneKey as laneKey,
  identityKey,
  timeLabelMode,
  transactionBranchOffset,
  tooltipPosition,
} from "/flow_layout.js";

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
  "claim-baseline-required",
  "claim-baseline-accepted",
]);

const trackedWorkEvents = new Set([
  "claim-created",
  "claim-requested",
  "claim-baseline-required",
  "claim-baseline-accepted",
  "claim-completed",
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

function flowEventLabel(event) {
  return eventLabel(event.event);
}

function interactionPeer(event) {
  const source = identityKey(event.details?.source_owner, event.details?.source_run_id);
  const target = identityKey(event.details?.target_owner, event.details?.target_run_id);
  const sourceOwner = event.details?.source_owner;
  const targetOwner = event.details?.target_owner;
  if (event.event === "handoff-offered" || event.event === "handoff-withdrawn") {
    return { runKey: target, owner: targetOwner };
  }
  if (event.event === "handoff-accepted" || event.event === "handoff-rejected") {
    return { runKey: source, owner: sourceOwner };
  }
  if (event.event === "message-sent" && !event.handoff_id) {
    return { runKey: target, owner: targetOwner };
  }
  if (event.event === "message-acknowledged" && event.details?.interaction_kind !== "handoff") {
    return { runKey: source, owner: sourceOwner };
  }
  return null;
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
  // A microtransaction is one continuous temporary branch.  Keeping every
  // transaction event on its branch track avoids a visual zig-zag at each
  // lifecycle transition; only entering and leaving that transaction crosses
  // between the main line and the branch track.
  return transactionBranchOffset(event);
}

function connectorPath(startX, startY, endX, endY) {
  if (startY === endY) return `M ${startX} ${startY} H ${endX}`;
  const bend = Math.min(18, Math.max(8, (endX - startX) / 3));
  return `M ${startX} ${startY} C ${startX + bend} ${startY}, ${endX - bend} ${endY}, ${endX} ${endY}`;
}

function nodeClearance(event) {
  if (event.event === "contention-decision-proposed" || offeredEvents.has(event.event)) return 7;
  if (stoppedEvents.has(event.event) || event.event === "contention-decision-responded") return 6;
  if (event.event === "work-suspended" || event.event === "claim-paused") return 5;
  if (semantic(event.event) === "conflict") return 9;
  if (event.event === "agent-joined") return 7;
  return compactEvents.has(event.event) ? 4.5 : 6;
}

function nodeRightExtent(event, workNumber) {
  if (workNumber) return String(workNumber).length > 2 ? 15 : 14;
  return nodeClearance(event);
}

function isTimelineAnchor(event) {
  return event.event === "agent-joined"
    || event.authority_effect === "terminal"
    || event.authority_effect === "release";
}

function buildEventPositions(events, left, workNumbers) {
  const positions = new Map();
  const previousByLane = new Map();
  // The flow preserves event order instead of pretending to be a duration
  // chart.  Keep a modest global gap as well as a larger same-run gap so
  // dense lifecycle clusters remain readable without inventing time.
  const globalGap = 12;
  let previousGlobalX = left - globalGap;
  events.forEach((event) => {
    const key = laneKey(event);
    const previous = previousByLane.get(key);
    let x = previousGlobalX + globalGap;
    if (previous) {
      const anchorGap = isTimelineAnchor(previous.event) || isTimelineAnchor(event) ? 6 : 0;
      const minimumLaneGap = nodeRightExtent(
        previous.event,
        workNumbers.get(workKey(previous.event)),
      ) + nodeClearance(event) + 18 + anchorGap;
      x = Math.max(x, previous.x + minimumLaneGap);
    }
    positions.set(event.event_id, x);
    previousByLane.set(key, { event, x });
    previousGlobalX = x;
  });
  return positions;
}

function eventConnectorPath(previous, event, startX, startY, endX, endY) {
  const available = Math.max(0, endX - startX);
  const targetInset = Math.min(nodeClearance(event) + 2.5, Math.max(3, available - 5));
  const sourceInset = Math.min(
    nodeClearance(previous) + 1,
    Math.max(0, available - targetInset - 5),
  );
  return connectorPath(startX + sourceInset, startY, endX - targetInset, endY);
}

function clearLandingX(desiredX, occupied, minimumX, maximumX) {
  const clearance = 12;
  const candidates = [0, 12, -12, 20, -20, 28, -28]
    .map((offset) => desiredX + offset)
    .filter((value) => value >= minimumX && value <= maximumX);
  return candidates.find((value) => occupied.every((nodeX) => Math.abs(nodeX - value) >= clearance))
    ?? desiredX;
}

function crossLanePath(sourceX, sourceY, targetX, targetY) {
  if (sourceX === targetX) return `M ${sourceX} ${sourceY} V ${targetY}`;
  return `M ${sourceX} ${sourceY} H ${targetX} V ${targetY}`;
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
  title.textContent = selfResponse ? t("flow.selfConfirmationTitle") : flowEventLabel(event);
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
  if (event.run_id) {
    const run = document.createElement("span");
    run.textContent = `${t("flow.run")} ${short(event.run_id, 34)}`;
    tooltip.append(run);
  }
  if (semantics.handoff.has(event.event)) {
    const peer = interactionPeer(event);
    if (peer?.owner) {
      const target = document.createElement("span");
      target.textContent = [
        `${t("flow.target")} ${short(peer.owner, 24)}`,
        peer.runKey ? short(peer.runKey.split("\u0000")[1], 28) : t("flow.ownerLevel"),
      ].join(" · ");
      tooltip.append(target);
    }
  }
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
  const position = tooltipPosition(point, {
    scrollLeft: tooltip.parentElement.scrollLeft,
    clientWidth: tooltip.parentElement.clientWidth,
    tooltipWidth: tooltip.offsetWidth,
  });
  tooltip.style.left = `${position.left}px`;
  tooltip.style.top = `${position.top}px`;
}

function marker(defs, name, colorClass) {
  const value = element("marker", {
    id: `arrow-${name}`,
    viewBox: "0 0 10 10",
    refX: 9,
    refY: 5,
    markerWidth: 4.5,
    markerHeight: 4.5,
    orient: "auto-start-reverse",
  });
  value.classList.add(`marker-${colorClass}`);
  value.append(element("path", { d: "M 2 2 L 8 5 L 2 8" }));
  defs.append(value);
}

function renderOwnerRail(ownerRail, layout) {
  ownerRail.replaceChildren();
  ownerRail.style.height = `${layout.height}px`;
  layout.ownerRows.forEach((row) => {
    const card = document.createElement("div");
    card.className = "flow-owner-card";
    if (row.ownerOnly) card.classList.add("owner-only");
    card.style.top = `${row.y - 26}px`;
    const badge = document.createElement("span");
    badge.className = "flow-owner-badge";
    badge.textContent = "OWNER";
    const owner = document.createElement("strong");
    owner.textContent = short(row.owner, 20);
    const run = document.createElement("span");
    run.className = "flow-owner-run";
    run.textContent = row.ownerOnly
      ? t("flow.noRuns")
      : row.runs.length === 1
        ? short(row.runs[0].runId, 25)
        : `${row.runs.length} ${t("flow.runSegments")}`;
    const heading = document.createElement("div");
    heading.className = "flow-owner-heading";
    heading.append(badge, owner);
    card.append(heading, run);
    ownerRail.append(card);
  });
}

export function renderFlow(svg, ownerRail, tooltip, dashboard, projectNames) {
  svg.replaceChildren();
  ownerRail.replaceChildren();
  tooltip.hidden = true;
  const events = dashboard.events;
  if (!events.length) {
    ownerRail.style.height = "0px";
    return { laneCount: 0, runCount: 0, eventCount: 0, workCount: 0, width: 0 };
  }

  // Owner identity cards end at x=208.  Keep a visible buffer before the
  // first execution node so a short flow never reads as part of the card.
  const left = 238;
  const layout = buildFlowLayout(events);
  const lanes = layout.runLanes;
  const laneKeys = new Set(lanes.map((lane) => lane.key));
  const activeRuns = activeRunKeys(dashboard.active_details ?? []);
  const lanePositions = layout.runPositions;
  const ownerRowsByOwner = new Map(layout.ownerRows.map((row) => [row.owner, row]));
  const workNumbers = new Map();
  events.forEach((event) => {
    const key = workKey(event);
    if (key && !workNumbers.has(key)) workNumbers.set(key, workNumbers.size + 1);
  });
  const positions = buildEventPositions(events, left, workNumbers);
  const farthestX = Math.max(...positions.values());
  const width = Math.max(980, farthestX + 70);
  const height = layout.height;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "xMinYMin meet");
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  renderOwnerRail(ownerRail, layout);

  const defs = element("defs");
  marker(defs, "normal", "normal");
  marker(defs, "branch", "branch");
  marker(defs, "handoff", "handoff");
  marker(defs, "waiting", "waiting");
  marker(defs, "decision", "decision");
  marker(defs, "accepted", "accepted");
  marker(defs, "rejected", "rejected");
  svg.append(defs);

  layout.groups.forEach((group) => {
    const background = element("rect", {
      x: 5,
      y: group.top,
      width: width - 10,
      height: group.height,
      rx: 8,
    });
    background.classList.add("flow-group-band");
    if (group.related) background.classList.add("related");
    svg.append(background);
    if (group.related) {
      const rail = element("rect", {
        x: 4,
        y: group.top + 5,
        width: 3,
        height: Math.max(8, group.height - 10),
        rx: 1.5,
      });
      rail.classList.add("flow-group-rail");
      svg.append(rail);
    }
    if (group.separatorY !== null) {
      const divider = element("line", {
        x1: 0,
        y1: group.separatorY,
        x2: width,
        y2: group.separatorY,
      });
      divider.classList.add("flow-group-divider");
      svg.append(divider);
    }
  });

  const nodeXsByLane = new Map();
  events.forEach((event) => {
    const x = positions.get(event.event_id);
    const key = laneKey(event);
    if (!nodeXsByLane.has(key)) nodeXsByLane.set(key, []);
    nodeXsByLane.get(key).push(x);
  });
  const ownerGuideExtents = new Map(
    layout.ownerRows.map((row) => [row.owner, { start: Infinity, end: -Infinity }]),
  );
  lanes.forEach((lane) => {
    const extent = ownerGuideExtents.get(lane.owner);
    extent.start = Math.min(extent.start, positions.get(lane.values[0].event_id));
    extent.end = Math.max(extent.end, positions.get(lane.values[lane.values.length - 1].event_id));
  });
  events.forEach((event) => {
    const peer = interactionPeer(event);
    if (!peer?.owner || (peer.runKey && laneKeys.has(peer.runKey))) return;
    const extent = ownerGuideExtents.get(peer.owner);
    if (!extent) return;
    const x = positions.get(event.event_id);
    extent.start = Math.min(extent.start, x);
    extent.end = Math.max(extent.end, x);
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

  layout.ownerRows.forEach((row) => {
    const band = element("rect", {
      x: 10,
      y: row.top + 3,
      width: width - 20,
      height: Math.max(16, row.height - 6),
      rx: 7,
    });
    band.classList.add("flow-band");
    svg.append(band);
    const rowRail = element("rect", {
      x: 10,
      y: row.top + 8,
      width: 4,
      height: Math.max(8, row.height - 16),
      rx: 2,
    });
    rowRail.classList.add("owner-row-rail");
    svg.append(rowRail);
    const extent = ownerGuideExtents.get(row.owner);
    if (Number.isFinite(extent?.start) && Number.isFinite(extent?.end)) {
      const guide = element("line", {
        x1: extent.start,
        y1: row.y,
        x2: Math.max(extent.start + 12, extent.end),
        y2: row.y,
      });
      guide.classList.add("owner-guide");
      svg.append(guide);
    }
  });

  lanes.forEach((lane) => {
    const y = lanePositions.get(lane.key);
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
        d: eventConnectorPath(previous, event, startX, startY, endX, endY),
        "marker-end": `url(#arrow-${type})`,
      });
      path.classList.add("flow-edge", type);
      svg.append(path);
    });
    const ownerRow = ownerRowsByOwner.get(lane.owner);
    if (ownerRow?.runs.length > 1) {
      const runLabel = element("text", {
        x: positions.get(lane.values[0].event_id) + 10,
        y: y - 11,
      });
      runLabel.classList.add("run-segment-label");
      runLabel.textContent = `R${lane.ordinal}`;
      svg.append(runLabel);
    }
  });

  events.forEach((event) => {
    const x = positions.get(event.event_id);
    const laneY = lanePositions.get(laneKey(event));
    const y = laneY + branchOffset(event);
    const type = semantic(event.event);
    const proposal = event.event === "contention-decision-responded"
      ? proposals.get(decisionKey(event))
      : null;
    const selfResponse = event.event === "contention-decision-responded" && isSelfResponse(event, proposal);
    const workNumber = workNumbers.get(workKey(event)) ?? null;
    const activeRunStart = event.event === "agent-joined" && activeRuns.has(laneKey(event));
    const node = eventNode(event, x, y, type, selfResponse);
    node.classList.add("flow-node", type);
    node.classList.add(`event-${event.event}`);
    node.setAttribute(
      "aria-label",
      [
        flowEventLabel(event),
        event.owner,
        event.run_id,
        event.scope,
        event.transaction_id,
        workNumber ? `${t("flow.work")} ${workNumber}` : null,
        activeRunStart ? t("flow.running") : null,
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
    if (activeRunStart) node.classList.add("active-run-start");
    if (event.event === "claim-paused" && event.details?.disposition) {
      node.classList.add(`pause-${event.details.disposition}`);
    }
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
    if (activeRunStart) {
      const halo = element("circle", { cx: x, cy: y, r: 11, "aria-hidden": "true" });
      halo.classList.add("flow-active-run-halo");
      svg.append(halo);
    }
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
      const peer = interactionPeer(event);
      const exactTarget = Boolean(peer?.runKey && laneKeys.has(peer.runKey));
      const targetRow = peer?.owner ? ownerRowsByOwner.get(peer.owner) : null;
      const targetCenterY = exactTarget ? lanePositions.get(peer.runKey) : targetRow?.y;
      const sameSource = exactTarget
        ? peer.runKey === laneKey(event)
        : peer?.owner === event.owner;
      if (Number.isFinite(targetCenterY) && !sameSource && targetCenterY !== laneY) {
        const direction = targetCenterY > y ? 1 : -1;
        const targetX = exactTarget
          ? clearLandingX(x, nodeXsByLane.get(peer.runKey) ?? [], left, width - 12)
          : x;
        const targetY = exactTarget
          ? targetCenterY - direction * 8
          : direction > 0
            ? targetRow.top + 3
            : targetRow.top + targetRow.height - 3;
        const cross = element("path", {
          d: crossLanePath(x, y + direction * 7, targetX, targetY),
          "marker-end": "url(#arrow-handoff)",
        });
        cross.classList.add("flow-edge", "handoff", "cross-lane");
        if (!exactTarget) cross.classList.add("owner-level");
        svg.insertBefore(cross, node);
        if (!exactTarget) {
          const ownerAnchor = element("circle", {
            cx: targetX,
            cy: targetY,
            r: 3.5,
          });
          ownerAnchor.classList.add("owner-link-anchor");
          svg.insertBefore(ownerAnchor, node);
        }
      }
    }

    if (event.event === "contention-decision-proposed") {
      const linkedLanes = new Set();
      (event.details?.contention_participants ?? []).forEach((participant) => {
        const targetKey = `${participant.owner}\u0000${participant.run_id}`;
        if (targetKey === laneKey(event) || linkedLanes.has(targetKey) || !laneKeys.has(targetKey)) {
          return;
        }
        linkedLanes.add(targetKey);
        const targetY = lanePositions.get(targetKey);
        if (targetY === laneY) return;
        const direction = targetY > y ? 1 : -1;
        const targetX = clearLandingX(
          x,
          nodeXsByLane.get(targetKey) ?? [],
          left,
          width - 12,
        );
        const cross = element("path", {
          d: crossLanePath(x, y + direction * 8, targetX, targetY - direction * 8),
          "marker-end": "url(#arrow-decision)",
        });
        cross.classList.add("flow-edge", "decision", "cross-lane");
        svg.insertBefore(cross, node);
      });
    }

    if (event.event === "contention-decision-responded" && proposal) {
      const targetKey = laneKey(proposal);
      if (targetKey !== laneKey(event) && laneKeys.has(targetKey)) {
        const targetY = lanePositions.get(targetKey);
        if (targetY === laneY) return;
        const direction = targetY > y ? 1 : -1;
        const response = event.details?.accepted ? "accepted" : "rejected";
        const targetX = clearLandingX(
          x,
          nodeXsByLane.get(targetKey) ?? [],
          left,
          width - 12,
        );
        const cross = element("path", {
          d: crossLanePath(x, y + direction * 8, targetX, targetY - direction * 8),
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
      if (targetKey === laneKey(event) || linkedLanes.has(targetKey) || !laneKeys.has(targetKey)) {
        return;
      }
      linkedLanes.add(targetKey);
      const targetY = lanePositions.get(targetKey);
      if (targetY === laneY) return;
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
  const ruler = element("line", {
    x1: left,
    y1: layout.timeAxisY + 4,
    x2: width - 12,
    y2: layout.timeAxisY + 4,
  });
  ruler.classList.add("flow-time-axis");
  const firstLabel = timestamp(first.at);
  const lastLabel = timestamp(last.at);
  if (timeLabelMode(left, positions.get(last.event_id)) === "range") {
    const rangeText = element("text", { x: left, y: layout.timeAxisY });
    rangeText.classList.add("time-label", "time-range-label");
    rangeText.textContent = firstLabel === lastLabel ? firstLabel : `${firstLabel} → ${lastLabel}`;
    svg.append(ruler, rangeText);
  } else {
    const firstText = element("text", { x: left, y: layout.timeAxisY });
    firstText.classList.add("time-label");
    firstText.textContent = firstLabel;
    const lastText = element("text", {
      x: positions.get(last.event_id),
      y: layout.timeAxisY,
      "text-anchor": "end",
    });
    lastText.classList.add("time-label");
    lastText.textContent = lastLabel;
    svg.append(ruler, firstText, lastText);
  }

  return {
    laneCount: layout.ownerRows.length,
    runCount: lanes.length,
    eventCount: events.length,
    workCount: workNumbers.size,
    relationshipGroupCount: layout.groupCount,
    width,
  };
}
