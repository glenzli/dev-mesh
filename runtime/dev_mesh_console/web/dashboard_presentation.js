const activeCoordinationKinds = new Set(["contention", "handoff"]);
const terminalContentionEvents = new Map([
  ["contention-cancelled", "cancelled"],
  ["contention-completed", "completed"],
]);

function count(value) {
  return Number(value ?? 0);
}

function contentionIdentifier(value) {
  return value?.contention_id ?? value?.object_id ?? "";
}

function participantKey(value) {
  return `${value?.owner ?? ""}\u0000${value?.run_id ?? ""}\u0000${value?.scope ?? ""}`;
}

export function contentionSummaries(dashboard) {
  const active = new Map(
    (dashboard?.active_details ?? [])
      .filter((item) => item.kind === "contention")
      .map((item) => [contentionIdentifier(item), item]),
  );
  const grouped = new Map();
  (dashboard?.coordination?.events ?? []).forEach((event) => {
    if (!event.contention_id && !event.details?.contention_id) return;
    const id = event.contention_id ?? event.details.contention_id;
    if (!id) return;
    const summary = grouped.get(id) ?? {
      active: active.has(id),
      contentionId: id,
      openedAt: null,
      participants: [],
      reasonCode: "",
      status: active.has(id) ? "active" : "resolved",
      updatedAt: null,
      workspaceId: event.workspace_id ?? active.get(id)?.workspace_id ?? "",
    };
    if (event.event === "contention-opened") summary.openedAt = event.at ?? summary.openedAt;
    const terminalStatus = terminalContentionEvents.get(event.event);
    if (terminalStatus) {
      summary.status = terminalStatus;
      summary.reasonCode = event.details?.reason_code ?? event.reason_code ?? "";
    }
    summary.updatedAt = event.at ?? summary.updatedAt;
    const participants = event.details?.contention_participants ?? [];
    const known = new Set(summary.participants.map(participantKey));
    participants.forEach((participant) => {
      const key = participantKey(participant);
      if (!known.has(key)) {
        summary.participants.push(participant);
        known.add(key);
      }
    });
    grouped.set(id, summary);
  });
  active.forEach((item, id) => {
    if (!id || grouped.has(id)) return;
    grouped.set(id, {
      active: true,
      contentionId: id,
      openedAt: item.created_at ?? null,
      participants: item.details?.contention_participants ?? [],
      reasonCode: "",
      status: "active",
      updatedAt: item.updated_at ?? item.created_at ?? null,
      workspaceId: item.workspace_id ?? "",
    });
  });
  return [...grouped.values()]
    .map((summary) => active.has(summary.contentionId)
      ? {...summary, active: true, status: "active"}
      : summary)
    .sort((left, right) => Number(right.active) - Number(left.active)
      || String(right.updatedAt ?? "").localeCompare(String(left.updatedAt ?? "")));
}

export function dashboardPresentation(dashboard, selectedWorkspace = "") {
  const coordination = dashboard?.coordination ?? {};
  const projectCollaboration = projectHistoryProjection(dashboard?.project_collaboration, selectedWorkspace);
  const activeDetails = dashboard?.active_details ?? [];
  const summaries = contentionSummaries(dashboard);
  const activeContentions = summaries.filter((item) => item.active);
  const relevantContentions = activeContentions.length ? activeContentions : summaries;
  const displayedContentions = relevantContentions.slice(0, 3);
  const activeRunKeys = new Set(
    activeDetails
      .filter((item) => item.kind === "run")
      .map((item) => `${item.workspace_id ?? ""}\u0000${item.run_id ?? item.object_id ?? ""}`),
  );
  const affectedRunKeys = new Set(
    relevantContentions.flatMap((item) => item.participants)
      .map((item) => `${item.owner ?? ""}\u0000${item.run_id ?? ""}`),
  );
  const showWorkbench = displayedContentions.length > 0;
  const hasActiveCoordination = activeDetails.some((item) => activeCoordinationKinds.has(item.kind));
  const attention = activeContentions.length > 0 || hasActiveCoordination;
  // History is evidence in the observation window, not current authority.
  const showProjectRelations = count(projectCollaboration.collaboration_relation_count) > 0;
  const showFlow = count(coordination.relation_count) > 0
    && count(coordination.event_count) > 0;
  const quiet = (dashboard?.projects?.length ?? 0) > 0
    && !showWorkbench
    && !hasActiveCoordination;

  return {
    activeContentionCount: activeContentions.length,
    activeRunCount: activeRunKeys.size,
    affectedRunCount: affectedRunKeys.size,
    attention,
    contentionCount: summaries.length,
    defaultOpenFlow: true,
    contentions: displayedContentions,
    independentRunCount: count(coordination.independent_run_count),
    projectCollaboration,
    requestedPathCount: (dashboard?.operational?.contention?.hot_paths ?? []).length,
    quiet,
    showFlow,
    showProjectRelations,
    showProjects: !selectedWorkspace,
    showWorkbench,
  };
}

export function projectHistoryProjection(projection = {}, selectedWorkspace = "") {
  // The Observer supplies a global overview even for a scoped dashboard.
  // Review includes only explicit relationships touching the selected project.
  const known = new Set((projection.nodes ?? []).map((node) => node.workspace_id));
  const edges = (projection.edges ?? []).filter((edge) =>
    count(edge.collaboration_count) > 0
    && known.has(edge.source_workspace_id) && known.has(edge.target_workspace_id)
    && edge.source_workspace_id !== edge.target_workspace_id
    && (!selectedWorkspace || edge.source_workspace_id === selectedWorkspace || edge.target_workspace_id === selectedWorkspace),
  );
  const related = new Set(edges.flatMap((edge) => [edge.source_workspace_id, edge.target_workspace_id]));
  return {
    nodes: (projection.nodes ?? []).filter((node) => related.has(node.workspace_id)),
    edges,
    hint_groups: [],
    collaboration_relation_count: edges.length,
  };
}
