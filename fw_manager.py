#!/usr/bin/env python3
"""
fw_manager.py - Network firmware manager for Arduino / ESP / any HTTP device.

What it does
------------
* Keeps a list of saved devices (name + host + version endpoint).
* Scans all devices over HTTP, reads the reported firmware info
  (expected JSON: {"id":"LED_WS2811.ino","build":20261029041512,
  "md5":"<32 hex>","chip":"esp8266"} - md5 and chip are optional, on
  ESP8266/ESP32 use ESP.getSketchMD5() and the chip family string).
* Shows firmware id, build, and an update status per device:
    UP TO DATE | UPDATE AVAILABLE | NEWER ON DEVICE | NO BINARY LINKED | UNKNOWN FW
* Keeps a firmware registry: one entry per firmware id and chip, each with a
  binary file + upload command template + (optional) manual build number.
  An entry with an empty chip matches any chip; a chip specific entry wins.
* Per-device actions: Check (re-read version), Update (run the upload
  command template), Install firmware (pick any registry entry for the
  device's chip and flash it), Link binary (create/edit the registry entry).
* Send bar: type text, press Enter; it goes to the selected device(s) and the
  reply shows in the log. HTTP: "/path?x=1" = GET as typed, other text =
  GET <device "Command path" field, default /cmd?q=><text>.
  TCP: raw text + newline to the device port.
* Devices can be disabled (Enable/Disable); disabled devices are skipped by
  Scan all and Update all outdated, but can still be checked by hand.
* Everything is persisted to fw_manager.json next to this script.

Variables and upload commands (fw_manager.properties)
-----------------------------------------------------
One properties file next to this script (created on first start, re-read on
every install, open it via Settings > Open variables file):
  name=value        any number of your own variables, used as {name}
  cmd.<id>=...      an upload command; pick it by <id> in the Firmware tab
Lines starting with # are comments. Variables may reference other variables,
built-ins and environment variables (%NAME%). A path value with * uses the
newest match (e.g. a core folder whose version changes).
Built-in placeholders:
  {host}  device host/IP        {port}  device port (default 80)
  {name}  device name           {id}    firmware id being installed
  {file}  path of linked binary {build} build number of the binary
  {chip}  chip family of the device
A firmware "Upload command" value that is not a known id is used as a raw template.

MD5 check (when the device reports "md5"):
  md5 equal to the md5 of the linked binary  -> UP TO DATE (build ignored)
  md5 different                              -> UPDATE AVAILABLE (or NEWER ON
                                                DEVICE if the device build is higher)
  no md5 reported                            -> build numbers decide, as before

Columns: right click a column header to show/hide columns (saved in the config).

UI state: window geometry, maximized state, log pane height and
column widths are saved to the config ("ui" key) on exit and restored on start.

Colors: every color the program uses is in the config under "colors", in four
groups: "log" (log line text), "status" (device row background per status),
"table" (device row text) and "selection" (selected firmware row). Values are
#rrggbb or a Tk color name. The code is in fw_manager_colors.py and the dialog
descriptions are in lang/<code>.json (see fw_manager_lang.py; Language menu). Edit them with Settings > Colors... or by hand while
the program is closed; they are used the next time the program starts. A
missing or invalid entry falls back to the built-in default.

Build number of a binary (first match wins):
  1. manual "build" field in the registry entry
  2. sidecar file "<binary>.json" containing {"build": ...}
  3. a 14 digit timestamp (YYYYMMDDHHMMSS) in the file name
  4. the file modification time formatted as YYYYMMDDHHMMSS

Requirements: Python 3.8+, tkinter (bundled with the standard installers).
"""

import glob
import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib import request, error, parse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont

from fw_manager_colors import (
    ST_OK, ST_UPD, ST_NEWER, ST_NOBIN, ST_UNKNOWN, ST_BUSY, ST_DISABLED,
    ST_UNCHECKED, STATUS_TEXT, STATUS_ID, DEFAULT_COLORS, ColorsDialog,
    seed_colors, resolve_color, current_colors, refresh_status_text)
from fw_manager_lang import t, list_languages, resolve_language, set_language

# --------------------------------------------------------------------------- #
# Constants / persistence
# --------------------------------------------------------------------------- #

# Shown in the window title.
VERSION = "1.2.0"

# Config file lives next to the script so the tool is portable.
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fw_manager.json")

# Seconds to wait for a device to answer a version request.
HTTP_TIMEOUT = 3.0

# Variables + upload command presets live in one properties file.
PROPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fw_manager.properties")

# Upload command id used when a registry entry has none.
DEFAULT_CMD = "curl"

# Presets that always exist (the properties file can override them).
BUILTIN_PRESETS = {
    "curl": 'curl -sS -F "file=@{file}" http://{host}:{port}/update',
}

SEED_PROPS = r"""# Firmware Manager variables and upload commands.
# name=value       variable, use it as {name} in values and commands
# cmd.<id>=...     upload command, selected by <id> in the Firmware tab
# Built-ins: {host} {port} {name} {id} {file} {build}
# Environment variables like %LOCALAPPDATA% are expanded in variable values.

# A path with * uses the newest match.
# OTA upload commands: pick espota8266 or espota32 in the Firmware tab
# (Upload command id).
espota8266=%LOCALAPPDATA%\Arduino15\packages\esp8266\hardware\esp8266\*\tools\espota.py
espota32=%LOCALAPPDATA%\Arduino15\packages\esp32\hardware\esp32\*\tools\espota.py

cmd.espota8266=python "{espota8266}" -i {host} -p 8266 -f "{file}"
cmd.espota32=python "{espota32}" -i {host} -p 3232 -f "{file}"

cmd.curl=curl -sS -F "file=@{file}" http://{host}:{port}/update
"""

