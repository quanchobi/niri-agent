#!/usr/bin/env python3
"""niri-agent: isolated nested niri desktops for AI agents.

Each session is a nested niri compositor whose window lives on its own named
workspace ("agent-<name>") of the host niri. Agents drive the nested session
with a virtual pointer/keyboard and screenshots, so the host desktop's focus and
input are never touched. Apps in a session talk to a private D-Bus session bus,
so portal dialogs and single-instance apps cannot reach the host desktop either.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

REPO = Path(__file__).resolve().parent
CHILD_CONFIG = REPO / "config" / "child.kdl"
IDLE_KDL_TEMPLATE = REPO / "config" / "agents.kdl"

_XDG_STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
STATE_DIR = _XDG_STATE / "niri-agent"
NIRI_CONFIG_DIR = _XDG_CONFIG / "niri"
AGENTS_KDL = Path(os.environ.get("NIRI_AGENT_KDL") or NIRI_CONFIG_DIR / "agents.kdl")

WORKSPACE_PREFIX = "agent-"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SOCKET_RE = re.compile(r"^niri\.(?P<display>.+)\.(?P<pid>\d+)\.sock$")
# Env vars that would let the nested niri window grab focus on the host
# (xdg-activation), or leak the host session into the nested one.
STRIPPED_ENV = ("XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID", "NIRI_SOCKET", "DISPLAY")
# The only services a session's private bus can activate. flatpak-portal backs
# `flatpak-spawn --sandbox` (GNOME runtimes decode icons in a helper started through it),
# and the session helper serves `flatpak run`. xdg-desktop-portal and the document portal
# are left out on purpose: their dialogs would open on the host desktop, and bridging the
# document portal to the host would hand sandboxed apps host-level trust.
PRIVATE_BUS_SERVICES = ("org.freedesktop.portal.Flatpak", "org.freedesktop.Flatpak")
_DBUS_ADDRESS_SAFE = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/.\\*")


class Error(Exception):
    pass


def _json_line(raw: bytes, what: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise Error(f"malformed {what}: {raw[:200]!r}") from e


# --------------------------------------------------------------------------- niri IPC


class Niri:
    """Request/response client for a niri IPC socket."""

    def __init__(self, path: str):
        self.path = path

    def request(self, req):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            try:
                s.connect(self.path)
            except OSError as e:
                raise Error(f"cannot connect to niri socket {self.path}: {e}") from e
            s.sendall(json.dumps(req).encode() + b"\n")
            line = s.makefile("rb").readline()
        reply = _json_line(line, "niri reply")
        if "Err" in reply:
            raise Error(f"niri: {reply['Err']}")
        ok = reply["Ok"]
        return ok if isinstance(ok, str) else next(iter(ok.values()))

    def action(self, action: str, /, **args):
        return self.request({"Action": {action: args}})


class EventStream:
    """niri event stream with timeouts."""

    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.sock.sendall(b'"EventStream"\n')
        self.buf = b""
        first = self.next(5)
        if first is None or "Ok" not in first:
            raise Error(f"niri refused event stream: {first!r}")

    def next(self, timeout: float):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.sock], [], [], remaining)
            if not ready:
                return None
            chunk = self.sock.recv(65536)
            if not chunk:
                raise Error("niri closed the event stream")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return _json_line(line, "niri event")

    def drain(self, quiet: float = 0.3, limit: float = 3.0):
        """Consume the initial state burst so later waits only see new events."""
        end = time.monotonic() + limit
        while time.monotonic() < end and self.next(quiet) is not None:
            pass

    def close(self):
        self.sock.close()


def host_niri() -> Niri:
    path = os.environ.get("NIRI_SOCKET")
    if not path:
        raise Error("NIRI_SOCKET is not set; run inside a niri session")
    return Niri(path)


def host_wayland_display(host: Niri) -> str:
    m = SOCKET_RE.match(Path(host.path).name)
    if m:
        return m["display"]
    display = os.environ.get("WAYLAND_DISPLAY")
    if not display:
        raise Error("cannot determine the host WAYLAND_DISPLAY")
    return display


def runtime_dir() -> Path:
    d = os.environ.get("XDG_RUNTIME_DIR")
    if not d:
        raise Error("XDG_RUNTIME_DIR is not set")
    return Path(d)


# --------------------------------------------------------------------------- Wayland wire client


def u32(v: int) -> bytes:
    return struct.pack("=I", v)


def i32(v: int) -> bytes:
    return struct.pack("=i", v)


def wl_fixed(v: float) -> bytes:
    return i32(round(v * 256))


def wl_string(s: str) -> bytes:
    data = s.encode() + b"\0"
    return u32(len(data)) + data + b"\0" * (-len(data) % 4)


def unpack_string(body: bytes, off: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("=I", body, off)
    s = body[off + 4 : off + 4 + n - 1].decode()
    return s, off + 4 + n + (-n % 4)


class Wayland:
    """Minimal Wayland client: registry, bind, requests, roundtrip. No event dispatch beyond that."""

    DISPLAY_ID = 1

    def __init__(self, display: str):
        path = display if display.startswith("/") else str(runtime_dir() / display)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(path)
        except OSError as e:
            raise Error(f"cannot connect to Wayland display {path}: {e}") from e
        self.buf = b""
        self._next_id = 2
        self.globals: dict[str, tuple[int, int]] = {}
        self.registry = self.new_id()
        self.send(self.DISPLAY_ID, 1, u32(self.registry))  # wl_display.get_registry
        self.roundtrip()

    def new_id(self) -> int:
        oid = self._next_id
        self._next_id += 1
        return oid

    def send(self, obj: int, opcode: int, payload: bytes = b"", fds: tuple[int, ...] = ()):
        msg = struct.pack("=II", obj, ((8 + len(payload)) << 16) | opcode) + payload
        if fds:
            socket.send_fds(self.sock, [msg], list(fds))
        else:
            self.sock.sendall(msg)

    def _read_message(self) -> tuple[int, int, bytes]:
        while True:
            if len(self.buf) >= 8:
                obj, word = struct.unpack_from("=II", self.buf)
                size = word >> 16
                if len(self.buf) >= size:
                    body = self.buf[8:size]
                    self.buf = self.buf[size:]
                    return obj, word & 0xFFFF, body
            chunk = self.sock.recv(65536)
            if not chunk:
                raise Error("Wayland connection closed by compositor")
            self.buf += chunk

    def roundtrip(self):
        callback = self.new_id()
        self.send(self.DISPLAY_ID, 0, u32(callback))  # wl_display.sync
        while True:
            obj, opcode, body = self._read_message()
            if obj == self.DISPLAY_ID and opcode == 0:  # wl_display.error
                oid, code = struct.unpack_from("=II", body)
                message, _ = unpack_string(body, 8)
                raise Error(f"Wayland protocol error on object {oid} (code {code}): {message}")
            if obj == self.registry and opcode == 0:  # wl_registry.global
                (name,) = struct.unpack_from("=I", body)
                interface, off = unpack_string(body, 4)
                (version,) = struct.unpack_from("=I", body, off)
                self.globals[interface] = (name, version)
            elif obj == callback and opcode == 0:  # wl_callback.done
                return

    def bind(self, interface: str, version: int) -> tuple[int, int]:
        if interface not in self.globals:
            raise Error(f"compositor does not support {interface}")
        name, advertised = self.globals[interface]
        version = min(version, advertised)
        oid = self.new_id()
        self.send(self.registry, 0, u32(name) + wl_string(interface) + u32(version) + u32(oid))
        return oid, version

    def close(self):
        self.sock.close()


def now_ms() -> int:
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}  # linux/input-event-codes.h


class VirtualPointer:
    """zwlr_virtual_pointer_v1. Coordinates are pixels of the nested output (== screenshot pixels)."""

    def __init__(self, wl: Wayland, width: int, height: int):
        self.wl, self.width, self.height = wl, width, height
        seat, _ = wl.bind("wl_seat", 1)
        manager, self.version = wl.bind("zwlr_virtual_pointer_manager_v1", 2)
        self.id = wl.new_id()
        wl.send(manager, 0, u32(seat) + u32(self.id))  # create_virtual_pointer

    def _frame(self):
        self.wl.send(self.id, 4)

    def move(self, x: float, y: float):
        x = min(max(round(x), 0), self.width - 1)
        y = min(max(round(y), 0), self.height - 1)
        self.wl.send(self.id, 1, struct.pack("=IIIII", now_ms(), x, y, self.width, self.height))
        self._frame()

    def button(self, button: int, pressed: bool):
        self.wl.send(self.id, 2, struct.pack("=III", now_ms(), button, 1 if pressed else 0))
        self._frame()

    def scroll(self, notches: int, horizontal: bool):
        axis = 1 if horizontal else 0
        step = 1 if notches > 0 else -1
        for _ in range(abs(notches)):
            self.wl.send(self.id, 5, u32(0))  # axis_source: wheel
            if self.version >= 2:
                self.wl.send(self.id, 7, struct.pack("=II", now_ms(), axis) + wl_fixed(15 * step) + i32(step))
            else:
                self.wl.send(self.id, 3, struct.pack("=II", now_ms(), axis) + wl_fixed(15 * step))
            self._frame()
            time.sleep(0.02)

    def destroy(self):
        self.wl.send(self.id, 8)


# name -> (keysym, modifier mask, xkb real modifier)
MODIFIERS = {
    "shift": ("Shift_L", 1, "Shift"),
    "ctrl": ("Control_L", 4, "Control"),
    "alt": ("Alt_L", 8, "Mod1"),
    "super": ("Super_L", 64, "Mod4"),
}
MODIFIER_ALIASES = {"control": "ctrl", "meta": "super", "win": "super", "logo": "super", "mod": "super"}
KEY_ALIASES = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab",
    "space": "space", "backspace": "BackSpace", "delete": "Delete", "del": "Delete",
    "insert": "Insert", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "Prior", "pgup": "Prior", "pagedown": "Next", "pgdn": "Next",
}


def char_keysym(c: str) -> str:
    if c == "\n":
        return "Return"
    if c == "\t":
        return "Tab"
    if c.isascii() and c.isalnum():
        return c
    return f"U{ord(c):04X}"


def key_keysym(token: str) -> str:
    alias = KEY_ALIASES.get(token.lower())
    if alias:
        return alias
    if len(token) == 1:
        return char_keysym(token.lower() if token.isalpha() else token)
    if re.fullmatch(r"[fF]\d{1,2}", token):
        return token.upper()
    return token  # raw xkb keysym name, e.g. XF86AudioMute


def parse_combo(combo: str) -> tuple[list[str], str]:
    """'ctrl+shift+t' -> (['ctrl', 'shift'], 't'). A trailing '+' key is written 'ctrl++'."""
    if combo == "+":
        return [], key_keysym("+")
    if combo.endswith("++"):
        mod_part, key = combo[:-2], "+"
        parts = mod_part.split("+") if mod_part else []
    else:
        *parts, key = combo.split("+")
    if not key:
        raise Error(f"invalid key combo {combo!r}")
    mods = []
    for p in parts:
        m = MODIFIER_ALIASES.get(p.lower(), p.lower())
        if m not in MODIFIERS:
            raise Error(f"unknown modifier {p!r} in {combo!r} (use shift, ctrl, alt, super)")
        mods.append(m)
    return mods, key_keysym(key)


def build_keymap(keysyms: list[str]) -> tuple[str, dict[str, int]]:
    """XKB keymap with one single-level key per keysym, plus modifier keys. Returns (text, keysym -> evdev code)."""
    syms = list(dict.fromkeys([m[0] for m in MODIFIERS.values()] + keysyms))
    codes = {s: i + 1 for i, s in enumerate(syms)}  # evdev code; xkb keycode = evdev + 8
    # GTK3's Wayland keyval->keycode lookup skips the keymap's max keycode (`keycode < max_keycode`),
    # which breaks key bindings (BackSpace, Escape, arrows, ctrl+a) for a keysym placed there.
    # xkbcommon derives max_keycode from declared keycodes, so declare one unused keycode past the last.
    pad = len(syms) + 9
    lines = ["xkb_keymap {", 'xkb_keycodes "(unnamed)" {', "minimum = 8;", f"maximum = {pad};"]
    lines += [f"<K{c}> = {c + 8};" for c in codes.values()] + [f"<KPAD> = {pad};"]
    lines += ["};", 'xkb_types "(unnamed)" { include "complete" };',
              'xkb_compatibility "(unnamed)" { include "complete" };', 'xkb_symbols "(unnamed)" {']
    lines += [f"key <K{c}> {{[ {s} ]}};" for s, c in codes.items()]
    lines += [f"modifier_map {real} {{ <K{codes[sym]}> }};" for sym, _, real in MODIFIERS.values()]
    lines += ["};", "};"]
    return "\n".join(lines) + "\n", codes


class VirtualKeyboard:
    """zwp_virtual_keyboard_v1 with a generated keymap covering exactly the keysyms needed."""

    def __init__(self, wl: Wayland, keysyms: list[str], delay: float):
        self.wl, self.delay = wl, delay
        seat, _ = wl.bind("wl_seat", 1)
        manager, _ = wl.bind("zwp_virtual_keyboard_manager_v1", 1)
        self.id = wl.new_id()
        wl.send(manager, 0, u32(seat) + u32(self.id))  # create_virtual_keyboard
        text, self.codes = build_keymap(keysyms)
        data = text.encode() + b"\0"
        fd = os.memfd_create("niri-agent-keymap", os.MFD_CLOEXEC)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            wl.send(self.id, 0, u32(1) + u32(len(data)), fds=(fd,))  # keymap, XKB_V1
        finally:
            os.close(fd)
        wl.roundtrip()

    def key(self, keysym: str, pressed: bool):
        self.wl.send(self.id, 1, struct.pack("=III", now_ms(), self.codes[keysym], 1 if pressed else 0))

    def modifiers(self, mask: int):
        self.wl.send(self.id, 2, struct.pack("=IIII", mask, 0, 0, 0))

    def tap(self, keysym: str):
        self.key(keysym, True)
        time.sleep(self.delay)
        self.key(keysym, False)
        time.sleep(self.delay)

    def combo(self, mods: list[str], keysym: str):
        mask = 0
        for m in mods:
            sym, bit, _ = MODIFIERS[m]
            self.key(sym, True)
            mask |= bit
        if mods:
            self.modifiers(mask)
            time.sleep(self.delay)
        self.tap(keysym)
        for m in reversed(mods):
            self.key(MODIFIERS[m][0], False)
        if mods:
            self.modifiers(0)
            time.sleep(self.delay)

    def destroy(self):
        self.wl.send(self.id, 3)


# --------------------------------------------------------------------------- sessions


def sessions_dir() -> Path:
    return STATE_DIR / "sessions"


def session_path(name: str) -> Path:
    return sessions_dir() / name


def load_session(name: str) -> dict:
    f = session_path(name) / "session.json"
    try:
        raw = f.read_bytes()
    except FileNotFoundError:
        raise Error(f"no session named {name!r} (see: niri-agent list)") from None
    return _json_line(raw, f"session file {f}")


def all_sessions() -> list[dict]:
    if not sessions_dir().is_dir():
        return []
    out = []
    for d in sorted(sessions_dir().iterdir()):
        if (d / "session.json").is_file():
            out.append(load_session(d.name))
    return out


def pid_alive(pid: int, comm: str = "niri") -> bool:
    """True if pid is a running (not zombie) process named `comm`."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # Format: "pid (comm) state ...". comm may contain spaces or parens, so split on the last ")".
    head, _, rest = stat.rpartition(")")
    actual = head.partition("(")[2]
    # A zombie keeps its comm until the parent reaps it, so it must not count as alive.
    return actual == comm and rest.split()[:1] != ["Z"]


