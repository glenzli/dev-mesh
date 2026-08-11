"use strict";

(() => {
  const LEFT = 22;
  const RIGHT = 26;
  const MOMENT_STEP = 32;
  const CANONICAL_HEIGHT = 44;
  const CANONICAL_RAIL_OFFSET = 25;
  const CANONICAL_BRANCH_STEP = 14;
  const CANONICAL_BOTTOM = 10;
  const AGENT_HEIGHT = 88;
  const SYSTEM_HEIGHT = 54;
  const NODE_TIER = 20;
  const BRANCH_TIER = 24;
  const BRANCH_TRANSITION = 14;

  function displayRunId(span) {
    return span.run_id || span.inferred_run_id || null;
  }

  function branchEpisodes(spans, relations) {
    const episodes = [];
    const byTransaction = new Map();
    const bySpan = new Map();
    spans.filter((span) => span.kind === "transaction").forEach((span) => {
      const transactionId = span.details?.transaction_id || span.id;
      let episode = byTransaction.get(transactionId);
      if (!episode) {
        episode = { transactionId, spanIds: [], startedAt: null, endedAt: null, status: "active" };
        byTransaction.set(transactionId, episode);
        episodes.push(episode);
      }
      episode.spanIds.push(span.id);
      bySpan.set(span.id, episode);
      if (!episode.startedAt || String(span.started_at) < String(episode.startedAt)) {
        episode.startedAt = span.started_at;
      }
      if (!episode.latestAt || String(span.started_at) >= String(episode.latestAt)) {
        episode.latestAt = span.started_at;
        episode.endedAt = span.ended_at || null;
        episode.status = span.status || "active";
      }
    });

    relations.filter((relation) => ["fork", "rejoin"].includes(relation.kind)).forEach((relation) => {
      const transactionId = relation.details?.transaction_id;
      const episode = (transactionId ? byTransaction.get(transactionId) : null)
        || bySpan.get(relation.kind === "fork" ? relation.target_item : relation.source_item);
      if (!episode) return;
      if (relation.kind === "fork") episode.forkAt = relation.at;
      else episode.rejoinAt = relation.at;
    });

    const levelEnds = [];
    episodes.sort((left, right) => (
      String(left.forkAt || left.startedAt).localeCompare(String(right.forkAt || right.startedAt))
    )).forEach((episode) => {
      const start = episode.forkAt || episode.startedAt || "";
      const end = episode.rejoinAt || episode.endedAt || "\uffff";
      let level = levelEnds.findIndex((levelEnd) => String(levelEnd) <= String(start));
      if (level < 0) level = levelEnds.length;
      levelEnds[level] = end;
      episode.level = level;
    });
    return { episodes, levelCount: levelEnds.length };
  }

  function laneHeight(lane, canonicalBranchLevels) {
    if (lane.kind === "canonical") {
      return Math.max(
        CANONICAL_HEIGHT,
        CANONICAL_RAIL_OFFSET + canonicalBranchLevels * CANONICAL_BRANCH_STEP + CANONICAL_BOTTOM,
      );
    }
    if (lane.kind === "system") return SYSTEM_HEIGHT;
    return AGENT_HEIGHT;
  }

  function compute(story = {}) {
    const lanes = story.lanes || [];
    const spans = story.spans || [];
    const markers = story.markers || [];
    const relations = story.relations || [];
    const canonicalBranches = branchEpisodes(spans, relations);
    const laneGeometry = new Map();
    let cursorY = 0;
    lanes.forEach((lane) => {
      const height = laneHeight(lane, canonicalBranches.levelCount);
      const center = lane.kind === "canonical"
        ? cursorY + CANONICAL_RAIL_OFFSET
        : cursorY + height / 2;
      laneGeometry.set(lane.id, { ...lane, y: cursorY, center, height });
      cursorY += height;
    });

    const moments = new Set();
    spans.forEach((span) => {
      if (span.started_at) moments.add(span.started_at);
      if (["session", "transaction", "waiting", "diverted"].includes(span.kind) && span.ended_at) {
        moments.add(span.ended_at);
      }
    });
    markers.forEach((marker) => { if (marker.at) moments.add(marker.at); });
    relations.forEach((relation) => { if (relation.at) moments.add(relation.at); });
    const orderedMoments = [...moments].sort((left, right) => String(left).localeCompare(String(right)));
    const indexByMoment = new Map(orderedMoments.map((moment, index) => [moment, index]));
    const xAt = (moment, fallback = 0) => LEFT + (indexByMoment.get(moment) ?? fallback) * MOMENT_STEP;
    const finalIndex = Math.max(1, orderedMoments.length - 1);

    const canonicalLane = [...laneGeometry.values()].find((lane) => lane.kind === "canonical");
    const canonicalBranchGeometry = canonicalLane
      ? canonicalBranches.episodes.map((episode) => {
          const startAt = episode.forkAt || episode.startedAt;
          const endAt = episode.rejoinAt || episode.endedAt;
          const x1 = xAt(startAt);
          const x2 = endAt
            ? xAt(endAt, finalIndex)
            : LEFT + (finalIndex + 1) * MOMENT_STEP;
          return {
            id: `canonical-branch:${episode.transactionId}`,
            transaction_id: episode.transactionId,
            status: episode.status,
            level: episode.level,
            x1,
            x2: Math.max(x1 + MOMENT_STEP, x2),
            railY: canonicalLane.center,
            y: canonicalLane.center + (episode.level + 1) * CANONICAL_BRANCH_STEP,
            merged: Boolean(episode.rejoinAt),
            aborted: episode.status === "aborted",
          };
        })
      : [];

    const spanGeometry = new Map();
    spans.forEach((span) => {
      const lane = laneGeometry.get(span.owner);
      if (!lane) return;
      const startIndex = indexByMoment.get(span.started_at) ?? 0;
      const endIndex = span.ended_at
        ? indexByMoment.get(span.ended_at) ?? startIndex + 1
        : Math.max(startIndex + 1, finalIndex + 1);
      const binding = span.run_binding || (span.run_id ? "native" : "unbound");
      const yOffset = span.kind === "transaction"
        ? BRANCH_TIER
        : span.kind === "claim" && ["inferred", "unbound", "mixed"].includes(binding)
          ? NODE_TIER
          : 0;
      const x1 = LEFT + startIndex * MOMENT_STEP;
      const x2 = LEFT + Math.max(startIndex + 1, endIndex) * MOMENT_STEP;
      spanGeometry.set(span.id, {
        x1,
        x2,
        y: lane.center + yOffset,
        lane: span.owner,
        kind: span.kind,
        runId: displayRunId(span),
        binding,
        width: Math.max(18, x2 - x1),
      });
    });

    const compoundGroups = new Map();
    spans.forEach((span) => {
      if (!span.run_id || !span.started_at || !["session", "claim"].includes(span.kind)) return;
      const key = `${span.owner}\u0000${span.run_id}\u0000${span.started_at}`;
      if (!compoundGroups.has(key)) compoundGroups.set(key, []);
      compoundGroups.get(key).push(span);
    });
    compoundGroups.forEach((members) => {
      const session = members.find((span) => span.kind === "session");
      const claims = members.filter((span) => span.kind === "claim");
      if (!session || !claims.length) return;
      const sessionGeometry = spanGeometry.get(session.id);
      const lane = laneGeometry.get(session.owner);
      if (!sessionGeometry || !lane) return;
      sessionGeometry.y = lane.center;
      sessionGeometry.compoundRole = "session";
      sessionGeometry.compoundRoot = session.id;
      claims.forEach((claim, index) => {
        const geometry = spanGeometry.get(claim.id);
        if (!geometry) return;
        geometry.x1 = sessionGeometry.x1 + 6 + index * 4;
        geometry.y = sessionGeometry.y - 6;
        geometry.compoundRole = "claim";
        geometry.compoundRoot = session.id;
      });
    });

    const markerGeometry = new Map();
    markers.forEach((marker) => {
      const lane = laneGeometry.get(marker.lane);
      if (!lane) return;
      markerGeometry.set(marker.id, {
        x: xAt(marker.at),
        y: lane.center,
        lane: marker.lane,
      });
    });

    const runSpineGeometry = [];
    const spineByOwnerRun = new Map();
    spans.filter((span) => span.kind === "session" && span.run_id).forEach((session) => {
      const geometry = spanGeometry.get(session.id);
      const lane = laneGeometry.get(session.owner);
      if (!geometry || !lane) return;
      const matchingSpans = spans.filter((span) => (
        span.owner === session.owner && displayRunId(span) === session.run_id
      ));
      const latestActionX = matchingSpans.reduce((latest, span) => {
        const item = spanGeometry.get(span.id);
        if (!item) return latest;
        return Math.max(latest, span.kind === "transaction" ? item.x2 : item.x1);
      }, geometry.x1);
      const observedEndX = LEFT + finalIndex * MOMENT_STEP;
      const x2 = session.ended_at
        ? xAt(session.ended_at, (indexByMoment.get(session.started_at) ?? 0) + 1)
        : Math.max(geometry.x1 + 24, latestActionX, observedEndX);
      const spine = {
        id: `run-spine:${session.owner}:${session.run_id}`,
        owner: session.owner,
        run_id: session.run_id,
        status: session.status || "observed",
        source_item: session.id,
        x1: geometry.x1,
        x2: Math.max(geometry.x1 + 18, x2),
        y: lane.center,
      };
      geometry.y = spine.y;
      runSpineGeometry.push(spine);
      spineByOwnerRun.set(`${session.owner}\u0000${session.run_id}`, spine);
    });

    const transactionContextById = new Map();
    const coveredIntervalsBySpine = new Map();

    function spineForSpan(span, geometry) {
      const runId = displayRunId(span);
      const direct = runId
        ? spineByOwnerRun.get(`${span.owner}\u0000${runId}`)
        : null;
      if (direct) return direct;
      const candidates = runSpineGeometry.filter((candidate) => (
        candidate.owner === span.owner
        && candidate.x1 <= geometry.x1
        && candidate.x2 >= geometry.x2
      ));
      return candidates.length === 1 ? candidates[0] : null;
    }

    function coverSpine(spine, span, geometry, transition = 0) {
      if (!spine || !geometry) return;
      if (!coveredIntervalsBySpine.has(spine.id)) coveredIntervalsBySpine.set(spine.id, []);
      coveredIntervalsBySpine.get(spine.id).push({ span, geometry, transition });
    }

    spans.filter((span) => span.kind === "transaction").forEach((span) => {
      const geometry = spanGeometry.get(span.id);
      const lane = laneGeometry.get(span.owner);
      if (!geometry || !lane || lane.kind !== "agent") return;
      geometry.y = lane.center + BRANCH_TIER;
      geometry.mainY = lane.center;
      geometry.branchContext = true;

      const transactionId = span.details?.transaction_id;
      if (transactionId) {
        if (!transactionContextById.has(transactionId)) transactionContextById.set(transactionId, []);
        transactionContextById.get(transactionId).push({ span, geometry });
      }

      coverSpine(spineForSpan(span, geometry), span, geometry, BRANCH_TRANSITION);
    });

    function transactionContext(owner, transactionId, x) {
      const candidates = (transactionContextById.get(transactionId) || [])
        .filter((context) => context.span.owner === owner);
      const containing = candidates.filter((context) => (
        context.geometry.x1 <= x && context.geometry.x2 >= x
      ));
      if (containing.length === 1) return containing[0];
      return candidates.length === 1 ? candidates[0] : null;
    }

    spans.filter((span) => span.kind !== "transaction").forEach((span) => {
      const geometry = spanGeometry.get(span.id);
      const transactionId = span.details?.transaction_id;
      if (!geometry || !transactionId) return;
      const context = transactionContext(span.owner, transactionId, geometry.x1);
      if (!context) return;
      geometry.y = context.geometry.y;
      geometry.branchContext = true;
      geometry.branchTransactionId = transactionId;
      delete geometry.compoundRole;
      delete geometry.compoundRoot;
    });

    markers.forEach((marker) => {
      const geometry = markerGeometry.get(marker.id);
      const transactionId = marker.details?.transaction_id;
      if (!geometry || !transactionId) return;
      const context = transactionContext(marker.lane, transactionId, geometry.x);
      if (!context) return;
      geometry.y = context.geometry.y;
      geometry.branchContext = true;
      geometry.branchTransactionId = transactionId;
    });

    spans.filter((span) => ["waiting", "diverted"].includes(span.kind)).forEach((span) => {
      const geometry = spanGeometry.get(span.id);
      if (!geometry || geometry.branchContext) return;
      coverSpine(spineForSpan(span, geometry), span, geometry);
    });

    const simultaneousGroups = new Map();
    spans.forEach((span) => {
      const runId = displayRunId(span);
      const geometry = spanGeometry.get(span.id);
      if (
        !runId || !geometry || !span.started_at || span.kind !== "claim"
        || geometry.compoundRole || geometry.branchContext
      ) return;
      const key = `${span.owner}\u0000${runId}\u0000${span.started_at}`;
      if (!simultaneousGroups.has(key)) simultaneousGroups.set(key, []);
      simultaneousGroups.get(key).push(span);
    });
    simultaneousGroups.forEach((members) => {
      if (members.length < 2) return;
      members.sort((left, right) => String(left.id).localeCompare(String(right.id)));
      const offsets = [0, -NODE_TIER, NODE_TIER, -29, 29];
      members.forEach((span, index) => {
        const geometry = spanGeometry.get(span.id);
        const spine = spineByOwnerRun.get(`${span.owner}\u0000${displayRunId(span)}`);
        if (!geometry || !spine) return;
        geometry.y = spine.y + (offsets[index] ?? ((index % 2 ? -1 : 1) * 29));
        geometry.stacked = index > 0;
      });
    });

    const attachmentGeometry = [];
    spans.forEach((span) => {
      if (span.kind !== "claim") return;
      const runId = displayRunId(span);
      const geometry = spanGeometry.get(span.id);
      const spine = spineByOwnerRun.get(`${span.owner}\u0000${runId}`);
      if (
        !runId || !geometry || !spine || geometry.compoundRole === "claim"
        || geometry.branchContext
      ) return;
      if (Math.abs(geometry.y - spine.y) < 0.5) return;
      attachmentGeometry.push({
        id: `run-attachment:${span.id}`,
        owner: span.owner,
        run_id: runId,
        binding: span.run_binding || (span.run_id ? "native" : "unbound"),
        source_item: spine.source_item,
        target_item: span.id,
        source: { x: geometry.x1, y: spine.y, lane: span.owner },
        target: { x: geometry.x1, y: geometry.y, lane: span.owner },
      });
    });

    const visibleRunSpineGeometry = [];
    runSpineGeometry.forEach((spine) => {
      const intervals = (coveredIntervalsBySpine.get(spine.id) || [])
        .sort((left, right) => left.geometry.x1 - right.geometry.x1);
      if (!intervals.length) {
        visibleRunSpineGeometry.push({ ...spine, segment: "canonical", final: true });
        return;
      }
      let cursor = spine.x1;
      intervals.forEach(({ span, geometry, transition }, index) => {
        const coveredStart = Math.max(spine.x1, geometry.x1 - transition);
        const coveredEnd = Math.min(spine.x2, geometry.x2 + transition);
        if (span.kind === "transaction") {
          geometry.forkX = coveredStart;
          geometry.rejoinX = coveredEnd;
        }
        if (coveredStart - cursor >= 2) {
          visibleRunSpineGeometry.push({
            ...spine,
            id: `${spine.id}:canonical:${index}`,
            x1: cursor,
            x2: coveredStart,
            segment: "canonical",
            final: false,
          });
        }
        cursor = Math.max(cursor, coveredEnd);
      });
      if (spine.x2 - cursor >= 2) {
        visibleRunSpineGeometry.push({
          ...spine,
          id: `${spine.id}:canonical:final`,
          x1: cursor,
          segment: "canonical",
          final: true,
        });
      }
    });

    function itemPoint(identifier, edge = "center") {
      const marker = markerGeometry.get(identifier);
      if (marker) return marker;
      const span = spanGeometry.get(identifier);
      if (!span) return null;
      const resolvedEdge = edge === "center" && span.kind !== "transaction"
        ? "start"
        : edge;
      return {
        x: resolvedEdge === "start"
          ? span.x1
          : resolvedEdge === "end"
            ? span.x2
            : (span.x1 + span.x2) / 2,
        y: span.y,
        lane: span.lane,
      };
    }

    const relationGeometry = new Map();
    relations.forEach((relation) => {
      const x = xAt(relation.at);
      const transactionIdentifier = relation.kind === "fork"
        ? relation.target_item
        : ["rejoin", "return"].includes(relation.kind)
          ? relation.source_item
          : null;
      const transactionGeometry = transactionIdentifier
        ? spanGeometry.get(transactionIdentifier)
        : null;
      const localBranch = transactionGeometry?.branchContext === true;
      const sourceItem = localBranch
        ? relation.kind === "fork"
          ? {
              x: transactionGeometry.forkX ?? transactionGeometry.x1 - BRANCH_TRANSITION,
              y: transactionGeometry.mainY,
              lane: transactionGeometry.lane,
            }
          : {
              x: transactionGeometry.x2,
              y: transactionGeometry.y,
              lane: transactionGeometry.lane,
            }
        : relation.source_item
          ? itemPoint(relation.source_item, relation.kind === "rejoin" ? "end" : "start")
          : null;
      const targetItem = localBranch
        ? relation.kind === "fork"
          ? {
              x: transactionGeometry.x1,
              y: transactionGeometry.y,
              lane: transactionGeometry.lane,
            }
          : {
              x: transactionGeometry.rejoinX ?? transactionGeometry.x2 + BRANCH_TRANSITION,
              y: transactionGeometry.mainY,
              lane: transactionGeometry.lane,
            }
        : relation.target_item
          ? itemPoint(relation.target_item, relation.kind === "fork" ? "start" : "center")
          : null;
      const sourceLane = laneGeometry.get(relation.source_owner);
      const targetLane = laneGeometry.get(relation.target_owner);
      const endpointRunId = relation.target_endpoint?.run_id;
      const endpointSpine = endpointRunId && relation.target_owner
        ? spineByOwnerRun.get(`${relation.target_owner}\u0000${endpointRunId}`)
        : null;
      const source = sourceItem
        || (sourceLane ? { x, y: sourceLane.center, lane: sourceLane.id } : null);
      const target = targetItem
        || (endpointSpine
          ? { x, y: endpointSpine.y, lane: endpointSpine.owner }
          : targetLane
            ? { x, y: targetLane.center, lane: targetLane.id }
            : null);
      const ownerPoints = (relation.owners || [])
        .map((owner) => laneGeometry.get(owner))
        .filter(Boolean)
        .map((lane) => ({ x, y: lane.center, lane: lane.id }));
      relationGeometry.set(relation.id, {
        x,
        source,
        target,
        targetEndpoint: relation.target_endpoint && target
          ? { ...relation.target_endpoint, point: target }
          : null,
        owners: ownerPoints,
        branchContext: localBranch,
      });
    });

    return {
      width: Math.max(680, LEFT + Math.max(1, orderedMoments.length) * MOMENT_STEP + RIGHT),
      height: Math.max(180, cursorY),
      moments: orderedMoments,
      momentGeometry: orderedMoments.map((moment) => ({ moment, x: xAt(moment) })),
      laneGeometry,
      spanGeometry,
      markerGeometry,
      canonicalBranchGeometry,
      runSpineGeometry: visibleRunSpineGeometry,
      attachmentGeometry,
      relationGeometry,
    };
  }

  window.DevMeshStorylineLayout = { compute };
})();
