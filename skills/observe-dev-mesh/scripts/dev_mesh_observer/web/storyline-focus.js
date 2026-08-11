"use strict";

(() => {
  const SYSTEM_OWNERS = new Set(["__canonical__", "__system__"]);

  function relationOwners(relation) {
    return [...new Set([
      ...(relation.owners || []),
      relation.source_owner,
      relation.target_owner,
    ].filter((owner) => owner && !SYSTEM_OWNERS.has(owner)))];
  }

  function select(story = {}) {
    const focusOwners = new Set(story.focus?.owners || []);
    if (!focusOwners.size) return story;

    const relations = (story.relations || []).filter((relation) => {
      if (relation.id === story.focus?.relation_id) return true;
      const owners = relationOwners(relation);
      return owners.length > 0 && owners.every((owner) => focusOwners.has(owner));
    });
    const referencedItems = new Set(
      relations.flatMap((relation) => [relation.source_item, relation.target_item]).filter(Boolean),
    );
    const spans = (story.spans || []).filter((span) => focusOwners.has(span.owner));
    const markers = (story.markers || []).filter((marker) => {
      if (referencedItems.has(marker.id)) return true;
      if (focusOwners.has(marker.lane)) return true;
      const owners = marker.owners || [];
      return owners.length > 0 && owners.every((owner) => focusOwners.has(owner));
    });
    const visibleItems = new Set([
      ...spans.map((span) => span.id),
      ...markers.map((marker) => marker.id),
    ]);
    const boundedRelations = relations.filter((relation) => (
      (!relation.source_item || visibleItems.has(relation.source_item))
      && (!relation.target_item || visibleItems.has(relation.target_item))
    ));
    const usedOwners = new Set([
      ...spans.map((span) => span.owner),
      ...markers.flatMap((marker) => marker.owners || []),
      ...boundedRelations.flatMap(relationOwners),
      ...focusOwners,
    ]);
    const lanes = (story.lanes || [])
      .filter((lane) => lane.kind === "canonical" || usedOwners.has(lane.id))
      .map((lane) => ({
        ...lane,
        item_count: lane.kind === "canonical"
          ? markers.filter((marker) => marker.lane === lane.id).length
          : spans.filter((span) => span.owner === lane.id).length,
      }));

    return {
      ...story,
      lanes,
      spans,
      markers,
      relations: boundedRelations,
      summary: {
        ...(story.summary || {}),
        actors: [...usedOwners].filter((owner) => !SYSTEM_OWNERS.has(owner)).length,
        work_spans: spans.length,
        markers: markers.length,
        relations: boundedRelations.length,
        visible_items: spans.length + markers.length,
        focused: true,
      },
    };
  }

  window.DevMeshStorylineFocus = { relationOwners, select };
})();
