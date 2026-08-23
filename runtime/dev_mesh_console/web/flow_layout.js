export function identityKey(owner, runId) {
  return owner && runId ? `${owner}\u0000${runId}` : null;
}

export function eventLaneKey(event) {
  if (event.owner || event.run_id) {
    return `${event.owner ?? "unknown"}\u0000${event.run_id ?? "unknown"}`;
  }
  const source = event.details?.source_owner;
  return `${source ?? "unattributed"}\u0000unattributed`;
}

const transactionEvents = new Set([
  "transaction-created",
  "transaction-prepared",
  "transaction-validated",
  "transaction-refreshed",
  "transaction-conflicted",
  "transaction-published",
  "transaction-aborted",
]);

export function transactionBranchOffset(event) {
  return transactionEvents.has(event.event) ? 15 : 0;
}

export function timeLabelMode(firstX, lastX, { labelWidth = 102, gap = 16 } = {}) {
  return lastX - firstX < labelWidth * 2 + gap ? "range" : "endpoints";
}

export function activeRunKeys(activeDetails = []) {
  return new Set(
    activeDetails
      .filter((item) => item?.kind === "run" && item.status === "active")
      .map((item) => identityKey(item.owner, item.run_id))
      .filter(Boolean),
  );
}

export function tooltipPosition(
  point,
  { scrollLeft = 0, clientWidth = 0, tooltipWidth = 0 },
  { gap = 16, edgeInset = 8 } = {},
) {
  const visibleLeft = scrollLeft + edgeInset;
  const visibleRight = scrollLeft + clientWidth - edgeInset;
  const maxLeft = Math.max(visibleLeft, visibleRight - tooltipWidth);
  return {
    left: Math.max(visibleLeft, Math.min(point.x + gap, maxLeft)),
    top: point.y + 12,
  };
}

function relatedOwners(event) {
  const owners = new Set([
    event.owner,
    event.details?.source_owner,
    event.details?.target_owner,
  ].filter(Boolean));
  (event.details?.contention_participants ?? []).forEach((participant) => {
    if (participant.owner) owners.add(participant.owner);
  });
  return [...owners];
}

function assignRunSlots(runs) {
  const slotEnds = [];
  runs
    .sort((left, right) => left.firstIndex - right.firstIndex || left.key.localeCompare(right.key))
    .forEach((run, index) => {
      let slot = slotEnds.findIndex((end) => end < run.firstIndex);
      if (slot < 0) slot = slotEnds.length;
      slotEnds[slot] = run.lastIndex;
      run.slot = slot;
      run.ordinal = index + 1;
    });
  return Math.max(1, slotEnds.length);
}