def live_session(name: str) -> dict:
    s = load_session(name)
    if not pid_alive(s["pid"]):
        raise Error(f"session {name!r} is dead; clean it up with: niri-agent stop {name}")
    return s


@contextlib.contextmanager
def state_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def idle_kdl() -> str:
    return IDLE_KDL_TEMPLATE.read_text()


def write_agents_kdl(text: str):
    target = AGENTS_KDL.resolve()
    tmp = target.with_name(f".{target.name}.niri-agent-tmp")
    tmp.write_text(text)
    os.replace(tmp, target)


def launch_rule(workspace: str, width: int, height: int) -> str:
    # A nested niri window always has app-id and title "niri"; this rule only exists
    # for the few seconds between writing it and the nested window mapping.
    return f"""
window-rule {{
    match app-id="^niri$" title="^niri$"
    open-on-workspace "{workspace}"
    open-focused false
    open-floating true
    default-column-width {{ fixed {width}; }}
    default-window-height {{ fixed {height}; }}
}}
"""


def check_installed():
    if not AGENTS_KDL.exists():
        raise Error(f"{AGENTS_KDL} does not exist; run install.sh")
    include = re.compile(r'^\s*include\s+"[^"]*agents\.kdl"', re.M)
    for f in NIRI_CONFIG_DIR.rglob("*.kdl"):
        with contextlib.suppress(OSError, UnicodeDecodeError):
            if include.search(f.read_text()):
                return
    raise Error(f'no `include "agents.kdl"` found under {NIRI_CONFIG_DIR}; run install.sh')


