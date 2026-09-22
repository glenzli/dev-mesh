// Group only exact identities. Display state comes from snapshots; steps come from recorded events.
export function activeWorkGroups(items = []) {
  const groups = new Map();
  for (const item of items) {
    const key = JSON.stringify([item.workspace_id, item.owner, item.run_id || item.object_id]);
    const group = groups.get(key) ?? {key, workspaceId: item.workspace_id, owner: item.owner,
      runId: item.run_id, run: null, objects: []};
    if (item.kind === "run") group.run = item;
    else group.objects.push(item);
    groups.set(key, group);
  }
  return [...groups.values()].sort((a, b) => {
    const pending = group => group.objects.some(item => item.status !== "active");
    return Number(pending(b)) - Number(pending(a)) || a.key.localeCompare(b.key);
  });
}

export function collaborationHistory(events = [], workspace = "") {
  const groups = new Map();
  for (const event of [...events].sort((a, b) => String(a.at).localeCompare(String(b.at)) || String(a.event_id).localeCompare(String(b.event_id)))) {
    if (workspace && workspace !== event.workspace_id) continue;
    const d = event.details ?? {};
    const identity = event.contention_id || d.contention_id ? ["contention", event.contention_id || d.contention_id]
      : event.handoff_id || d.handoff_id ? ["handoff", event.handoff_id || d.handoff_id]
      : d.message_id ? ["message", d.message_id]
      : ["event", event.event_id];
    const key = JSON.stringify([event.workspace_id, ...identity]);
    const group = groups.get(key) ?? {key, kind: identity[0], id: identity[1], workspaceId: event.workspace_id, steps: [], participants: []};
    if (group.steps.some(step => step.event_id === event.event_id)) continue;
    group.steps.push(event);
    const participants = d.contention_participants ?? [
      {owner: d.source_owner || event.owner, run_id: d.source_run_id || event.run_id, scope: event.scope},
      {owner: d.target_owner, run_id: d.target_run_id},
    ];
    for (const participant of participants) {
      if (!participant.owner) continue;
      const existing = group.participants.find(item => item.owner === participant.owner && item.run_id === participant.run_id && item.scope === participant.scope);
      if (!existing) group.participants.push(participant);
    }
    group.updatedAt = event.at;
    group.latest = event;
    groups.set(key, group);
  }
  return [...groups.values()].sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)) || a.key.localeCompare(b.key));
}

export function createActivityHistory({t, eventLabel, formatTime}) {
  const list = document.getElementById("history-list");
  const search = document.getElementById("history-search");
  const filter = document.getElementById("history-kind");
  const more = document.getElementById("history-more");
  const count = document.getElementById("history-count");
  const expanded = new Set();
  let dashboard = null;
  let workspace = "";
  let shown = 20;
  const make = (tag, cls, text) => {
    const node = document.createElement(tag);
    node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  function render() {
    if (!dashboard) return;
    const names = new Map(dashboard.projects.map(project => [project.workspace_id, project.name]));
    const query = search.value.toLocaleLowerCase();
    const groups = collaborationHistory(dashboard.coordination?.events, workspace).filter(group => {
      if (filter.value && group.kind !== filter.value) return false;
      const searchable = [group.id, names.get(group.workspaceId), ...group.participants.flatMap(p => [p.owner, p.run_id, p.scope]),
        ...group.steps.flatMap(step => [eventLabel(step.event), step.details?.reason_code])].filter(Boolean).join(" ").toLocaleLowerCase();
      return !query || searchable.includes(query);
    });
    count.textContent = t("history.count", {shown: Math.min(shown, groups.length), total: groups.length});
    more.hidden = shown >= groups.length;
    list.replaceChildren(...groups.slice(0, shown).map(group => {
      const card = make("details", "history-card");
      card.open = expanded.has(group.key);
      card.addEventListener("toggle", () => {if (card.isConnected) {if (card.open) expanded.add(group.key); else expanded.delete(group.key);}});
      const summary = make("summary", "history-card-heading");
      const identity = make("div", "history-identity");
      const actors = [...new Set(group.participants.map(p => p.owner))].join(" ↔ ");
      identity.append(make("strong", "", actors || group.id), make("span", "", `${names.get(group.workspaceId) || ""} · ${eventLabel(group.latest.event)} · ${t("history.steps", {count: group.steps.length})}`));
      const time = make("time", "", formatTime(group.updatedAt));
      time.dateTime = group.updatedAt;
      summary.append(identity, time);
      card.append(summary);
      const facts = make("div", "history-facts");
      facts.append(make("code", "", group.id));
      group.participants.forEach(p => facts.append(make("div", "", [p.owner, p.run_id || t("history.ownerOnly"), p.scope].filter(Boolean).join(" · "))));
      card.append(facts);
      const steps = make("ol", "history-steps");
      group.steps.forEach(event => {
        const item = make("li", "");
        const line = make("div", "history-step-heading");
        line.append(make("strong", "", eventLabel(event.event)), make("time", "", formatTime(event.at)));
        const d = event.details ?? {};
        const localized = (prefix, value) => !value ? "" : t(`${prefix}.${value}`) === `${prefix}.${value}` ? value : t(`${prefix}.${value}`);
        const decision = localized("history.decision", d.decision);
        const reason = localized("history.reason", d.reason_code);
        const response = typeof d.accepted === "boolean" ? t(d.accepted ? "history.accepted" : "history.rejected") : "";
        item.append(line, make("span", "", [event.owner, event.scope, decision, response, reason].filter(Boolean).join(" · ")));
        steps.append(item);
      });
      card.append(steps);
      return card;
    }));
    if (!groups.length) list.append(make("p", "history-empty", t(query || filter.value ? "history.noMatch" : "history.empty")));
    document.getElementById("history-limit").hidden = !dashboard.coordination?.events_truncated;
  }
  search.addEventListener("input", () => {shown = 20; render();});
  filter.addEventListener("change", () => {shown = 20; render();});
  more.addEventListener("click", () => {shown += 20; render();});
  return {update(value, selected) {dashboard = value; workspace = selected; render();}, reset() {shown = 20; search.value = ""; filter.value = ""; expanded.clear();}};
}
