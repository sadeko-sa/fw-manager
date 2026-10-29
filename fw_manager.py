#!/usr/bin/env python3
"""
fw_manager.py - Network firmware manager for Arduino / ESP / any HTTP device.

What it does
------------
* Keeps a list of saved devices (name + host + version endpoint).
* Scans all devices over HTTP, reads the reported firmware info
  (expected JSON: {"id":"LED_WS2811.ino","build":20261029041512,
  "md5":"<32 hex>"} - md5 is optional, on ESP8266/ESP32 use ESP.getSketchMD5()).
* Shows firmware id, build, and an update status per device:
    UP TO DATE | UPDATE AVAILABLE | NEWER ON DEVICE | NO BINARY LINKED | UNKNOWN FW
* Keeps a firmware registry: firmware id -> binary file + install command
  template + (optional) manual build number.
* Per-device actions: Check (re-read version), Update (run the install
  command template), Link binary (create/edit the registry entry).
* Everything is persisted to fwmanager.json next to this script.

Command template placeholders
-----------------------------
  {host}  device host/IP        {port}  device port (default 80)
  {name}  device name           {id}    firmware id being installed
  {file}  path of linked binary {build} build number of the binary
Examples:
  curl -sS -F "file=@{file}" http://{host}/update
  python espota.py -i {host} -f "{file}"
  arduino-cli upload -i "{file}" -p {host} --fqbn esp8266:esp8266:nodemcuv2

MD5 check (when the device reports "md5"):
  md5 equal to the md5 of the linked binary  -> UP TO DATE (build ignored)
  md5 different                              -> UPDATE AVAILABLE (or NEWER ON
                                                DEVICE if the device build is higher)
  no md5 reported                            -> build numbers decide, as before

Columns: right click a column header to show/hide columns (saved in the config).

Build number of a binary (first match wins):
  1. manual "build" field in the registry entry
  2. sidecar file "<binary>.json" containing {"build": ...}
  3. a 14 digit timestamp (YYYYMMDDHHMMSS) in the file name
  4. the file modification time formatted as YYYYMMDDHHMMSS

Requirements: Python 3.8+, tkinter (bundled with the standard installers).
"""

import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib import request, error
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont

# --------------------------------------------------------------------------- #
# Constants / persistence
# --------------------------------------------------------------------------- #

# Config file lives next to the script so the tool is portable.
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fwmanager.json")

# Seconds to wait for a device to answer a version request.
HTTP_TIMEOUT = 3.0

# Default install command used when a registry entry has none.
DEFAULT_CMD = 'curl -sS -F "file=@{file}" http://{host}:{port}/update'

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


# Status labels (also used as Treeview tags for coloring).
ST_OK = "UP TO DATE"
ST_UPD = "UPDATE AVAILABLE"
ST_NEWER = "NEWER ON DEVICE"
ST_NOBIN = "NO BINARY LINKED"
ST_UNKNOWN = "UNKNOWN FW"
ST_BUSY = "WORKING..."


def load_config():
    """Load config from disk; return an empty structure if missing/corrupt."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    # Guarantee both top-level keys exist.
    data.setdefault("devices", [])
    data.setdefault("firmware", {})
    return data


def save_config(cfg):
    """Write config to disk (pretty printed so it is hand-editable)."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# --------------------------------------------------------------------------- #
# Device communication
# --------------------------------------------------------------------------- #

