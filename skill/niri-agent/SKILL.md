---
name: niri-agent
description: Drive GUI apps on the user's Linux desktop inside an isolated nested niri session on its own workspace, without touching the user's focus or input. Use when a task needs computer use (clicking, typing, screenshots of graphical apps) under the niri compositor.
---

# niri-agent

`niri-agent` gives you a private desktop: a nested niri compositor shown as one window on a host workspace named `agent-<name>`. Everything you launch, click, type and screenshot happens inside it. The user keeps working undisturbed and can watch by switching to that workspace.

All commands print JSON. Errors go to stderr with exit code 1.

## Workflow

1. Start a session and remember its name:
   ```bash
   niri-agent start            # -> {"name": "a1", "screen": {"width": 1280, "height": 800, ...}, ...}
   niri-agent start research --size 1600x1000
   ```
2. Launch apps **inside** it (never with plain shell backgrounding, which would open on the user's desktop):
   ```bash
   niri-agent run a1 -- firefox --new-instance --profile /tmp/agent-ff https://example.com
   niri-agent run a1 -- kitty
   ```
   Apps fill the screen. X11 apps work through xwayland-satellite if it is installed.
3. Look, act, look again:
   ```bash
   niri-agent screenshot a1                 # -> {"path": ".../shots/....png", "width": 1280, "height": 800}
   niri-agent click a1 640 400              # --button right|middle, --double
   niri-agent type a1 "hello world"         # '-' reads stdin
   niri-agent key a1 ctrl+l Return          # combos in order; modifiers: shift ctrl alt super
   niri-agent scroll a1 640 400 5           # positive = down; --horizontal
   niri-agent drag a1 100 100 400 300
   niri-agent move a1 640 400
   ```
   Take a screenshot after every action that changes the screen; do not assume it worked.
4. Manage windows through the nested niri's IPC:
   ```bash
   niri-agent msg a1 -- -j windows          # ids, titles, app ids, focus
   niri-agent msg a1 -- action focus-window --id 3
   niri-agent msg a1 -- action close-window --id 3
   niri-agent msg a1 -- action focus-column-left
   ```
5. Stop the session when done. This closes every app in it and removes the workspace:
   ```bash
   niri-agent stop a1
   ```

## Rules

- Coordinates are screenshot pixels. Read them straight off the latest screenshot; no scaling.
- Keyboard input goes to the focused window of the session. Click a window (or `focus-window`) first.
- Keys are delivered straight to the app; they never trigger niri keybindings. Use `msg` for window management.
- Never use `ydotool`, `wtype`, `xdotool`, `grim` or `niri msg` against the host yourself. They act on the user's desktop.
- Never export the session's `WAYLAND_DISPLAY` or `NIRI_SOCKET` into your shell. Pass everything through `niri-agent`.
- One session per task. Reuse it across steps and stop it at the end, including after failures.
- `niri-agent list` shows sessions; a session with `"alive": false` needs `niri-agent stop <name>`.
- The user can watch with `niri-agent show <name>`. Do not call it yourself: it switches the user's view.
- Key names: letters, digits, punctuation, `Return`/`enter`, `Tab`, `Escape`/`esc`, `BackSpace`, `Delete`, `space`, arrows (`Up`, `left`, ...), `Home`, `End`, `pgup`, `pgdn`, `F1`-`F24`, or any XKB keysym name.
