# Dev Mesh 0.2.8

Released 2026-10-26. This release adds native Windows support while retaining the macOS/POSIX
backend. The coordination protocol remains `20260823.1` and event schema remains `2`; existing
workspaces do not require a cutover.

- Windows coordination uses exclusive native file handles. Managed Git children inherit the
  canonical and transaction fences, preserving exclusion when the calling process is terminated.
  POSIX continues to use `flock` and inherited descriptors.
- State writes flush file contents before publication and use a non-overwriting atomic rename on
  Windows. Directory fsync remains POSIX-only; Windows does not claim equivalent directory-metadata
  durability after power loss. Win32 stream, device, and case aliases are rejected for state files.
- Git queries batch path arguments below the Windows command-line limit, staging streams literal
  pathspecs through stdin, and Windows Git commands enable long paths locally. Raw workspace-byte
  projections use binary reads, preserving CRLF and Ctrl+Z bytes.
- The Windows Console starts without the macOS/Linux Unix Infra Discovery binding and reports that
  capability in its ready/status records. HTTP collection and dashboards remain available; the UI
  hides the unavailable discovery-repair action. The macOS LaunchAgent remains macOS-only.
- Windows/PowerShell usage is documented in both READMEs and the packaged skills. CI covers Windows,
  macOS, and Linux with Python 3.11 and 3.13.

Validation: the 249-test runtime suite passed on Windows (Python 3.13) and Ubuntu (Python 3.12), with
seven expected platform/environment skips in each run. Targeted checks additionally passed for the
packaged join/claim/edit/finish/leave lifecycle, the latest Console capability status, native Git
fencing after caller termination, and actual Windows default Console startup plus HTTP dashboard
responses. Compilation, entrypoint help, JavaScript syntax, and patch whitespace checks passed.

Build the release from the clean source commit with `scripts/plugin_dist.py`, then verify
`source_revision`, `tree_sha256`, and file count before synchronizing the package to marketplace.
