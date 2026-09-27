# Tyrell interface reuse

Wayland's terminal interface is derived from the existing Tyrell UI at source
revision 74005b8 (the development worktree supplied by the application owner).
The source repository was read, never modified.

Reused interaction components live in `src/wayland/tui`: the curses dashboard
renderer/event loop, composer, terminal escape-sequence decoder, markdown/history
presentation and cache, selection/clipboard, links, appearance editor, progress
presentation, and animated startup. The original clipboard and composer tests
are carried forward with Wayland fixtures. Formatting was normalized.

The runtime boundary is replaced rather than connected to Tyrell:
- A Wayland-only Unix socket under its own root, with mode 0600.
- Independent role IDs, conversations and UI preferences in Wayland's SQLite DB.
- Trading setup replaces repository, worktree and development-tool permissions.
- Market, positions, orders and audit replace developer-specific content.
- Direct OpenAI API analytical chat; no Codex/Copilot/OpenCode CLI discovery.
- No Tyrell database, agent sessions, provider processes or credentials are read.

Role archive/hide operations affect presentation only, never actual positions,
order intentions or execution state. UI chat cannot approve or submit trades.
The entry kill switch writes through to the existing deterministic runtime state.
Settings remain paper-only and never remove the LIVE lock.

The local operator service is distinct from the broker monitor. Opening the UI
does not start an autonomous trading loop, establish brokerage connectivity or
claim that stored quotes are fresh. Detaching the UI preserves analytical work.
An interrupted service records the incomplete analysis as waiting at next startup.

Keeping a local source copy avoids adding Tyrell as a runtime dependency. This
means later Tyrell improvements must be ported deliberately and regression tested.