def fetch_version(dev):
    """
    Ask a device for its firmware info over HTTP.

    Returns (fw_id, build, md5, error_text). On any failure fw_id/build/md5 are None
    and error_text explains why (shown in the Detail column).
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
        return None, None, None, "no answer (%s)" % getattr(exc, "reason", exc)

    # Preferred format: JSON object with "id" and "build".
    try:
        obj = json.loads(body)
        if isinstance(obj, dict) and obj.get("id"):
            md5 = str(obj.get("md5") or "").strip().lower()
            return str(obj["id"]), str(obj.get("build", "")), md5, ""
    except ValueError:
        pass

    # Fallback: loose regex for plain-text replies like  id=X build=123
    m_id = re.search(r'"?id"?\s*[:=]\s*"?([\w.\-]+)', body)
    m_bd = re.search(r'"?build"?\s*[:=]\s*"?(\d+)', body)
    m_md = re.search(r'"?md5"?\s*[:=]\s*"?([0-9a-fA-F]{32})', body)
    if m_id:
        return (m_id.group(1), (m_bd.group(1) if m_bd else ""),
                (m_md.group(1).lower() if m_md else ""), "")
    return None, None, None, "unrecognized reply"


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
    Modal form. fields = [(key, label, kind)], kind is "text" or "file".
    After wait_window(), .result is a dict of values or None if cancelled.
    """

    def __init__(self, parent, title, fields, values=None):
        super().__init__(parent)
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
            ttk.Entry(frm, textvariable=var, width=60).grid(
                row=row, column=1, sticky="ew", padx=5)
            if kind == "file":
                # Browse button fills the entry with a chosen file path.
                ttk.Button(frm, text="...", width=3,
                           command=lambda v=var: self._browse(v)).grid(row=row, column=2)

        btns = ttk.Frame(frm)
        btns.grid(row=len(fields), column=0, columnspan=3, pady=(10, 0), sticky="e")
        ttk.Button(btns, text="OK", command=self._ok).pack(side="left", padx=3)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left")

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window(self)

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

    DEV_COLS = ("name", "host", "fw_id", "build", "md5", "target", "status", "detail")
    FW_COLS = ("id", "file", "build", "md5", "cmd")

    def __init__(self):
        super().__init__()
        self.title("Firmware Manager")
        self.geometry("1100x640")

        self.cfg = load_config()
        # Runtime (non-persisted) state per device name:
        # {"fw_id", "build", "status", "detail"}
        self.state = {}
        # Device names currently running a job (prevents double actions).
        self.busy = set()
        # Worker threads push callables here; main thread executes them.
        self.q = queue.Queue()

        self._build_ui()
        self._refresh_devices()
        self._refresh_firmware()
        self.after(100, self._pump)      # start queue pump
        self.after(300, self.scan_all)   # auto scan on start

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        """Create notebook, toolbars, trees and the log pane."""
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True)

        nb = ttk.Notebook(paned)
        paned.add(nb, weight=4)

        # ---- Devices tab ----
        dev_tab = ttk.Frame(nb)
        nb.add(dev_tab, text="Devices")

        bar = ttk.Frame(dev_tab, padding=4)
        bar.pack(fill="x")
        for text, cmd in (("Scan all", self.scan_all),
                          ("Check", self.check_selected),
                          ("Update", self.update_selected),
                          ("Update all outdated", self.update_all_outdated),
                          ("Link binary", self.link_selected),
                          ("|", None),
                          ("Add", self.add_device),
                          ("Edit", self.edit_device),
                          ("Remove", self.remove_device)):
            if cmd is None:
                ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
            else:
                ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)

        self.dev_tree = self._make_tree(
            dev_tab, self.DEV_COLS,
            ("Name", "Host", "Reported FW", "Build", "Reported MD5", "Target FW",
             "Status", "Detail"),
            (140, 130, 160, 130, 250, 160, 140, 260), "dev", self._dev_menu)
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

        style.map("Treeview",
                  foreground=_strip_selected("foreground"),
                  background=_strip_selected("background"))

        # Bold + underlined font used for selected rows.
        self.sel_font = tkfont.nametofont("TkDefaultFont").copy()
        self.sel_font.configure(weight="bold", underline=True)
        # Bright, clearly different row colors per status. Foreground is set
        # explicitly (black) so text stays readable on a dark OS theme.
        status_colors = {
            ST_OK:      "#3ddc84",   # bright green
            ST_UPD:     "#ffd400",   # bright yellow
            ST_NEWER:   "#4da6ff",   # bright blue
            ST_NOBIN:   "#2fe0e0",   # bright cyan
            ST_UNKNOWN: "#ff4d4d",   # bright red
            ST_BUSY:    "#d28bff",   # bright purple
            "?":        "#bdbdbd",   # light gray = not checked yet
        }
        for tag, color in status_colors.items():
            self.dev_tree.tag_configure(tag, background=color, foreground="#000000")
        # Selected variants: SAME background (status color stays visible),
        # only the font changes. Created after the base tags so they have
        # higher priority in Tk's tag stacking order.
        for tag, color in status_colors.items():
            self.dev_tree.tag_configure(tag + "|sel", background=color,
                                        foreground="#000000", font=self.sel_font)
        # Re-mark rows whenever the selection changes.
        self.dev_tree.bind("<<TreeviewSelect>>", lambda e: self._update_marks())
        # Double click = check (right click is bound inside _make_tree:
        # header -> column chooser, row -> device menu).
        self.dev_tree.bind("<Double-1>", lambda e: self.check_selected())

        # ---- Firmware tab ----
        fw_tab = ttk.Frame(nb)
        nb.add(fw_tab, text="Firmware registry")

        bar2 = ttk.Frame(fw_tab, padding=4)
        bar2.pack(fill="x")
        for text, cmd in (("Add", self.add_fw), ("Edit", self.edit_fw),
                          ("Remove", self.remove_fw)):
            ttk.Button(bar2, text=text, command=cmd).pack(side="left", padx=2)

        self.fw_tree = self._make_tree(
            fw_tab, self.FW_COLS,
            ("Firmware ID", "Binary file", "Build (manual/auto)", "File MD5",
             "Install command"),
            (180, 320, 140, 250, 340), "fw")
        self.fw_tree.bind("<Double-1>", lambda e: self.edit_fw())

        # ---- Log pane ----
        log_frame = ttk.Frame(paned)
        paned.add(log_frame, weight=1)
        self.log = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

        # Context menu for devices.
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Check", command=self.check_selected)
        self.menu.add_command(label="Update", command=self.update_selected)
        self.menu.add_command(label="Link binary", command=self.link_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Edit", command=self.edit_device)
        self.menu.add_command(label="Remove", command=self.remove_device)

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

    def say(self, text):
        """Append a timestamped line to the log pane (thread safe)."""
        stamp = datetime.now().strftime("%H:%M:%S")

        def _write():
            self.log.configure(state="normal")
            self.log.insert("end", "[%s] %s\n" % (stamp, text))
            self.log.see("end")
            self.log.configure(state="disabled")
        self.ui(_write)

    # ------------------------------------------------------------ refresh --

    def _target_fw(self, dev):
        """
        Return (target_id, registry_entry) for a device.
        Target = explicit 'target' field, else the firmware id it reports.
        """
        st = self.state.get(dev["name"], {})
        tid = dev.get("target") or st.get("fw_id") or ""
        return tid, self.cfg["firmware"].get(tid)

    def _refresh_devices(self):
        """Rebuild the device tree from config + runtime state."""
        sel = self.dev_tree.selection()
        self.dev_tree.delete(*self.dev_tree.get_children())
        for dev in self.cfg["devices"]:
            st = self.state.get(dev["name"], {})
            tid, _ = self._target_fw(dev)
            status = ST_BUSY if dev["name"] in self.busy else st.get("status", "?")
            self.dev_tree.insert(
                "", "end", iid=dev["name"],
                values=(dev["name"], dev["host"],
                        st.get("fw_id") or "unknown", st.get("build", ""),
                        st.get("md5", ""),
                        tid, status, st.get("detail", "")),
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
            status = self.dev_tree.set(iid, "status")
            is_sel = iid in selected
            tags = (status, status + "|sel") if is_sel else (status,)
            self.dev_tree.item(iid, tags=tags)

    def _refresh_firmware(self):
        """Rebuild the firmware registry tree."""
        self.fw_tree.delete(*self.fw_tree.get_children())
        for fid, fw in sorted(self.cfg["firmware"].items()):
            self.fw_tree.insert("", "end", iid=fid, values=(
                fid, fw.get("file", ""),
                fw.get("build") or local_build(fw) or "-",
                file_md5(fw.get("file", "")) or "-",
                fw.get("cmd", "")))

    def _selected_devices(self):
        """Return the list of selected device dicts."""
        names = self.dev_tree.selection()
        return [d for d in self.cfg["devices"] if d["name"] in names]

    # ------------------------------------------------------------- checks --

    def _do_check(self, dev):
        """Worker body: query one device and store the resulting status."""
        fid, build, md5, err = fetch_version(dev)
        st = {"fw_id": fid, "build": build or "", "md5": md5 or "", "detail": err}
        # Compare against the (possibly overridden) target firmware.
        tid = dev.get("target") or fid or ""
        fw = self.cfg["firmware"].get(tid)
        st["status"] = compute_status(fid, build, fw, md5)
        if st["status"] == ST_NOBIN and not st["detail"]:
            st["detail"] = "no binary linked for '%s'" % tid
        elif st["status"] == ST_UPD and fw:
            if md5 and build and build == local_build(fw):
                st["detail"] = "same build, different binary (md5 differs)"
            else:
                st["detail"] = "available build %s" % local_build(fw)
        elif st["status"] == ST_NEWER and md5:
            st["detail"] = "device build is higher and md5 differs from linked binary"
        self.state[dev["name"]] = st
        self.say("%s: %s %s -> %s" % (dev["name"], fid or "unknown",
                                       build or "", st["status"]))

    def _run_checks(self, devs):
        """Check several devices in parallel, then refresh the table."""
        for d in devs:
            self.busy.add(d["name"])
        self.ui(self._refresh_devices)

        def job():
            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(self._do_check, devs))
            for d in devs:
                self.busy.discard(d["name"])
            self.ui(self._refresh_devices)
        threading.Thread(target=job, daemon=True).start()

    def scan_all(self):
        """Check every saved device."""
        devs = [d for d in self.cfg["devices"] if d["name"] not in self.busy]
        if devs:
            self.say("Scanning %d device(s)..." % len(devs))
            self._run_checks(devs)

    def check_selected(self):
        """Re-check the selected devices."""
        devs = [d for d in self._selected_devices() if d["name"] not in self.busy]
        if devs:
            self._run_checks(devs)

    # ------------------------------------------------------------ updates --

    def _install(self, dev):
        """
        Worker body: run the install command template for one device.
        Returns True when the command exited with code 0.
        """
        tid, fw = self._target_fw(dev)
        if not fw or not os.path.isfile(fw.get("file", "")):
            self.say("%s: no binary linked for '%s'" % (dev["name"], tid))
            return False
        template = fw.get("cmd") or DEFAULT_CMD
        try:
            cmd = template.format(host=dev["host"], port=dev.get("port") or 80,
                                  name=dev["name"], id=tid, file=fw["file"],
                                  build=local_build(fw))
        except (KeyError, IndexError) as exc:
            self.say("%s: bad placeholder in command: %s" % (dev["name"], exc))
            return False

        self.say("%s: running: %s" % (dev["name"], cmd))
        try:
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    errors="replace",
                                    stdin=subprocess.DEVNULL,
                                    **hidden_process_kwargs())
            # Stream the tool output into the log as it arrives.
            for line in proc.stdout:
                self.say("  %s | %s" % (dev["name"], line.rstrip()))
            code = proc.wait()
        except OSError as exc:
            self.say("%s: failed to start command: %s" % (dev["name"], exc))
            return False
        self.say("%s: command exited with code %s" % (dev["name"], code))
        return code == 0

    def _run_updates(self, devs):
        """Update devices one after another, re-checking each afterwards."""
        for d in devs:
            self.busy.add(d["name"])
        self.ui(self._refresh_devices)

        def job():
            for d in devs:
                ok = self._install(d)
                self.say("%s: update %s" % (d["name"], "finished" if ok else "FAILED"))
                # Devices usually reboot after flashing; give them a moment.
                if ok:
                    threading.Event().wait(4)
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
        if messagebox.askyesno("Update", "Install firmware on: %s ?" % names):
            self._run_updates(devs)

    def update_all_outdated(self):
        """Update every device whose status is UPDATE AVAILABLE."""
        devs = [d for d in self.cfg["devices"]
                if self.state.get(d["name"], {}).get("status") == ST_UPD
                and d["name"] not in self.busy]
        if not devs:
            self.say("Nothing to update.")
            return
        names = ", ".join(d["name"] for d in devs)
        if messagebox.askyesno("Update all", "Update: %s ?" % names):
            self._run_updates(devs)

    # ----------------------------------------------------- device editing --

    DEV_FIELDS = [("name", "Name", "text"), ("host", "Host / IP", "text"),
                  ("port", "Port (default 80)", "text"),
                  ("version_path", "Version path (default /version)", "text"),
                  ("target", "Target FW id (empty = same as reported)", "text")]

    def add_device(self):
        """Add a new device and check it immediately."""
        dlg = FormDialog(self, "Add device", self.DEV_FIELDS)
        self._store_device(dlg.result, None)

    def edit_device(self):
        """Edit the first selected device."""
        sel = self._selected_devices()
        if not sel:
            return
        dlg = FormDialog(self, "Edit device", self.DEV_FIELDS, sel[0])
        self._store_device(dlg.result, sel[0])

    def _store_device(self, res, old):
        """Validate and save a device form result (old = device being edited)."""
        if not res or not res["name"] or not res["host"]:
            return
        # Names are the unique key; refuse duplicates.
        if any(d["name"] == res["name"] and d is not old for d in self.cfg["devices"]):
            messagebox.showerror("Error", "Name already exists.")
            return
        # Keep port numeric.
        res["port"] = int(res["port"]) if res["port"].isdigit() else ""
        if old is None:
            self.cfg["devices"].append(res)
        else:
            self.state.pop(old["name"], None)
            old.clear()
            old.update(res)
        save_config(self.cfg)
        self._refresh_devices()
        self._run_checks([res if old is None else old])

    def remove_device(self):
        """Remove the selected devices from the saved list."""
        sel = self._selected_devices()
        if sel and messagebox.askyesno("Remove", "Remove %d device(s)?" % len(sel)):
            for d in sel:
                self.cfg["devices"].remove(d)
                self.state.pop(d["name"], None)
            save_config(self.cfg)
            self._refresh_devices()

    # ----------------------------------------------------- firmware editing --

    FW_FIELDS = [("id", "Firmware id", "text"), ("file", "Binary file", "file"),
                 ("build", "Build (empty = auto)", "text"),
                 ("cmd", "Install command template", "text")]

    def _store_fw(self, res, old_id=None):
        """Save a firmware form result, handling renames."""
        if not res or not res["id"]:
            return
        if old_id and old_id != res["id"]:
            self.cfg["firmware"].pop(old_id, None)
        # Pre-fill the default command so it is visible/editable later.
        res["cmd"] = res["cmd"] or DEFAULT_CMD
        self.cfg["firmware"][res["id"]] = {k: res[k] for k in ("file", "build", "cmd")}
        save_config(self.cfg)
        self._refresh_firmware()
        # Statuses may change now that a binary is linked.
        self.scan_all()

    def add_fw(self):
        """Add a registry entry."""
        dlg = FormDialog(self, "Add firmware", self.FW_FIELDS, {"cmd": DEFAULT_CMD})
        self._store_fw(dlg.result)

    def edit_fw(self):
        """Edit the selected registry entry."""
        sel = self.fw_tree.selection()
        if not sel:
            return
        vals = dict(self.cfg["firmware"][sel[0]], id=sel[0])
        dlg = FormDialog(self, "Edit firmware", self.FW_FIELDS, vals)
        self._store_fw(dlg.result, sel[0])

    def remove_fw(self):
        """Remove the selected registry entries."""
        sel = self.fw_tree.selection()
        if sel and messagebox.askyesno("Remove", "Remove %d entr(y/ies)?" % len(sel)):
            for fid in sel:
                self.cfg["firmware"].pop(fid, None)
            save_config(self.cfg)
            self._refresh_firmware()
            self.scan_all()

    def link_selected(self):
        """Create/edit the registry entry for the selected device's target FW."""
        sel = self._selected_devices()
        if not sel:
            return
        tid, fw = self._target_fw(sel[0])
        if not tid:
            messagebox.showinfo("Link", "Device FW id unknown. Set a Target FW id "
                                        "via Edit, or add it in the Firmware tab.")
            return
        vals = dict(fw or {"cmd": DEFAULT_CMD}, id=tid)
        dlg = FormDialog(self, "Link binary for %s" % tid, self.FW_FIELDS, vals)
        self._store_fw(dlg.result, tid)


if __name__ == "__main__":
    # Hide the python.exe console so only the GUI window is visible.
    hide_own_console()
    App().mainloop()