def hidden_process_kwargs():
    """
    Extra Popen arguments that stop Windows from flashing/showing a console
    window for child processes (curl, espota, arduino-cli, cmd.exe ...).
    Returns an empty dict on non-Windows platforms.
    """
    if os.name != "nt":
        return {}
    # STARTUPINFO with SW_HIDE hides the window of the launched process.
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {
        "startupinfo": si,
        # CREATE_NO_WINDOW: do not allocate a console for the child at all.
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def hide_own_console():
    """
    Hide the console window that python.exe opens for this script itself.
    No effect when started with pythonw.exe / .pyw (no console exists).
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # 0 = SW_HIDE
    except Exception:
        # Cosmetic only; never let this break startup.
        pass


# Log color for each device status line.
STATUS_LOG_TAG = {
    ST_OK: "ok", ST_UPD: "warn", ST_NEWER: "ok",
    ST_NOBIN: "err", ST_UNKNOWN: "err",
}


def load_config():
    """Load config from disk; return an empty structure if missing/corrupt."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    # Guarantee both top-level keys exist.
    data.setdefault("devices", [])
    data.setdefault("firmware", [])
    for d in data["devices"]: d.pop("target", None)
    # Old format: dict id -> entry. Becomes a list of entries with id and chip.
    if isinstance(data["firmware"], dict):
        data["firmware"] = [{"id": k, "chip": "", **v} for k, v in data["firmware"].items()]
        # Show the new chip columns once.
        for key, after in (("dev", "fw_id"), ("fw", "id")):
            cols = data.get("columns", {}).get(key)
            if cols and after in cols: cols.insert(cols.index(after) + 1, "chip")
    return data


def find_fw(cfg, fid, chip=""):
    """
    Return the registry entry for a firmware id and chip, or None.
    An entry for exactly that chip wins; else the entry with no chip.
    """
    anyone = None
    for fw in cfg["firmware"]:
        if fw.get("id") != fid: continue
        if chip and fw.get("chip") == chip: return fw
        if not fw.get("chip"): anyone = fw
    return anyone


def save_config(cfg):
    """Write config to disk (pretty printed so it is hand-editable)."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def ensure_props_file():
    """Create the properties file with examples if it does not exist."""
    if not os.path.isfile(PROPS_FILE):
        with open(PROPS_FILE, "w", encoding="utf-8") as f:
            f.write(SEED_PROPS)


def load_props():
    """Read the properties file. Returns (variables, commands) dicts."""
    ensure_props_file()
    variables, cmds = {}, dict(BUILTIN_PRESETS)
    try:
        with open(PROPS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in "#!" or "=" not in line:
                    continue
                key, val = (x.strip() for x in line.split("=", 1))
                if key.startswith("cmd."):
                    cmds[key[4:]] = val
                elif key:
                    variables[key] = val
    except OSError:
        pass
    return variables, cmds


_PLACEHOLDER = re.compile(r"\{([A-Za-z_][\w.\-]*)\}")


def expand(text, builtins, variables, _stack=()):
    """
    Replace every {name} in text. Built-ins win over file variables; file
    variables may reference other variables. Raises KeyError for an unknown
    name and ValueError for a circular reference.
    """
    def sub(m):
        key = m.group(1)
        if key in builtins:
            return str(builtins[key])
        if key in variables:
            if key in _stack:
                raise ValueError(t("err.circular_var", name=key))
            return newest_match(expand(os.path.expandvars(variables[key]),
                                       builtins, variables, _stack + (key,)))
        raise KeyError(key)
    return _PLACEHOLDER.sub(sub, text)


def newest_match(pattern):
    """If pattern has * or ?, return the highest-versioned match (else as is)."""
    if not any(c in pattern for c in "*?"):
        return pattern
    hits = glob.glob(pattern)
    if not hits:
        return pattern
    return max(hits, key=lambda h: [(int(x), "") if x.isdigit() else (-1, x)
                                    for x in re.split(r"(\d+)", h)])


def resolve_command(value, builtins):
    """
    Turn a firmware 'cmd' value (command id or raw template) into a command.
    Precedence: built-ins > name.
    """
    variables, cmds = load_props()
    return expand(cmds.get(value or DEFAULT_CMD, value), builtins, variables)


def open_path(path):
    """Open a file or folder with the OS default application."""
    if os.name == "nt":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# --------------------------------------------------------------------------- #
# Device communication
# --------------------------------------------------------------------------- #

def fetch_version(dev):
    """
    Ask a device for its firmware info over HTTP.

    Returns (fw_id, build, md5, chip, error_text). On any failure fw_id/build/md5/chip
    are None and error_text explains why (shown in the Detail column).
    """
    port = dev.get("port") or 80
    path = dev.get("version_path") or "/version"
    if not path.startswith("/"):
        path = "/" + path
    url = "http://%s:%s%s" % (dev["host"], port, path)
    try:
        with request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
    except (error.URLError, OSError, ValueError) as exc:
        return None, None, None, None, t("detail.no_answer",
                                         reason=getattr(exc, "reason", exc))

    # Preferred format: JSON object with "id" and "build".
    try:
        obj = json.loads(body)
        if isinstance(obj, dict) and obj.get("id"):
            md5 = str(obj.get("md5") or "").strip().lower()
            chip = str(obj.get("chip") or "").strip().lower()
            return str(obj["id"]), str(obj.get("build", "")), md5, chip, ""
    except ValueError:
        pass

    # Fallback: loose regex for plain-text replies like  id=X build=123
    m_id = re.search(r'"?id"?\s*[:=]\s*"?([\w.\-]+)', body)
    m_bd = re.search(r'"?build"?\s*[:=]\s*"?(\d+)', body)
    m_md = re.search(r'"?md5"?\s*[:=]\s*"?([0-9a-fA-F]{32})', body)
    m_ch = re.search(r'"?chip"?\s*[:=]\s*"?([\w\-]+)', body)
    if m_id:
        return (m_id.group(1), (m_bd.group(1) if m_bd else ""),
                (m_md.group(1).lower() if m_md else ""),
                (m_ch.group(1).lower() if m_ch else ""), "")
    return None, None, None, None, t("detail.unrecognized")


def send_to_device(dev, mode, port, path, text):
    """
    Send text to a device and return its reply as text.

    mode "TCP":  raw TCP connection to host:port, text + newline is sent,
                 everything received until the device closes or goes quiet
                 (HTTP_TIMEOUT) is returned.
    otherwise:   HTTP to http://host:port/path - GET when text is empty,
                 else POST with text as the body (text/plain).
    Raises OSError (incl. URLError/timeouts) when the device cannot be reached.
    """
    if mode.upper() == "TCP":
        chunks = []
        with socket.create_connection((dev["host"], port), timeout=HTTP_TIMEOUT) as s:
            s.sendall((text + "\n").encode("utf-8"))
            try:
                while True:
                    data = s.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
            except socket.timeout:
                pass
        return b"".join(chunks).decode("utf-8", "replace").strip()

    if not path.startswith("/"):
        path = "/" + path
    req = request.Request(
        "http://%s:%s%s" % (dev["host"], port, path),
        data=text.encode("utf-8") if text else None,
        headers={"Content-Type": "text/plain"} if text else {})
    try:
        with request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace").strip()
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace").strip()
        return ("HTTP %s %s\n%s" % (exc.code, exc.reason, body)).strip()


# --------------------------------------------------------------------------- #
# Firmware registry helpers
# --------------------------------------------------------------------------- #

def local_build(fw):
    """Work out the build number of a registry entry's binary (see header)."""
    # 1. manual override in registry
    if str(fw.get("build", "")).strip():
        return str(fw["build"]).strip()
    path = fw.get("file", "")
    if not path or not os.path.isfile(path):
        return ""
    # 2. sidecar json
    try:
        with open(path + ".json", "r", encoding="utf-8") as f:
            side = json.load(f)
        if str(side.get("build", "")).strip():
            return str(side["build"]).strip()
    except (OSError, ValueError, AttributeError):
        pass
    # 3. 14 digit timestamp in file name
    m = re.search(r"(\d{14})", os.path.basename(path))
    if m:
        return m.group(1)
    # 4. modification time
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y%m%d%H%M%S")


# md5 cache: (path, mtime_ns, size) -> hex digest, so rescans don't rehash.
_MD5_CACHE = {}


def file_md5(path):
    """Return the lowercase md5 hex digest of a file ('' if unreadable)."""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _MD5_CACHE.get(key)
    if hit:
        return hit
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    _MD5_CACHE[key] = h.hexdigest()
    return _MD5_CACHE[key]


def compute_status(reported_id, reported_build, target_fw, reported_md5=""):
    """
    Decide the status string for one device.

    reported_id    firmware id the device reported (None if unreachable)
    reported_build build the device reported
    target_fw      registry entry to compare against (or None)
    reported_md5   md5 the device reported (optional, '' if none)
    """
    if not reported_id:
        return ST_UNKNOWN
    if not target_fw or not target_fw.get("file") or not os.path.isfile(target_fw["file"]):
        return ST_NOBIN
    new = local_build(target_fw)

    # md5 is the exact-identity check; build only gives the ordering.
    dev_md5 = (reported_md5 or "").strip().lower()
    if dev_md5:
        file_md = file_md5(target_fw["file"])
        if file_md:
            if dev_md5 == file_md:
                return ST_OK
            try:
                if int(reported_build) > int(new):
                    return ST_NEWER
            except (ValueError, TypeError):
                pass
            return ST_UPD

    if not new or not reported_build:
        # Cannot compare numerically; treat different/missing as update.
        return ST_UPD
    try:
        if int(reported_build) < int(new):
            return ST_UPD
        if int(reported_build) > int(new):
            return ST_NEWER
        return ST_OK
    except ValueError:
        return ST_OK if reported_build == new else ST_UPD


# --------------------------------------------------------------------------- #
# Generic form dialog
# --------------------------------------------------------------------------- #

class FormDialog(tk.Toplevel):
    """
    Modal form. fields = [(key, label, kind)], kind is "text", "file" or a
    list of choices (editable dropdown).
    After wait_window(), .result is a dict of values or None if cancelled.
    """

    def __init__(self, parent, title, fields, values=None):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.transient(parent)
        self.result = None
        self.vars = {}
        values = values or {}

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        for row, (key, label, kind) in enumerate(fields):
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(values.get(key, "")))
            self.vars[key] = var
            if isinstance(kind, (list, tuple)):
                ttk.Combobox(frm, textvariable=var, values=list(kind),
                             width=57).grid(row=row, column=1, sticky="ew", padx=5)
            else:
                ttk.Entry(frm, textvariable=var, width=60).grid(
                    row=row, column=1, sticky="ew", padx=5)
            if kind == "file":
                # Browse button fills the entry with a chosen file path.
                ttk.Button(frm, text="...", width=3,
                           command=lambda v=var: self._browse(v)).grid(row=row, column=2)

        btns = ttk.Frame(frm)
        btns.grid(row=len(fields), column=0, columnspan=3, pady=(10, 0), sticky="e")
        ttk.Button(btns, text=t("btn.ok"), command=self._ok).pack(side="left", padx=3)
        ttk.Button(btns, text=t("btn.cancel"), command=self.destroy).pack(side="left")

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self._center(parent)
        self.deiconify()
        self.grab_set()
        self.wait_window(self)

    def _center(self, parent):
        """Place the dialog in the middle of its parent window."""
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))

    def _browse(self, var):
        """Open a file chooser and store the selection in var."""
        path = filedialog.askopenfilename()
        if path:
            var.set(path)

    def _ok(self):
        """Collect all entry values and close."""
        self.result = {k: v.get().strip() for k, v in self.vars.items()}
        self.destroy()


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #

class App(tk.Tk):
    """Main window: devices tab, firmware tab, log pane."""

    DEV_COLS = ("name", "host", "fw_id", "chip", "build", "md5", "status", "detail")
    FW_COLS = ("id", "chip", "file", "build", "md5", "cmd")
    CHIPS = ("esp8266", "esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6")

    def __init__(self):
        super().__init__()

        self.cfg = load_config()
        self.cfg["language"] = set_language(self.cfg.get("language"))
        refresh_status_text()
        self.title(t("app.title", version=VERSION))
        if seed_colors(self.cfg):
            try:
                save_config(self.cfg)    # so the colors show up in the file
            except OSError:
                pass
        self.geometry(self.cfg.get("ui", {}).get("geometry") or "1100x640")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Runtime (non-persisted) state per device name:
        # {"fw_id", "build", "status", "detail"}
        self.state = {}
        # Device names currently running a job (prevents double actions).
        self.busy = set()
        # Worker threads push callables here; main thread executes them.
        self.q = queue.Queue()

        self._build_ui()
        self._build_menubar()
        self._refresh_devices()
        self._refresh_firmware()
        self._restore_ui()
        self.after(100, self._pump)      # start queue pump
        self.after(300, self.scan_all)   # auto scan on start

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        """Create notebook, toolbars, trees and the log pane."""
        paned = self.paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True)

        nb = self.nb = ttk.Notebook(paned)
        paned.add(nb, weight=4)

        # ---- Devices tab ----
        dev_tab = ttk.Frame(nb)
        nb.add(dev_tab, text=t("tab.devices"))

        bar = ttk.Frame(dev_tab, padding=4)
        bar.pack(fill="x")
        for text, cmd in ((t("btn.scan_all"), self.scan_all),
                          (t("btn.check"), self.check_selected),
                          (t("btn.update"), self.update_selected),
                          (t("btn.update_all"), self.update_all_outdated),
                          (t("btn.install"), self.install_fw),
                          (t("btn.link"), self.link_selected),
                          (t("btn.toggle"), self.toggle_disabled),
                          ("|", None),
                          (t("btn.add"), self.add_device),
                          (t("btn.edit"), self.edit_device),
                          (t("btn.remove"), self.remove_device)):
            if cmd is None:
                ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
            else:
                ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)

        self.dev_tree = self._make_tree(
            dev_tab, self.DEV_COLS,
            tuple(t("col.dev." + c) for c in self.DEV_COLS),
            (140, 130, 160, 80, 130, 250, 140, 260), "dev", self._dev_menu)
        # Remove EVERY state mapping that touches "selected" (and the Tk 8.6.9
        # "!selected" bug entries) from the Treeview colors. Result: row tag
        # colors always win, so the native blue selection never hides the
        # status color. Selection is shown by our own marker/bold tags below.
        style = ttk.Style(self)

        def _strip_selected(option):
            """Return the style map entries for option without selected states."""
            entries = style.map("Treeview", query_opt=option)
            return [e for e in entries
                    if not any("selected" in str(s) for s in e[:-1])]

        style.map("Dev.Treeview",
                  foreground=_strip_selected("foreground"),
                  background=_strip_selected("background"))
        self.dev_tree.configure(style="Dev.Treeview")
        # Every other tree (firmware registry) shows a clear blue selection.
        style.map("Treeview",
                  foreground=_strip_selected("foreground")
                  + [("selected", self._color("selection", "text"))],
                  background=_strip_selected("background")
                  + [("selected", self._color("selection", "background"))])

        # Bold + underlined font used for selected rows.
        self.sel_font = tkfont.nametofont("TkDefaultFont").copy()
        self.sel_font.configure(weight="bold", underline=True)
        TEXT_FG = self._color("table", "text")
        OFF_FG = self._color("table", "disabled_text")
        # Row background per status (colors come from the config).
        status_colors = {tag: self._color("status", tag)
                         for tag in DEFAULT_COLORS["status"]}
        for tag, color in status_colors.items():
            fg = OFF_FG if tag == ST_DISABLED else TEXT_FG
            self.dev_tree.tag_configure(tag, background=color, foreground=fg)
        # Selected variants: SAME background (status color stays visible),
        # only the font changes. Created after the base tags so they have
        # higher priority in Tk's tag stacking order.
        for tag, color in status_colors.items():
            fg = OFF_FG if tag == ST_DISABLED else TEXT_FG
            self.dev_tree.tag_configure(tag + "|sel", background=color,
                                        foreground=fg, font=self.sel_font)
        # Re-mark rows whenever the selection changes.
        self.dev_tree.bind("<<TreeviewSelect>>", lambda e: self._update_marks())
        # Double click = edit device (right click is bound inside _make_tree:
        # header -> column chooser, row -> device menu).
        self.dev_tree.bind("<Double-1>", lambda e: self.edit_device())

        # ---- Firmware tab ----
        fw_tab = ttk.Frame(nb)
        nb.add(fw_tab, text=t("tab.firmware"))

        bar2 = ttk.Frame(fw_tab, padding=4)
        bar2.pack(fill="x")
        for text, cmd in ((t("btn.add"), self.add_fw), (t("btn.edit"), self.edit_fw),
                          (t("btn.remove"), self.remove_fw)):
            ttk.Button(bar2, text=text, command=cmd).pack(side="left", padx=2)

        self.fw_tree = self._make_tree(
            fw_tab, self.FW_COLS,
            tuple(t("col.fw." + c) for c in self.FW_COLS),
            (180, 80, 320, 140, 250, 340), "fw")
        self.fw_tree.bind("<Double-1>", lambda e: self.edit_fw())

        # ---- Log pane ----
        log_frame = ttk.Frame(paned)
        paned.add(log_frame, weight=1)

        # Send bar right below the log (console style): type, press Enter.
        sendbar = ttk.Frame(log_frame, padding=(4, 4))
        sendbar.pack(side="bottom", fill="x")
        self.send_mode = tk.StringVar(value="HTTP")
        ttk.Combobox(sendbar, textvariable=self.send_mode, values=["HTTP", "TCP"],
                     width=6, state="readonly").pack(side="left", padx=(0, 4))
        self.send_text = tk.StringVar()
        ent = ttk.Entry(sendbar, textvariable=self.send_text)
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<Return>", lambda e: self.send_selected())
        ent.bind("<Up>", lambda e: self._hist_move(-1))
        ent.bind("<Down>", lambda e: self._hist_move(1))
        self.send_entry = ent
        self.send_hist = []   # sent texts, oldest first
        self.send_pos = 0     # Up/Down position in send_hist
        ttk.Button(sendbar, text=t("btn.send"), command=self.send_selected).pack(
            side="left", padx=(4, 0))

        self.log = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        self._apply_log_colors()

        # Context menu for devices.
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label=t("btn.check"), command=self.check_selected)
        self.menu.add_command(label=t("btn.update"), command=self.update_selected)
        self.menu.add_command(label=t("btn.install"), command=self.install_fw)
        self.menu.add_command(label=t("btn.link"), command=self.link_selected)
        self.menu.add_command(label=t("btn.toggle"), command=self.toggle_disabled)
        self.menu.add_separator()
        self.menu.add_command(label=t("btn.edit"), command=self.edit_device)
        self.menu.add_command(label=t("btn.remove"), command=self.remove_device)

    def _build_language_menu(self, parent):
        """
        Create the Language menu: one radio item per file in lang/.

        @param parent  menu that will hold the submenu
        @return        the new submenu
        """
        sub = tk.Menu(parent, tearoff=0)
        self.lang_var = tk.StringVar(value=self.cfg.get("language", ""))
        for code, name in list_languages():
            sub.add_radiobutton(label=name, value=code, variable=self.lang_var,
                                command=lambda c=code: self.select_language(c))
        return sub

    def select_language(self, code):
        """
        Language menu: save the choice to the config. The program text
        changes the next time the program starts.

        @param code  language code, e.g. "en"
        """
        self.cfg["language"] = resolve_language(code)
        self.lang_var.set(self.cfg["language"])
        try:
            save_config(self.cfg)
        except OSError as exc:
            messagebox.showerror(t("dlg.language"), t("msg.save_failed", error=exc))
            return
        self.say(t("msg.language_saved"), "warn")

    def _build_menubar(self):
        """Create the window menu bar (File / Devices / Firmware / Settings / Language)."""
        bar = tk.Menu(self)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label=t("menu.exit"), command=self._on_close)
        bar.add_cascade(label=t("menu.file"), menu=m)

        m = tk.Menu(bar, tearoff=0)
        for label, cmd in ((t("btn.scan_all"), self.scan_all),
                           (t("btn.check"), self.check_selected),
                           (t("btn.update"), self.update_selected),
                           (t("btn.update_all"), self.update_all_outdated),
                           (t("btn.install"), self.install_fw),
                           (t("btn.link"), self.link_selected),
                           (t("btn.toggle"), self.toggle_disabled),
                           (None, None),
                           (t("btn.add"), self.add_device),
                           (t("btn.edit"), self.edit_device),
                           (t("btn.remove"), self.remove_device)):
            if label:
                m.add_command(label=label, command=cmd)
            else:
                m.add_separator()
        bar.add_cascade(label=t("menu.devices"), menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label=t("btn.add"), command=self.add_fw)
        m.add_command(label=t("btn.edit"), command=self.edit_fw)
        m.add_command(label=t("btn.remove"), command=self.remove_fw)
        bar.add_cascade(label=t("menu.firmware"), menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label=t("menu.colors"), command=self.edit_colors)
        m.add_separator()
        m.add_command(label=t("menu.open_props"), command=self.open_props)
        m.add_command(label=t("menu.show_props"), command=self.show_props)
        m.add_command(label=t("menu.open_config"),
                      command=lambda: open_path(os.path.dirname(CONFIG_FILE)))
        bar.add_cascade(label=t("menu.settings"), menu=m)

        bar.add_cascade(label=t("menu.language"), menu=self._build_language_menu(bar))

        self.configure(menu=bar)

    def open_props(self):
        """Open the variables file in the default editor."""
        ensure_props_file()
        open_path(PROPS_FILE)

    def show_props(self):
        """Write all variables and command ids to the log pane."""
        variables, cmds = load_props()
        self.say("%s:" % PROPS_FILE)
        for k, v in variables.items():
            self.say("  %s = %s" % (k, v))
        for k, v in cmds.items():
            self.say("  cmd.%s = %s" % (k, v))

    def _color(self, group, name):
        """
        Configured color if Tk accepts it, else the built-in default.

        @param group  color group, e.g. "status"
        @param name   color name inside the group
        @return       color string usable by Tk
        """
        return resolve_color(self, self.cfg, group, name)

    def _apply_log_colors(self):
        """
        Set the text color of each log line kind (called once at startup).
        Colors come from the "log" group of the config.
        """
        for name in DEFAULT_COLORS["log"]:
            self.log.tag_configure(name, foreground=self._color("log", name))

    def edit_colors(self):
        """
        Settings > Colors...: edit the colors and save them to the config.
        The new colors are used the next time the program starts.
        """
        dlg = ColorsDialog(self, current_colors(self.cfg))
        if dlg.result is None:
            return
        self.cfg["colors"] = dlg.result
        try:
            save_config(self.cfg)
        except OSError as exc:
            messagebox.showerror(t("dlg.colors"), t("msg.save_failed", error=exc))
            return
        messagebox.showinfo(t("dlg.colors"), t("msg.colors_saved"))

    def _make_tree(self, parent, cols, heads, widths, key, row_menu=None):
        """
        Build a Treeview with scrollbar inside parent and return it.
        key       name used to store the visible columns in the config
        row_menu  optional handler for right click on a row
        Right click on a column header opens a show/hide column menu.
        """
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=cols, show="headings",
                            selectmode="extended")
        for c, h, w in zip(cols, heads, widths):
            # anchor="w" left-aligns the header text (default is centered).
            tree.heading(c, text=h, anchor="w")
            tree.column(c, width=w, anchor="w")
        # Restore saved column widths.
        for c, w in self.cfg.get("ui", {}).get("widths", {}).get(key, {}).items():
            if c in cols and isinstance(w, int) and w >= 20:
                tree.column(c, width=w)
        sb = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)

        # Restore saved column visibility (ignore stale/unknown names).
        saved = [c for c in self.cfg.get("columns", {}).get(key, []) if c in cols]
        if saved:
            tree.configure(displaycolumns=[c for c in cols if c in saved])

        def on_right_click(event):
            if self._header_menu(tree, key, event):
                return
            if row_menu:
                row_menu(event)
        tree.bind("<Button-3>", on_right_click)
        return tree

    def _header_menu(self, tree, key, event):
        """
        Show the Windows-like 'choose columns' menu when the click landed on
        the header. Returns True if the click was on the header.
        """
        if tree.identify_region(event.x, event.y) not in ("heading", "separator"):
            return False
        all_cols = list(tree["columns"])
        shown = list(tree["displaycolumns"])
        if shown == ["#all"]:
            shown = all_cols
        menu = tk.Menu(self, tearoff=0)
        menu._vars = []  # keep BooleanVars alive while the menu exists
        for c in all_cols:
            var = tk.BooleanVar(value=c in shown)
            menu._vars.append(var)
            menu.add_checkbutton(label=tree.heading(c, "text"), variable=var,
                                 command=lambda c=c: self._toggle_column(tree, key, c))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return True

    def _toggle_column(self, tree, key, col):
        """Show/hide one column (at least one stays visible) and persist."""
        all_cols = list(tree["columns"])
        shown = list(tree["displaycolumns"])
        if shown == ["#all"]:
            shown = all_cols
        if col in shown:
            if len(shown) == 1:
                return  # never hide the last column
            shown.remove(col)
        else:
            shown.append(col)
        shown = [c for c in all_cols if c in shown]  # keep natural order
        tree.configure(displaycolumns=shown)
        self.cfg.setdefault("columns", {})[key] = shown
        save_config(self.cfg)

    def _dev_menu(self, event):
        """Right click: select the row under cursor and show the menu."""
        row = self.dev_tree.identify_row(event.y)
        if row:
            self.dev_tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------ UI state --

    def _restore_ui(self):
        """Restore maximized state and log pane height."""
        ui = self.cfg.get("ui", {})
        if ui.get("zoomed"):
            try:
                # wm_state, not state: App.state is the per-device status dict.
                self.wm_state("zoomed")
            except tk.TclError:
                pass

        def sash():
            h = self.paned.winfo_height()
            log_h = ui.get("log_h")
            if isinstance(log_h, int) and 40 <= log_h <= h - 80:
                self.paned.sashpos(0, h - log_h)
        self.after(200, sash)  # needs a laid-out window

    def _on_close(self):
        """Save the UI state, then quit."""
        try:
            zoomed = self.wm_state() == "zoomed"
            if zoomed:
                self.wm_state("normal")   # so geometry() is the normal size
                self.update_idletasks()
            ui = self.cfg.setdefault("ui", {})
            ui["geometry"] = self.geometry()
            ui["zoomed"] = zoomed
            ui["log_h"] = self.paned.winfo_height() - self.paned.sashpos(0)
            ui["widths"] = {
                key: {c: int(t.column(c, "width")) for c in t["columns"]}
                for key, t in (("dev", self.dev_tree), ("fw", self.fw_tree))}
            save_config(self.cfg)
        except (OSError, tk.TclError):
            pass
        self.destroy()

    # --------------------------------------------------------- thread glue --

    def _pump(self):
        """Run callables queued by worker threads on the Tk main thread."""
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def ui(self, fn):
        """Schedule fn on the main thread (safe to call from any thread)."""
        self.q.put(fn)

    def say(self, text, tag=None):
        """
        Append a timestamped line to the log pane (thread safe).
        tag = one of the DEFAULT_COLORS["log"] names to color the line (None = default).
        """
        stamp = datetime.now().strftime("%H:%M:%S")

        def _write():
            self.log.configure(state="normal")
            self.log.insert("end", "[%s] " % stamp, "dim")
            self.log.insert("end", "%s\n" % text, (tag,) if tag else ())
            self.log.see("end")
            self.log.configure(state="disabled")
        self.ui(_write)

    # ------------------------------------------------------------ refresh --

    def _device_fw(self, dev):
        """Return (reported_id, registry_entry) for a device (entry matches id and chip)."""
        fid = self.state.get(dev["name"], {}).get("fw_id") or ""
        return fid, find_fw(self.cfg, fid, self._chip(dev))

    def _chip(self, dev):
        """Chip of a device: explicit 'chip' field, else the one it reports."""
        return dev.get("chip") or self.state.get(dev["name"], {}).get("chip") or ""

    def _refresh_devices(self):
        """Rebuild the device tree from config + runtime state."""
        sel = self.dev_tree.selection()
        self.dev_tree.delete(*self.dev_tree.get_children())
        for dev in self.cfg["devices"]:
            st = self.state.get(dev["name"], {})
            status = st.get("status", ST_UNCHECKED)
            if dev.get("disabled"): status = ST_DISABLED
            if dev["name"] in self.busy: status = ST_BUSY
            self.dev_tree.insert(
                "", "end", iid=dev["name"],
                values=(dev["name"], dev["host"],
                        st.get("fw_id") or t("val.unknown"), self._chip(dev),
                        st.get("build", ""), st.get("md5", ""),
                        STATUS_TEXT[status], st.get("detail", "")),
                tags=(status,))
        # Restore selection where possible.
        keep = [s for s in sel if self.dev_tree.exists(s)]
        if keep:
            self.dev_tree.selection_set(keep)
        self._update_marks()

    def _update_marks(self):
        """
        Show selection without touching the status color: selected rows keep
        their status background but get a bold underlined font (via the
        "<status>|sel" tag).
        """
        selected = set(self.dev_tree.selection())
        for iid in self.dev_tree.get_children():
            shown = self.dev_tree.set(iid, "status")
            status = STATUS_ID.get(shown, shown)
            is_sel = iid in selected
            tags = (status, status + "|sel") if is_sel else (status,)
            self.dev_tree.item(iid, tags=tags)

    def _refresh_firmware(self):
        """Rebuild the firmware registry tree."""
        self.fw_tree.delete(*self.fw_tree.get_children())
        for i, fw in sorted(enumerate(self.cfg["firmware"]),
                            key=lambda p: (p[1].get("id", ""), p[1].get("chip", ""))):
            self.fw_tree.insert("", "end", iid=str(i), values=(
                fw.get("id", ""), fw.get("chip", ""), fw.get("file", ""),
                fw.get("build") or local_build(fw) or "-",
                file_md5(fw.get("file", "")) or "-",
                fw.get("cmd", "")))

    def _selected_fw(self):
        """Return the list of selected registry entries."""
        return [self.cfg["firmware"][int(i)] for i in self.fw_tree.selection()]

    def _selected_devices(self):
        """Return the list of selected device dicts."""
        names = self.dev_tree.selection()
        return [d for d in self.cfg["devices"] if d["name"] in names]

    # ------------------------------------------------------------- checks --

    def _do_check(self, dev):
        """Worker body: query one device and store the resulting status."""
        fid, build, md5, chip, err = fetch_version(dev)
        chip = dev.get("chip") or chip or ""
        st = {"fw_id": fid, "build": build or "", "md5": md5 or "", "chip": chip,
              "detail": err}
        # Compare against the registry entry for the reported id and chip.
        tid = fid or ""
        fw = find_fw(self.cfg, tid, chip)
        st["status"] = compute_status(fid, build, fw, md5)
        if st["status"] == ST_NOBIN and not st["detail"]:
            st["detail"] = t("detail.no_binary", id=tid, chip=chip or t("val.unknown"))
        elif st["status"] == ST_UPD and fw:
            if md5 and build and build == local_build(fw):
                st["detail"] = t("detail.same_build")
            else:
                st["detail"] = t("detail.available_build", build=local_build(fw))
        elif st["status"] == ST_NEWER and md5:
            st["detail"] = t("detail.newer_md5")
        self.state[dev["name"]] = st
        self.say(t("out.check_result", name=dev["name"], fw=fid or t("val.unknown"),
                   build=build or "", status=STATUS_TEXT[st["status"]]),
                 STATUS_LOG_TAG.get(st["status"]))
        if fid:
            file_md = file_md5(fw["file"]) if fw and fw.get("file") else ""
            if not md5:
                self.say(t("out.md5_not_reported", name=dev["name"]), "warn")
            elif not file_md:
                self.say(t("out.md5_not_compared", name=dev["name"], id=tid), "warn")
            elif md5.strip().lower() == file_md:
                self.say(t("out.md5_match", name=dev["name"], md5=md5), "ok")
            else:
                self.say(t("out.md5_diff", name=dev["name"], device=md5, file=file_md),
                         "warn")

    def _run_checks(self, devs):
        """Check several devices in parallel, then refresh the table."""
        for d in devs:
            self.busy.add(d["name"])
        self.ui(self._refresh_devices)

        def one(d):
            try:
                self._do_check(d)
            finally:
                self.busy.discard(d["name"])
                self.ui(self._refresh_devices)

        def job():
            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(one, devs))
        threading.Thread(target=job, daemon=True).start()

    def scan_all(self):
        """Check every saved device that is not disabled."""
        devs = [d for d in self.cfg["devices"]
                if d["name"] not in self.busy and not d.get("disabled")]
        if devs:
            self.say(t("out.scanning", count=len(devs)))
            self._run_checks(devs)

    def check_selected(self):
        """Re-check the selected devices."""
        devs = [d for d in self._selected_devices() if d["name"] not in self.busy]
        if devs:
            self._run_checks(devs)

    # --------------------------------------------------------------- send --

    def _hist_move(self, step):
        """Up/Down in the send box: walk through previously sent texts."""
        h = self.send_hist
        if h:
            self.send_pos = max(0, min(len(h), self.send_pos + step))
            self.send_text.set(h[self.send_pos] if self.send_pos < len(h) else "")
            self.send_entry.icursor("end")
        return "break"

    def send_selected(self):
        """
        Send the send-bar text to every selected device, log each reply.
        HTTP: text starting with / is a GET of that path as typed; anything
        else is sent as GET <device "Command path"><text> (default /cmd?q=),
        with the text URL-encoded.
        TCP: text + newline over a raw connection.
        """
        text = self.send_text.get().strip()
        devs = [d for d in self._selected_devices() if d["name"] not in self.busy]
        if not text or not devs:
            return
        mode = self.send_mode.get()
        if not self.send_hist or self.send_hist[-1] != text:
            self.send_hist.append(text)
        self.send_pos = len(self.send_hist)
        self.send_text.set("")

        def one(dev):
            path, body = "/", text
            if mode == "HTTP":
                body = ""
                if text.startswith("/"):
                    path = text
                else:
                    prefix = dev.get("cmd_path") or "/cmd?q="
                    if not prefix.startswith("/"):
                        prefix = "/" + prefix
                    path = prefix + parse.quote(text)
            self.say("%s: > %s" % (dev["name"], text), "info")
            try:
                reply = send_to_device(dev, mode, dev.get("port") or 80, path, body)
            except OSError as exc:
                self.say(t("out.send_failed", name=dev["name"],
                           reason=getattr(exc, "reason", exc)), "err")
                return
            for line in (reply.splitlines() or [t("out.empty_reply")]):
                self.say("  %s | %s" % (dev["name"], line))

        def job():
            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(one, devs))
        threading.Thread(target=job, daemon=True).start()

    # ------------------------------------------------------------ updates --

    def _install(self, dev, fw=None):
        """
        Worker body: run the upload command template for one device.
        fw = registry entry to flash (default: the entry for the device's own firmware).
        Returns True when the upload command exited with code 0.
        """
        if fw: tid = fw["id"]
        else: tid, fw = self._device_fw(dev)
        if not fw or not os.path.isfile(fw.get("file", "")):
            self.say(t("out.no_binary", name=dev["name"], id=tid,
                       chip=self._chip(dev) or t("val.unknown")), "err")
            return False
        try:
            cmd = resolve_command(fw.get("cmd"), {
                "host": dev["host"], "port": dev.get("port") or 80,
                "name": dev["name"], "id": tid, "file": fw["file"],
                "build": local_build(fw), "chip": self._chip(dev)})
        except (KeyError, ValueError) as exc:
            self.say(t("out.bad_placeholder", name=dev["name"], error=exc), "err")
            return False

        self.say(t("out.running", name=dev["name"], cmd=cmd), "info")
        try:
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    errors="replace",
                                    stdin=subprocess.DEVNULL,
                                    **hidden_process_kwargs())
            # Stream the tool output into the log as it arrives.
            for line in proc.stdout:
                self.say("  %s | %s" % (dev["name"], line.rstrip()), "dim")
            code = proc.wait()
        except OSError as exc:
            self.say(t("out.start_failed", name=dev["name"], error=exc), "err")
            return False
        self.say(t("out.exit_code", name=dev["name"], code=code),
                 "ok" if code == 0 else "err")
        return code == 0

    def _run_updates(self, devs, fw=None):
        """
        Update devices one after another (re-check only when an upload failed).
        fw = registry entry to flash on all of them (default: each device's own).
        """
        for d in devs:
            self.busy.add(d["name"])
        self.ui(self._refresh_devices)

        def job():
            for d in devs:
                ok = self._install(d, fw)
                self.say(t("out.update_finished" if ok else "out.update_failed",
                           name=d["name"]), "ok" if ok else "err")
                # No re-check after a successful upload; only after a failure.
                if not ok:
                    self._do_check(d)
                self.busy.discard(d["name"])
                self.ui(self._refresh_devices)
        threading.Thread(target=job, daemon=True).start()

    def update_selected(self):
        """Update the selected devices (asks for confirmation)."""
        devs = [d for d in self._selected_devices() if d["name"] not in self.busy]
        if not devs:
            return
        names = ", ".join(d["name"] for d in devs)
        if messagebox.askyesno(t("dlg.update"), t("msg.update_confirm", names=names)):
            self._run_updates(devs)

    def install_fw(self):
        """Pick a registry entry for the selected devices' chip and flash it."""
        devs = [d for d in self._selected_devices() if d["name"] not in self.busy]
        if not devs:
            return
        chips = {self._chip(d) for d in devs}
        if len(chips) > 1:
            messagebox.showerror(t("dlg.install"), t("msg.diff_chips"))
            return
        chip = chips.pop()
        opts = {"%s [%s]" % (f["id"], f.get("chip") or t("val.any")): f
                for f in self.cfg["firmware"] if not f.get("chip") or f["chip"] == chip}
        if not opts:
            messagebox.showinfo(t("dlg.install"), t("msg.no_fw_for_chip",
                                                    chip=chip or t("val.unknown")))
            return
        dlg = FormDialog(self, t("dlg.install"), [("fw", t("field.fw"), sorted(opts))])
        fw = opts.get(dlg.result["fw"]) if dlg.result else None
        if not fw:
            return
        lines = "\n".join("%s (%s)" % (d["name"], self.state.get(d["name"], {}).get("fw_id")
                                       or t("val.unknown"))
                          for d in devs)
        if messagebox.askyesno(t("dlg.install"), t("msg.flash_confirm", id=fw["id"],
                                                   chip=fw.get("chip") or t("val.any"),
                                                   lines=lines)):
            self._run_updates(devs, fw)

    def update_all_outdated(self):
        """Update every device whose status is UPDATE AVAILABLE."""
        devs = [d for d in self.cfg["devices"]
                if self.state.get(d["name"], {}).get("status") == ST_UPD
                and d["name"] not in self.busy and not d.get("disabled")]
        if not devs:
            self.say(t("out.nothing_to_update"))
            return
        names = ", ".join(d["name"] for d in devs)
        if messagebox.askyesno(t("dlg.update_all"), t("msg.update_all_confirm", names=names)):
            self._run_updates(devs)

    # ----------------------------------------------------- device editing --

    @property
    def DEV_FIELDS(self):
        """Device form fields."""
        return [("name", t("field.name"), "text"), ("host", t("field.host"), "text"),
                ("port", t("field.port"), "text"),
                ("version_path", t("field.version_path"), "text"),
                ("chip", t("field.chip_dev"), list(self.CHIPS)),
                ("cmd_path", t("field.cmd_path"), "text")]

    def add_device(self):
        """Add a new device (form pre-filled from the selected device) and check it."""
        sel = self._selected_devices()
        dlg = FormDialog(self, t("dlg.add_device"), self.DEV_FIELDS, sel[0] if sel else None)
        self._store_device(dlg.result, None)

    def edit_device(self):
        """Edit the first selected device."""
        sel = self._selected_devices()
        if not sel:
            return
        dlg = FormDialog(self, t("dlg.edit_device"), self.DEV_FIELDS, sel[0])
        self._store_device(dlg.result, sel[0])

    def _store_device(self, res, old):
        """Validate and save a device form result (old = device being edited)."""
        if not res or not res["name"] or not res["host"]:
            return
        # Names are the unique key; refuse duplicates.
        if any(d["name"] == res["name"] and d is not old for d in self.cfg["devices"]):
            messagebox.showerror(t("dlg.error"), t("msg.name_exists"))
            return
        # Keep port numeric.
        res["port"] = int(res["port"]) if res["port"].isdigit() else ""
        if old is None:
            self.cfg["devices"].append(res)
        else:
            self.state.pop(old["name"], None)
            disabled = old.get("disabled")
            old.clear()
            old.update(res)
            if disabled: old["disabled"] = True
        save_config(self.cfg)
        self._refresh_devices()
        self._run_checks([res if old is None else old])

    def toggle_disabled(self):
        """Disable the selected devices (enable them if all are already disabled)."""
        sel = self._selected_devices()
        if not sel: return
        off = not all(d.get("disabled") for d in sel)
        for d in sel:
            if off: d["disabled"] = True
            else: d.pop("disabled", None)
        save_config(self.cfg)
        self._refresh_devices()

    def remove_device(self):
        """Remove the selected devices from the saved list."""
        sel = self._selected_devices()
        if sel and messagebox.askyesno(t("dlg.remove"), t("msg.remove_devices", count=len(sel))):
            for d in sel:
                self.cfg["devices"].remove(d)
                self.state.pop(d["name"], None)
            save_config(self.cfg)
            self._refresh_devices()

    # ----------------------------------------------------- firmware editing --

    @property
    def FW_FIELDS(self):
        """Firmware form fields; the command dropdown lists every command id."""
        return [("id", t("field.fw_id"), "text"),
                ("chip", t("field.chip_fw"), list(self.CHIPS)),
                ("file", t("field.file"), "file"),
                ("build", t("field.build"), "text"),
                ("cmd", t("field.cmd"), sorted(load_props()[1]))]

    def _store_fw(self, res, old=None):
        """Save a firmware form result (old = registry entry being edited)."""
        if not res or not res["id"]:
            return
        # One entry per firmware id and chip; refuse duplicates.
        if any(f["id"] == res["id"] and f.get("chip", "") == res["chip"] and f is not old
               for f in self.cfg["firmware"]):
            messagebox.showerror(t("dlg.error"), t("msg.fw_exists"))
            return
        # Pre-fill the default command so it is visible/editable later.
        res["cmd"] = res["cmd"] or DEFAULT_CMD
        entry = {k: res[k] for k in ("id", "chip", "file", "build", "cmd")}
        if old is None:
            self.cfg["firmware"].append(entry)
        else:
            old.clear()
            old.update(entry)
        save_config(self.cfg)
        self._refresh_firmware()
        # Statuses may change now that a binary is linked.
        self.scan_all()

    def add_fw(self):
        """Add a registry entry (form pre-filled from the selected entry)."""
        sel = self._selected_fw()
        vals = dict(sel[0]) if sel else {"cmd": DEFAULT_CMD}
        dlg = FormDialog(self, t("dlg.add_fw"), self.FW_FIELDS, vals)
        self._store_fw(dlg.result)

    def edit_fw(self):
        """Edit the selected registry entry."""
        sel = self._selected_fw()
        if not sel:
            return
        dlg = FormDialog(self, t("dlg.edit_fw"), self.FW_FIELDS, dict(sel[0]))
        self._store_fw(dlg.result, sel[0])

    def remove_fw(self):
        """Remove the selected registry entries."""
        sel = self._selected_fw()
        if sel and messagebox.askyesno(t("dlg.remove"), t("msg.remove_fw", count=len(sel))):
            gone = {id(f) for f in sel}
            self.cfg["firmware"][:] = [f for f in self.cfg["firmware"] if id(f) not in gone]
            save_config(self.cfg)
            self._refresh_firmware()
            self.scan_all()

    def link_selected(self):
        """Create/edit the registry entry for the selected device's firmware."""
        sel = self._selected_devices()
        if not sel:
            return
        tid, fw = self._device_fw(sel[0])
        if not tid:
            messagebox.showinfo(t("dlg.link_info"), t("msg.link_unknown"))
            return
        vals = dict(fw or {"cmd": DEFAULT_CMD, "chip": self._chip(sel[0])}, id=tid)
        dlg = FormDialog(self, t("dlg.link_binary", id=tid), self.FW_FIELDS, vals)
        self._store_fw(dlg.result, fw)


if __name__ == "__main__":
    # Hide the python.exe console so only the GUI window is visible.
    hide_own_console()
    App().mainloop()