def child_env(host: Niri, bus_address: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    env["WAYLAND_DISPLAY"] = host_wayland_display(host)
    # The host throttles frame callbacks for windows on hidden workspaces to ~1/s. With a
    # blocking eglSwapBuffers the nested niri's event loop stalls for that long after every
    # redraw, delaying input and IPC. Mesa's Wayland EGL maps vblank_mode=0 to swap interval 0.
    env["vblank_mode"] = "0"
    # Everything the nested niri spawns inherits this. A non-session niri exports no D-Bus
    # interfaces itself, so the bus may come up after niri does.
    env["DBUS_SESSION_BUS_ADDRESS"] = bus_address
    return env


# --------------------------------------------------------------------------- private session bus


def dbus_unix_address(path: Path) -> str:
    raw = os.fsencode(path)
    return "unix:path=" + "".join(chr(b) if b in _DBUS_ADDRESS_SAFE else f"%{b:02x}" for b in raw)


def host_bus_address() -> str | None:
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if addr:
        return addr
    default = runtime_dir() / "bus"  # where GDBus/sd-bus look when the variable is unset
    return dbus_unix_address(default) if default.is_socket() else None


def dbus_service_dirs() -> list[Path]:
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [Path(d) / "dbus-1" / "services" for d in [str(data_home), *data_dirs.split(":")] if d]


def write_private_services(dest: Path, search: list[Path]):
    """Activation files for PRIVATE_BUS_SERVICES only, reduced to Name and Exec.

    Dropping SystemdService makes the private daemon exec the service itself instead of
    asking the host's systemd user manager, which would start it on the host bus."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in PRIVATE_BUS_SERVICES:
        for d in search:
            try:
                lines = (d / f"{name}.service").read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            keys = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
            if keys.get("Name") == name and keys.get("Exec"):
                (dest / f"{name}.service").write_text(f"[D-BUS Service]\nName={name}\nExec={keys['Exec']}\n")
                break


def bus_config(address: str, services: Path) -> str:
    return f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>{xml_escape(address)}</listen>
  <auth>EXTERNAL</auth>
  <servicedir>{xml_escape(str(services))}</servicedir>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def start_bus(sdir: Path, address: str, env: dict) -> subprocess.Popen:
    """Run the session's private dbus-daemon; returns once it accepts connections."""
    services = sdir / "dbus-services"
    write_private_services(services, dbus_service_dirs())
    (sdir / "dbus.conf").write_text(bus_config(address, services))
    with open(sdir / "dbus.log", "wb") as log:
        try:
            proc = subprocess.Popen(
                ["dbus-daemon", "--nofork", f"--config-file={sdir / 'dbus.conf'}", "--print-address=1"],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, start_new_session=True,
            )
        except FileNotFoundError:
            raise Error("dbus-daemon not found; install the dbus package") from None
    try:
        # The address is printed once the daemon is listening.
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        line = proc.stdout.readline() if ready else b""
        proc.stdout.close()
        if not line.strip():
            raise Error(f"private dbus-daemon did not start; see {sdir / 'dbus.log'}")
    except BaseException:
        stop_child(proc)
        raise
    return proc


def find_nested_socket(pid: int, timeout: float) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for p in runtime_dir().glob(f"niri.*.{pid}.sock"):
            m = SOCKET_RE.match(p.name)
            if m:
                return str(p), m["display"]
        time.sleep(0.05)
    raise Error(f"nested niri (pid {pid}) did not create its IPC socket")


def terminate(pid: int, comm: str = "niri"):
    """Stop a session process by pid (used by `stop`, which has no Popen handle)."""
    if not pid_alive(pid, comm):
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not pid_alive(pid, comm):
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def stop_child(proc: subprocess.Popen):
    """Stop a nested niri this process spawned, and reap it so no zombie is left."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def unname_workspace(host: Niri, workspace: str):
    if any(w["name"] == workspace for w in host.request("Workspaces")):
        host.action("UnsetWorkspaceName", reference={"Name": workspace})


def pick_workspace(host: Niri, output: str) -> dict:
    """The bottom empty workspace of `output`: niri always keeps one, so naming it never shifts user workspaces."""
    occupied = {w["workspace_id"] for w in host.request("Windows")}
    candidates = [
        w for w in host.request("Workspaces")
        if w["output"] == output and w["name"] is None and w["id"] not in occupied
    ]
    if not candidates:
        raise Error(f"no empty workspace on output {output!r}")
    return max(candidates, key=lambda w: w["idx"])


def nested_output(nested: Niri) -> dict:
    outputs = nested.request("Outputs")
    if not outputs:
        raise Error("nested niri has no output")
    out = next(iter(outputs.values()))
    if out.get("current_mode") is not None:
        mode = out["modes"][out["current_mode"]]
        width, height = mode["width"], mode["height"]
    else:
        lg = out["logical"]
        width, height = round(lg["width"] * lg["scale"]), round(lg["height"] * lg["scale"])
    return {"width": width, "height": height, "scale": out["logical"]["scale"]}


def wait_for_nested(nested: Niri, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return nested_output(nested)
        except Error:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def start_session(name: str | None, output: str | None, width: int, height: int) -> dict:
    host = host_niri()
    check_installed()
    with state_lock():
        if name is None:
            taken = {s["name"] for s in all_sessions()}
            name = next(f"a{i}" for i in range(1, 1000) if f"a{i}" not in taken)
        if not NAME_RE.match(name):
            raise Error(f"invalid session name {name!r} (lowercase letters, digits, dashes)")
        sdir = session_path(name)
        if sdir.exists():
            raise Error(f"session {name!r} already exists (stop it first)")
        workspace = WORKSPACE_PREFIX + name
        if any(w["name"] == workspace for w in host.request("Workspaces")):
            raise Error(f"host already has a workspace named {workspace!r}")
        if output is None:
            focused = host.request("FocusedOutput")
            if not focused:
                raise Error("no focused output; pass --output")
            output = focused["name"]
        target = pick_workspace(host, output)
        bus_socket = runtime_dir() / f"niri-agent-{name}.bus"
        bus_address = dbus_unix_address(bus_socket)
        host_bus = host_bus_address()

        # Everything from naming the workspace until session.json exists is one transaction:
        # on any failure (including Ctrl-C) the nested niri, its bus, the workspace name and
        # the session dir are rolled back. Without session.json, `stop` cannot find the session.
        proc = bus = None
        try:
            events = EventStream(host.path)
            try:
                host.action("SetWorkspaceName", name=workspace, workspace={"Id": target["id"]})
                events.drain()
                write_agents_kdl(idle_kdl() + launch_rule(workspace, width, height))
                while True:
                    ev = events.next(10)
                    if ev is None:
                        raise Error(f"niri did not reload {AGENTS_KDL}; is it included from config.kdl?")
                    if "ConfigLoaded" in ev:
                        if ev["ConfigLoaded"]["failed"]:
                            raise Error("niri failed to load its config after writing agents.kdl; check `niri validate`")
                        break

                sdir.mkdir(parents=True)
                with open(sdir / "niri.log", "wb") as log:
                    proc = subprocess.Popen(
                        ["niri", "-c", str(CHILD_CONFIG)],
                        env=child_env(host, bus_address), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                window = None
                deadline = time.monotonic() + 15
                while window is None:
                    if proc.poll() is not None:
                        raise Error(f"nested niri exited with {proc.returncode}; see {sdir / 'niri.log'}")
                    if time.monotonic() > deadline:
                        raise Error("nested niri window did not appear on the host")
                    ev = events.next(0.25)
                    w = (ev or {}).get("WindowOpenedOrChanged", {}).get("window")
                    if w and w["pid"] == proc.pid:
                        window = w
            finally:
                # Drop the launch rule as soon as the window has mapped (or we gave up),
                # so it cannot catch unrelated nested niris during the slower steps below.
                events.close()
                write_agents_kdl(idle_kdl())

            if window["workspace_id"] != target["id"]:
                host.action("MoveWindowToWorkspace", window_id=window["id"], reference={"Id": target["id"]}, focus=False)
            niri_socket, display = find_nested_socket(proc.pid, 10)
            # Services the bus activates (e.g. sandboxed flatpak helpers) belong to the session.
            bus_env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
            bus_env.update(WAYLAND_DISPLAY=display, DBUS_SESSION_BUS_ADDRESS=bus_address)
            bus_socket.unlink(missing_ok=True)
            bus = start_bus(sdir, bus_address, bus_env)
            screen = wait_for_nested(Niri(niri_socket), 10)
            session = {
                "name": name,
                "pid": proc.pid,
                "workspace": workspace,
                "output": output,
                "host_window_id": window["id"],
                "wayland_display": display,
                "niri_socket": niri_socket,
                "screen": screen,
                "bus_pid": bus.pid,
                "bus_socket": str(bus_socket),
                "host_bus": host_bus,
                "dir": str(sdir),
                "created": time.time(),
            }
            (sdir / "session.json").write_text(json.dumps(session, indent=2))
        except BaseException:
            if proc is not None:
                stop_child(proc)
            if bus is not None:
                stop_child(bus)
                bus_socket.unlink(missing_ok=True)
            with contextlib.suppress(Error, OSError):
                unname_workspace(host, workspace)
            shutil.rmtree(sdir, ignore_errors=True)
            raise
        return session


def stop_session(host: Niri | None, s: dict):
    terminate(s["pid"])
    if "bus_pid" in s:  # absent for sessions started before private buses existed
        terminate(s["bus_pid"], "dbus-daemon")
        Path(s["bus_socket"]).unlink(missing_ok=True)
    if host is not None:
        with contextlib.suppress(Error):
            unname_workspace(host, s["workspace"])
    shutil.rmtree(session_path(s["name"]), ignore_errors=True)


def screenshot(s: dict, out: str | None, pointer: bool) -> dict:
    nested = Niri(s["niri_socket"])
    if out:
        path = Path(out).expanduser().resolve()
    else:
        path = Path(s["dir"]) / "shots" / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.monotonic_ns() % 10**6:06d}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    nested.action("ScreenshotScreen", write_to_disk=True, show_pointer=pointer, path=str(path))
    # The PNG is encoded off-thread; wait for a complete file (IEND chunk at the end).
    deadline = time.monotonic() + 10
    while True:
        with contextlib.suppress(FileNotFoundError):
            data = path.read_bytes()
            if data.endswith(b"IEND\xaeB`\x82"):
                break
        if time.monotonic() > deadline:
            raise Error(f"screenshot was not written to {path}")
        time.sleep(0.03)
    width, height = struct.unpack(">II", data[16:24])
    return {"path": str(path), "width": width, "height": height}


# --------------------------------------------------------------------------- CLI


def emit(obj):
    print(json.dumps(obj, indent=2))


def session_summary(s: dict) -> dict:
    return {
        "name": s["name"],
        "alive": pid_alive(s["pid"]),
        "workspace": s["workspace"],
        "output": s["output"],
        "screen": s["screen"],
        "wayland_display": s["wayland_display"],
        "niri_socket": s["niri_socket"],
    }


def parse_size(v: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)x(\d+)", v)
    if not m or int(m[1]) < 200 or int(m[2]) < 200:
        raise argparse.ArgumentTypeError("size must be WIDTHxHEIGHT, each >= 200")
    return int(m[1]), int(m[2])


def cmd_start(a):
    s = start_session(a.name, a.output, *a.size)
    emit(session_summary(s))


def cmd_stop(a):
    if not a.all and not a.name:
        raise Error("give a session name or --all")
    try:
        host = host_niri()
    except Error:
        host = None
    targets = all_sessions() if a.all else [load_session(a.name)]
    stopped, failed = [], []
    for s in targets:
        try:
            stop_session(host, s)
        except (Error, OSError) as e:
            failed.append(f"{s['name']}: {e}")
        else:
            stopped.append(s["name"])
    emit({"stopped": stopped})
    if failed:
        raise Error("could not stop " + "; ".join(failed))


def cmd_list(a):
    emit([session_summary(s) for s in all_sessions()])


def cmd_show(a):
    s = load_session(a.name)
    host_niri().action("FocusWorkspace", reference={"Name": s["workspace"]})


def cmd_menu(a):
    names = [s["name"] for s in all_sessions() if pid_alive(s["pid"])]
    if not names:
        raise Error("no running agent sessions")
    try:
        r = subprocess.run(shlex.split(a.dmenu), input="\n".join(names) + "\n", capture_output=True, text=True)
    except FileNotFoundError as e:
        raise Error(f"launcher not found: {e.filename} (install it or pass --dmenu)") from None
    choice = r.stdout.strip()
    if choice in names:
        host_niri().action("FocusWorkspace", reference={"Name": WORKSPACE_PREFIX + choice})


def cmd_run(a):
    if not a.command:
        raise Error("missing command: niri-agent run NAME -- COMMAND [ARGS...]")
    if a.command[0].startswith("-"):
        raise Error(f"{a.command[0]!r} is not a command; options go before the name: niri-agent run --host-bus NAME -- COMMAND")
    s = live_session(a.name)
    command = a.command
    if a.host_bus:
        if not s.get("host_bus"):
            raise Error(f"session {a.name!r} has no recorded host session bus; --host-bus is unavailable")
        command = ["env", f"DBUS_SESSION_BUS_ADDRESS={s['host_bus']}", *command]
    Niri(s["niri_socket"]).action("Spawn", command=command)
    emit({"spawned": a.command, "bus": "host" if a.host_bus else "private"})


def cmd_msg(a):
    s = live_session(a.name)
    env = {**os.environ, "NIRI_SOCKET": s["niri_socket"]}
    sys.exit(subprocess.run(["niri", "msg", *a.args], env=env).returncode)


def cmd_screenshot(a):
    emit(screenshot(live_session(a.name), a.out, a.pointer))


@contextlib.contextmanager
def pointer(name: str):
    s = live_session(name)
    screen = nested_output(Niri(s["niri_socket"]))
    wl = Wayland(s["wayland_display"])
    try:
        vp = VirtualPointer(wl, screen["width"], screen["height"])
        yield vp
        vp.destroy()
        wl.roundtrip()
    finally:
        wl.close()


def cmd_move(a):
    with pointer(a.name) as vp:
        vp.move(a.x, a.y)


def cmd_click(a):
    button = BUTTONS[a.button]
    with pointer(a.name) as vp:
        vp.move(a.x, a.y)
        time.sleep(0.05)
        for i in range(2 if a.double else 1):
            vp.button(button, True)
            time.sleep(0.03)
            vp.button(button, False)
            time.sleep(0.06)


def cmd_drag(a):
    with pointer(a.name) as vp:
        vp.move(a.x1, a.y1)
        time.sleep(0.05)
        vp.button(BUTTONS["left"], True)
        steps = 20
        for i in range(1, steps + 1):
            vp.move(a.x1 + (a.x2 - a.x1) * i / steps, a.y1 + (a.y2 - a.y1) * i / steps)
            time.sleep(0.015)
        vp.button(BUTTONS["left"], False)


def cmd_scroll(a):
    with pointer(a.name) as vp:
        vp.move(a.x, a.y)
        time.sleep(0.05)
        vp.scroll(a.notches, a.horizontal)


def run_keyboard(name: str, keysyms: list[str], delay_ms: int, fn):
    s = live_session(name)
    wl = Wayland(s["wayland_display"])
    try:
        kb = VirtualKeyboard(wl, keysyms, delay_ms / 1000)
        fn(kb)
        kb.destroy()
        wl.roundtrip()
    finally:
        wl.close()


def cmd_type(a):
    text = sys.stdin.read() if a.text == "-" else a.text
    syms = [char_keysym(c) for c in text]

    def go(kb):
        for sym in syms:
            kb.tap(sym)

    run_keyboard(a.name, syms, a.delay_ms, go)


def cmd_key(a):
    combos = [parse_combo(c) for c in a.combos]

    def go(kb):
        for mods, sym in combos:
            kb.combo(mods, sym)

    run_keyboard(a.name, [sym for _, sym in combos], a.delay_ms, go)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="niri-agent", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start", help="start a nested niri session on its own host workspace")
    sp.add_argument("name", nargs="?", help="session name (default: a1, a2, ...)")
    sp.add_argument("--size", type=parse_size, default=(1280, 800), help="WIDTHxHEIGHT in host logical px (default 1280x800)")
    sp.add_argument("--output", help="host output for the workspace (default: focused output)")
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("stop", help="stop a session (closes all its apps) and remove its workspace")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(fn=cmd_stop)

    sub.add_parser("list", help="list sessions").set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="switch the host view to a session's workspace")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("menu", help="pick a session with a dmenu-style launcher and show it")
    sp.add_argument("--dmenu", default="fuzzel --dmenu --prompt 'agent> '", help="launcher command reading choices on stdin")
    sp.set_defaults(fn=cmd_menu)

    sp = sub.add_parser("run", help="launch an app inside the session")
    sp.add_argument("--host-bus", action="store_true",
                    help="connect the app to your session D-Bus instead of the session's private bus "
                         "(keyring, dconf writes; portal dialogs may then open on your desktop)")
    sp.add_argument("name")
    sp.add_argument("command", nargs=argparse.REMAINDER)
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("msg", help="run `niri msg ...` against the nested niri")
    sp.add_argument("name")
    sp.add_argument("args", nargs=argparse.REMAINDER)
    sp.set_defaults(fn=cmd_msg)

    sp = sub.add_parser("screenshot", help="screenshot the session screen; prints path and pixel size")
    sp.add_argument("name")
    sp.add_argument("--out", help="output PNG path (default: session dir)")
    sp.add_argument("--pointer", action="store_true", help="include the pointer")
    sp.set_defaults(fn=cmd_screenshot)

    for cmd, helptext in (("move", "move the pointer"), ("click", "click at screenshot pixel X Y")):
        sp = sub.add_parser(cmd, help=helptext)
        sp.add_argument("name")
        sp.add_argument("x", type=float)
        sp.add_argument("y", type=float)
        if cmd == "click":
            sp.add_argument("--button", choices=sorted(BUTTONS), default="left")
            sp.add_argument("--double", action="store_true")
        sp.set_defaults(fn=cmd_move if cmd == "move" else cmd_click)

    sp = sub.add_parser("drag", help="left-drag from X1 Y1 to X2 Y2")
    sp.add_argument("name")
    for c in ("x1", "y1", "x2", "y2"):
        sp.add_argument(c, type=float)
    sp.set_defaults(fn=cmd_drag)

    sp = sub.add_parser("scroll", help="scroll wheel notches at X Y (positive = down/right)")
    sp.add_argument("name")
    sp.add_argument("x", type=float)
    sp.add_argument("y", type=float)
    sp.add_argument("notches", type=int)
    sp.add_argument("--horizontal", action="store_true")
    sp.set_defaults(fn=cmd_scroll)

    sp = sub.add_parser("type", help="type text into the focused window ('-' reads stdin)")
    sp.add_argument("name")
    sp.add_argument("text")
    sp.add_argument("--delay-ms", type=int, default=5)
    sp.set_defaults(fn=cmd_type)

    sp = sub.add_parser("key", help="press key combos in order, e.g. ctrl+l Return")
    sp.add_argument("name")
    sp.add_argument("combos", nargs="+")
    sp.add_argument("--delay-ms", type=int, default=20)
    sp.set_defaults(fn=cmd_key)
    return p


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    for attr in ("command", "args"):
        rest = getattr(args, attr, None)
        if rest and rest[0] == "--":
            setattr(args, attr, rest[1:])
    try:
        args.fn(args)
    except Error as e:
        print(f"niri-agent: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