export function buildFlowLayout(
  events,
  { top = 76, laneHeight = 72, subLaneGap = 24, groupGap = 16 } = {},
) {
  const groupedRuns = new Map();
  events.forEach((event, index) => {
    const key = eventLaneKey(event);
    if (!groupedRuns.has(key)) groupedRuns.set(key, []);
    groupedRuns.get(key).push({ event, index });
  });
  const runLanes = [...groupedRuns.entries()].map(([key, entries]) => {
    const [owner, runId] = key.split("\u0000");
    return {
      key,
      owner,
      runId,
      values: entries.map(({ event }) => event),
      first: entries[0].event.at,
      last: entries[entries.length - 1].event.at,
      firstIndex: entries[0].index,
      lastIndex: entries[entries.length - 1].index,
      slot: 0,
      ordinal: 1,
    };
  });
  const ownerReferences = new Map();
  const noteOwner = (owner, runId, at) => {
    if (!owner || !at) return;
    if (!ownerReferences.has(owner)) ownerReferences.set(owner, []);
    ownerReferences.get(owner).push({at, runId});
  };
  events.forEach((event) => {
    noteOwner(event.owner, event.run_id, event.at);
    noteOwner(event.details?.source_owner, event.details?.source_run_id, event.at);
    noteOwner(event.details?.target_owner, event.details?.target_run_id, event.at);
    (event.details?.contention_participants ?? []).forEach((participant) => {
      noteOwner(participant.owner, participant.run_id, event.at);
    });
  });
  const rowsByOwner = new Map();
  runLanes.forEach((run) => {
    if (!rowsByOwner.has(run.owner)) rowsByOwner.set(run.owner, []);
    rowsByOwner.get(run.owner).push(run);
  });
  ownerReferences.forEach((_times, owner) => {
    if (!rowsByOwner.has(owner)) rowsByOwner.set(owner, []);
  });
  const ownerRows = [...rowsByOwner.entries()].map(([owner, runs]) => {
    const references = ownerReferences.get(owner) ?? [];
    const referenceTimes = references.map((reference) => reference.at);
    const slotCount = runs.length ? assignRunSlots(runs) : 1;
    const first = runs.length
      ? runs.reduce((value, run) => run.first < value ? run.first : value, runs[0].first)
      : referenceTimes.reduce(
        (value, at) => at < value ? at : value,
        referenceTimes[0],
      );
    const recentStart = runs.length
      ? runs.reduce((value, run) => run.first > value ? run.first : value, runs[0].first)
      : referenceTimes.reduce(
        (value, at) => at > value ? at : value,
        referenceTimes[0],
      );
    const last = runs.length
      ? runs.reduce((value, run) => run.last > value ? run.last : value, runs[0].last)
      : referenceTimes.reduce(
        (value, at) => at > value ? at : value,
        referenceTimes[0],
      );
    return {
      owner,
      runs,
      ownerOnly: runs.length === 0,
      referencedRunIds: [...new Set(references.map((reference) => reference.runId).filter(Boolean))],
      slotCount,
      height: laneHeight + (slotCount - 1) * subLaneGap,
      first,
      recentStart,
      last,
    };
  });
  const parent = new Map(ownerRows.map((row) => [row.owner, row.owner]));
  const root = (owner) => {
    let current = owner;
    while (parent.get(current) !== current) current = parent.get(current);
    let cursor = owner;
    while (parent.get(cursor) !== current) {
      const next = parent.get(cursor);
      parent.set(cursor, current);
      cursor = next;
    }
    return current;
  };
  const union = (left, right) => {
    const leftRoot = root(left);
    const rightRoot = root(right);
    if (leftRoot !== rightRoot) parent.set(rightRoot, leftRoot);
  };
  events.forEach((event) => {
    const owners = relatedOwners(event);
    owners.slice(1).forEach((owner) => union(owners[0], owner));
  });

  const groupedOwners = new Map();
  ownerRows.forEach((row) => {
    const key = root(row.owner);
    if (!groupedOwners.has(key)) groupedOwners.set(key, []);
    groupedOwners.get(key).push(row);
  });
  const orderedGroups = [...groupedOwners.values()]
    .map((rows) => ({
      rows: rows.sort((left, right) => (
        right.recentStart.localeCompare(left.recentStart)
        || left.owner.localeCompare(right.owner)
      )),
      latest: rows.reduce((value, row) => row.last > value ? row.last : value, rows[0].last),
    }))
    .sort((left, right) => right.latest.localeCompare(left.latest));

  const orderedRows = [];
  const groups = [];
  const runPositions = new Map();
  let cursorY = top;
  orderedGroups.forEach((group, groupIndex) => {
    const groupTop = cursorY - laneHeight / 2;
    group.rows.forEach((row, memberIndex) => {
      row.y = cursorY;
      row.top = cursorY - laneHeight / 2;
      row.groupIndex = groupIndex;
      row.groupStart = memberIndex === 0;
      row.runs.forEach((run) => runPositions.set(run.key, row.y + run.slot * subLaneGap));
      orderedRows.push(row);
      cursorY += row.height;
    });
    const groupBottom = cursorY - laneHeight / 2;
    groups.push({
      index: groupIndex,
      top: groupTop,
      height: groupBottom - groupTop,
      separatorY: groupIndex > 0 ? groupTop - groupGap / 2 : null,
      related: group.rows.length > 1,
      ownerCount: group.rows.length,
    });
    if (groupIndex < orderedGroups.length - 1) cursorY += groupGap;
  });

  return {
    runLanes,
    ownerRows: orderedRows,
    groups,
    runPositions,
    groupCount: groups.length,
    // Keep the time ruler clear of the first owner band.  It is deliberately
    // independent of the lane baseline so a one-row flow does not look like
    // the ruler is part of that Agent's execution line.
    timeAxisY: Math.max(20, top - laneHeight / 2 - 14),
    height: cursorY + 24,
  };
}
