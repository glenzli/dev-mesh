# Console interaction review — 2026-09-20

The Console required an absolute path to add a watched folder. Its default dashboard also devoted
substantial space to completed contentions, listed each Run and its Claims separately, and relied
on a wide relationship graph to explain the collaboration sequence.

## Changes

- **Folder selection:** a local directory browser replaces required path entry. Users can navigate
  breadcrumbs, go to the parent or home directory, reopen watched locations, filter names on the
  server, and include hidden directories. Empty folders are selectable. Direct path entry remains
  an optional disclosure. Selecting a directory does not register it until “Watch this folder”.
- **Registration feedback:** exact duplicate roots are visibly disabled. If registration succeeds
  while collection is busy or fails, the API reports the durable registration separately and the
  UI says that collection has not completed. It does not invite a duplicate registration retry.
- **Current work:** one card groups a Run with its Claims using workspace, Owner and Run identity.
  Full Run IDs and scopes remain inspectable. Cards with pending states sort first; an open Run
  without a Claim is explicitly labeled. Project and collaboration-record shortcuts reduce searching.
- **Information hierarchy:** the top metrics consistently show open Runs, work scopes, recorded
  collaborations and actionable notes. They link to the corresponding section. Completed contention
  details are collapsed, while active contention and retained wait Claims retain their guidance.
- **Collaboration history:** a searchable activity list is the default. Record groups use exact
  workspace and contention, handoff or message IDs. Expanding a group shows chronological steps and
  participant identities. Unknown recipient Runs remain explicitly unbound. Filters separate
  contention handling, messages and handoffs; “Show more” exposes further groups within the query.
  The relationship graph and cross-project overview remain available through the graph view.
- **Interaction continuity:** language changes retain expanded record groups and the selected view.
  Dashboard requests reject superseded responses when filters change quickly. Background refresh
  pauses while the user interacts with history, current-work cards, contention details or a dialog.
  Refresh and Discovery checks now report errors and successful outcomes in a visible status area.
- **Language and layout:** new controls have Chinese/English labels, keyboard focus and responsive
  layouts. Message recording and terminal contention decisions are named without implying message
  delivery or automatic release of a Claim.

## Ownership and boundaries

`directories.py` owns non-recursive directory navigation. The endpoint returns directory names and
paths only, with a 200-entry limit and server-side filtering; it does not read file contents or
initialize coordination state. The existing literal-loopback and same-origin checks protect it.
Directory browsing itself does not alter the watched-root registry.

`root_picker.js` owns navigation, cancellation, selection, duplicate detection, registration and
feedback. `activity_history.js` owns exact history grouping and its searchable disclosure view.
The application shell retains dashboard loading and composition. The coordination protocol,
authority rules, reviewed Run-close workflow and Observer data schemas are unchanged. No Run or
Claim from another task was changed. This work adds selection and shortcuts for watched roots;
it does not add an unfollow or historical-data deletion workflow.

## Validation

- Console directories, history, runtime and dashboard suites: **30 tests passed, no skips** with
  loopback access enabled. Cases cover directory-only traversal, Unicode and spaces, hidden folders,
  permissions, result limits and filtering beyond the initial page, foreign Host/Origin rejection,
  durable registration during collection failure, duplicate roots, and exact cross-workspace/Run
  history grouping. The history projection test also passed after final presentation changes.
- JavaScript syntax and `git diff --check` passed; Chinese/English key and placeholder parity checked.
- A wheel was built without dependencies or network resolution and installed into a temporary
  directory. All 19 Console source/resource files matched the installed package. That package
  served the new picker successfully through a real browser.
- Browser checks used an isolated database copy and temporary root registry. They covered browsing,
  choosing an empty folder, successful registration feedback, duplicate prevention, hidden folders,
  record-type filtering, chronological steps, retained expansion across language changes, graph/list
  switching, and rapid project changes. The final project view matched the last selection.
- Desktop and 390-pixel layout checks passed without page overflow. No unexpected browser warnings
  or errors occurred in the checked workflows.

The focused boundary is Console HTTP, state projection, assets and UI interaction. The earlier
full coordination test result was not rerun for unchanged protocol code. Logs, the wheel and source
identity evidence are retained under `/private/tmp/dev-mesh-console-ux-20260920/`; OS cleanup may
remove temporary evidence, while this document preserves the review and validation record.

## Local delivery

The existing `com.glenzli.dev-mesh.observer-console` LaunchAgent is restarted to load the verified
source. Its service configuration and existing watched roots are preserved. The live entry point is
`http://127.0.0.1:8765/`. This is a local Console update; no Git commit, remote push or plugin release
is part of this work. Earlier collaboration repairs and inherited Discovery changes remain intact.

## Padding follow-up

Live inspection found zero horizontal padding on the recent-contention disclosure and its nested
path section: the earlier path-section selector only matched a direct workbench child. The history
view controls also had no top inset beneath the note's separator.

The stylesheet now uses a shared panel inset of 16px on desktop and 14px at widths up to 700px.
Headers, project/current-work lists, diagnostics, history controls and event rows align to that
inset. The recent-contention summary and both direct/nested path sections have 12px vertical space,
and the history controls have 12px top spacing. Single-project current-work panels also retain
full width despite the newer two-column grid rule.

Validation used the live Console: computed desktop summary/path padding was `12px 16px`, and at
390px it was `12px 14px`, with no horizontal page overflow. Expanded and collapsed content was
visually checked. A scoped desktop view measured 1216px for both its grid and activity panel.
`git diff --check` passed. Only stylesheet and review-record changes were needed; the live server
serves the updated asset directly, so no backend restart or rebuild was necessary.

## Toolbar loading follow-up

The loading animation targeted the refresh/Discovery repair button itself, rotating its square
border and hit target. Both buttons now keep their frame stationary and replace the symbol with
a centered 16px progress ring while busy. Completion restores the original symbol. Existing
disabled state and accessible busy labels remain in place. The reduced-motion rule disables the
ring's animation while preserving the visible busy indicator.

Live validation at `http://127.0.0.1:8765/` clicked Discovery repair and observed `aria-busy=true`,
a disabled button, a visible animated ring and a hidden symbol. The button's animation and
transform both remained `none`; its rectangle stayed at 34×34px with unchanged position before,
during and after the request. Completion restored the enabled button and original symbol. A real
refresh also completed and restored its idle state. The served CSS included the updated
reduced-motion rule; that preference was inspected in CSS, not emulated. Browser warnings/errors
were empty and `git diff --check` passed. These HTML/CSS changes are served directly by the live
Console; no backend restart, package rebuild or additional protocol tests were needed.
