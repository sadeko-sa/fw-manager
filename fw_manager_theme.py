#!/usr/bin/env python3
"""
fw_manager_theme.py - Themes for fw_manager.py.

Replaces fw_manager_colors.py (delete that file). Holds the status ids, the
theme files in theme/, applying the selected theme to Tk / ttk and the
Settings > Theme editor.

One theme = one JSON file in theme/ next to this script: theme/<code>.json
(dark_os.json = system widgets with the program's own colors, dark.json =
everything dark). A theme holds EVERY color the program uses plus the ttk
theme it is drawn with. Nothing is hardcoded in the program: the colors come
only from the selected theme. An empty or invalid value means "not set", and
the system / Tk draws that part itself.

Theme file layout (any entry may be missing, it then counts as empty):
    {
      "theme_name": "Dark OS",            display name in Settings > Theme
      "ttk_theme": "clam",                ttk theme to switch to, "" = keep the system one
      "palette":   {name: color, ...},    window and widget colors (see GROUPS)
      "log":       {name: color, ...},    log line text
      "status":    {id: color, ...},      device row background per status
      "table":     {name: color, ...},    device row text
      "selection": {name: color, ...}     selected firmware row
    }
Colors are #rrggbb or a Tk color name. Only the names listed in GROUPS are read
and written; other entries in a theme file are dropped when the editor saves it.

The selected theme code is saved in the config as "theme" (Settings > Theme).
A theme is used from the next program start. A theme file is edited in
Settings > Theme > Edit theme... or by hand while the program is closed.

The names of the colors and groups are the entries "<group>.<name>" and
"group.<group>" of lang/<code>.json (see fw_manager_lang.py); a missing entry
shows the id instead of a description.
"""

import json
import os
import re
import tkinter as tk
from tkinter import ttk, messagebox, colorchooser

from fw_manager_lang import t

# --------------------------------------------------------------------------- #
# Status ids
# --------------------------------------------------------------------------- #

# Status ids (lowercase, no spaces; also used as Treeview tags for coloring).
ST_OK = "up_to_date"
"""Status: device runs the linked binary."""
ST_UPD = "update_available"
"""Status: linked binary differs from the device firmware."""
ST_NEWER = "newer_on_device"
"""Status: device build is higher than the linked binary."""
ST_NOBIN = "no_binary_linked"
"""Status: no registry entry or binary file for the device firmware."""
ST_UNKNOWN = "unknown_fw"
"""Status: device reported no firmware id."""
ST_BUSY = "working"
"""Status: a check or upload is running."""
ST_DISABLED = "disabled"
"""Status: device is disabled."""
ST_UNCHECKED = "unchecked"
"""Status: device has not been checked yet."""

STATUS_IDS = (ST_OK, ST_UPD, ST_NEWER, ST_NOBIN, ST_UNKNOWN, ST_BUSY,
              ST_DISABLED, ST_UNCHECKED)
"""Every status id, in display order."""

STATUS_TEXT = {}
"""Text shown in the Status column and the log for each status id (see refresh_status_text)."""

STATUS_ID = {}
"""Reverse of STATUS_TEXT: status column text -> status id."""


def refresh_status_text():
    """
    Fill STATUS_TEXT and STATUS_ID from the active language ("state.<id>" entries).
    Both dicts are changed in place, so modules that imported them see the new text.
    Call it after set_language().
    """
    STATUS_TEXT.clear()
    STATUS_TEXT.update({sid: t("state." + sid) for sid in STATUS_IDS})
    STATUS_ID.clear()
    STATUS_ID.update({text: sid for sid, text in STATUS_TEXT.items()})


refresh_status_text()

# --------------------------------------------------------------------------- #
# Theme structure
# --------------------------------------------------------------------------- #

GROUPS = {
    # Window and widget colors. Used when a theme sets any of them; if every
    # entry is empty the system look is left completely alone.
    "palette": ("bg", "surface", "surface_hi", "surface_press", "field",
                "fg", "fg_disabled", "border", "select_bg", "select_fg",
                "thumb"),
    # Log line text per kind of line.
    "log": ("err", "warn", "ok", "info", "dim"),
    # Device table row background per status.
    "status": STATUS_IDS,
    # Device table row text.
    "table": ("text", "disabled_text"),
    # Selected row in the firmware table.
    "selection": ("text", "background"),
}
"""Every color of a theme: group -> color names. These are ids, not colors."""

LOG_KINDS = GROUPS["log"]
"""Names of the log line kinds (tags of the log pane)."""

THEME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme")
"""Folder with one JSON file per theme, named <code>.json (dark_os.json, dark.json, ...)."""

DEFAULT_THEME = "dark_os"
"""Theme code used when the config has none (or its theme file is gone)."""

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
"""Allowed theme codes (file names without .json)."""

_active = {"code": "", "data": None}
"""The theme in use: its code and its normalized data (see set_theme)."""


def _text(value):
    """
    Clean up one value read from a theme file.

    @param value  anything read from JSON
    @return       the stripped string, or "" if value is not a string
    """
    return value.strip() if isinstance(value, str) else ""


def _normalize(data):
    """
    Complete a theme read from a file: every entry of GROUPS exists and every
    value is a string ("" = not set).

    @param data  parsed JSON (any type; anything but a dict counts as empty)
    @return      {"theme_name", "ttk_theme", <group>: {<name>: color}}
    """
    if not isinstance(data, dict):
        data = {}
    out = {"theme_name": _text(data.get("theme_name")),
           "ttk_theme": _text(data.get("ttk_theme"))}
    for group, names in GROUPS.items():
        sub = data.get(group)
        if not isinstance(sub, dict):
            sub = {}
        out[group] = {name: _text(sub.get(name)) for name in names}
    return out


_active["data"] = _normalize({})

# --------------------------------------------------------------------------- #
# Theme files
# --------------------------------------------------------------------------- #


def theme_path(code):
    """
    Path of the file of a theme.

    @param code  theme code, e.g. "dark_os"
    @return      path of <code>.json inside THEME_DIR
    """
    return os.path.join(THEME_DIR, code + ".json")


def _read(code):
    """
    Read the raw JSON of one theme file.

    @param code  theme code
    @return      the parsed dict; empty if the file is missing or invalid
    """
    try:
        with open(theme_path(code), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_theme(code):
    """
    Read one theme file.

    @param code  theme code
    @return      the normalized theme (see _normalize); every value is "" if the
                 file is missing or invalid
    """
    return _normalize(_read(code))


def save_theme(code, data):
    """
    Write a theme to theme/<code>.json (created if it does not exist). The file
    is written to a temporary name first, so a failed write never leaves a
    half written theme.

    @param code  theme code (letters, digits, _ and -)
    @param data  theme in the layout of _normalize
    @raise OSError  if the file cannot be written
    """
    os.makedirs(THEME_DIR, exist_ok=True)
    path = theme_path(code)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_normalize(data), f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def list_themes():
    """
    Find the available themes.

    @return  list of (code, display name) sorted by display name; the name is the
             "theme_name" entry of the file, or the code if it has none
    """
    found = []
    try:
        files = sorted(os.listdir(THEME_DIR))
    except OSError:
        return found
    for fn in files:
        if not fn.lower().endswith(".json"):
            continue
        code = fn[:-5]
        name = _read(code).get("theme_name")
        found.append((code, name if isinstance(name, str) and name else code))
    return sorted(found, key=lambda item: item[1].lower())


def resolve_theme(code):
    """
    Check a theme code without activating it.

    @param code  theme code; may be empty or unknown
    @return      code if its file exists and is valid, else DEFAULT_THEME if that
                 file is valid, else the first valid theme, else "" (no theme
                 files: every color is left to the system)
    """
    if isinstance(code, str) and code and _read(code):
        return code
    if _read(DEFAULT_THEME):
        return DEFAULT_THEME
    for other, _name in list_themes():
        if _read(other):
            return other
    return ""


def set_theme(code):
    """
    Make a theme the active one (the one apply_theme and resolve_color use).

    @param code  theme code; empty or unknown means resolve_theme's fallback
    @return      the code that is now active
    """
    code = resolve_theme(code)
    _active["code"] = code
    _active["data"] = load_theme(code) if code else _normalize({})
    return code


def active_theme():
    """
    @return  code of the active theme ("" if there is none)
    """
    return _active["code"]


def drop_legacy_colors(cfg):
    """
    Remove the color settings older versions kept in the config ("colors",
    "dark_theme" and ui "log_colors"). Colors now live only in the theme files.

    @param cfg  config dict, changed in place
    @return     True if anything was removed
    """
    changed = False
    for key in ("colors", "dark_theme"):
        if key in cfg:
            del cfg[key]
            changed = True
    ui = cfg.get("ui")
    if isinstance(ui, dict) and "log_colors" in ui:
        del ui["log_colors"]
        changed = True
    return changed


def resolve_color(widget, group, name):
    """
    Color of the active theme if Tk accepts it.

    @param widget  any Tk widget, used to test the color
    @param group   color group, e.g. "status"
    @param name    color name inside the group
    @return        the color string, or "" if the theme leaves it empty or Tk
                   rejects it ("" = not set, Tk / the system decides)
    """
    value = _active["data"][group][name]
    if not value:
        return ""
    try:
        widget.winfo_rgb(value)
    except tk.TclError:
        return ""
    return value


def color_label(group, name):
    """
    Description of one color for the dialog.

    @param group  color group, e.g. "log"
    @param name   color name inside the group, e.g. "err"
    @return       the active language text for "group.name", or name if there is none
    """
    key = "%s.%s" % (group, name)
    text = t(key)
    return name if text == key else text


def group_label(group):
    """
    Title of one color group for the dialog.

    @param group  color group, e.g. "status"
    @return       the active language text for "group.<group>", or group if there is none
    """
    key = "group." + group
    text = t(key)
    return group if text == key else text


# --------------------------------------------------------------------------- #
# Applying a theme
# --------------------------------------------------------------------------- #


def _cfg(style, name, **opts):
    """
    style.configure(name, ...) with only the options that have a value, so an
    empty color leaves that option to the system.

    @param style  the ttk.Style
    @param name   style name, e.g. "TButton"
    @param opts   options; "" and None are skipped
    """
    opts = {k: v for k, v in opts.items() if v not in ("", None)}
    if opts:
        style.configure(name, **opts)


def _map(style, name, **opts):
    """
    style.map(name, ...) with only the (state, color) pairs that have a color.

    @param style  the ttk.Style
    @param name   style name, e.g. "TButton"
    @param opts   option -> [(state, color), ...]; pairs with an empty color are skipped
    """
    clean = {}
    for opt, pairs in opts.items():
        keep = [(state, color) for state, color in pairs if color]
        if keep:
            clean[opt] = keep
    if clean:
        style.map(name, **clean)


def _style_ttk(style, p):
    """
    Recolor every ttk widget class the program uses.

    @param style  the ttk.Style
    @param p      palette colors {name: color}; "" = not set
    """
    bg, surface, hi, press = p["bg"], p["surface"], p["surface_hi"], p["surface_press"]
    field, fg, off, border = p["field"], p["fg"], p["fg_disabled"], p["border"]
    sel_bg, sel_fg, thumb = p["select_bg"], p["select_fg"], p["thumb"]

    # Defaults for every ttk widget; the specific entries below refine them.
    _cfg(style, ".", background=bg, foreground=fg, fieldbackground=field,
         bordercolor=border, darkcolor=bg, lightcolor=bg, troughcolor=field,
         selectbackground=sel_bg, selectforeground=sel_fg, insertcolor=fg,
         focuscolor=bg)
    _map(style, ".", foreground=[("disabled", off)])

    # Containers and static widgets.
    _cfg(style, "TFrame", background=bg)
    _cfg(style, "TLabel", background=bg, foreground=fg)
    _cfg(style, "TLabelframe", background=bg, bordercolor=border,
         darkcolor=bg, lightcolor=bg)
    _cfg(style, "TLabelframe.Label", background=bg, foreground=fg)
    _cfg(style, "TSeparator", background=border)
    _cfg(style, "TPanedwindow", background=bg)
    # The draggable divider between the tabs and the log pane.
    _cfg(style, "Sash", sashthickness=6, gripcount=0, background=border)

    # Push buttons (toolbar, dialogs).
    _cfg(style, "TButton", background=surface, foreground=fg,
         bordercolor=border, darkcolor=surface, lightcolor=surface,
         padding=(8, 3))
    _map(style, "TButton",
         background=[("disabled", bg), ("pressed", press), ("active", hi)],
         darkcolor=[("pressed", press), ("active", hi)],
         lightcolor=[("pressed", press), ("active", hi)],
         foreground=[("disabled", off)])

    # Check / radio buttons (not used today, themed for future dialogs).
    for name in ("TCheckbutton", "TRadiobutton"):
        _cfg(style, name, background=bg, foreground=fg,
             indicatorbackground=field, indicatorforeground=fg)
        _map(style, name,
             background=[("active", bg)],
             indicatorbackground=[("pressed", press), ("selected", sel_bg)])

    # Single line text input.
    _cfg(style, "TEntry", fieldbackground=field, foreground=fg,
         insertcolor=fg, bordercolor=border, darkcolor=field, lightcolor=field)
    _map(style, "TEntry",
         fieldbackground=[("disabled", bg), ("readonly", surface)],
         foreground=[("disabled", off)])

    # Drop-down box (send mode, dialog pickers).
    _cfg(style, "TCombobox", fieldbackground=field, background=surface,
         foreground=fg, arrowcolor=fg, insertcolor=fg, bordercolor=border,
         darkcolor=field, lightcolor=field, selectbackground=field,
         selectforeground=fg)
    _map(style, "TCombobox",
         fieldbackground=[("readonly", field), ("disabled", bg)],
         background=[("active", hi), ("pressed", press)],
         foreground=[("disabled", off)],
         arrowcolor=[("disabled", off)],
         selectbackground=[("readonly", field)],
         selectforeground=[("readonly", fg)])

    # Tabs (Devices / Firmware).
    _cfg(style, "TNotebook", background=bg, bordercolor=border,
         tabmargins=(2, 3, 2, 0))
    _cfg(style, "TNotebook.Tab", background=surface, foreground=fg,
         bordercolor=border, padding=(12, 4))
    _map(style, "TNotebook.Tab",
         background=[("selected", bg), ("active", hi)],
         foreground=[("selected", fg)])

    # Scrollbars (trees, log pane, dialogs).
    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        _cfg(style, name, background=thumb, troughcolor=field,
             bordercolor=field, arrowcolor=fg, darkcolor=thumb,
             lightcolor=thumb)
        _map(style, name, background=[("active", press)])

    # Tables (device list, firmware registry). Per-row status colors are set
    # by fw_manager.py with tag_configure and keep winning over these.
    _cfg(style, "Treeview", background=field, fieldbackground=field,
         foreground=fg, bordercolor=border, darkcolor=field, lightcolor=field,
         rowheight=22)
    _cfg(style, "Treeview.Heading", background=surface, foreground=fg,
         bordercolor=border, darkcolor=surface, lightcolor=surface,
         relief="flat", padding=(6, 3))
    _map(style, "Treeview.Heading", background=[("active", hi)])
    _map(style, "Treeview",
         background=[("selected", sel_bg)],
         foreground=[("selected", sel_fg)])


def _style_classic(root, p):
    """
    Fill the Tk option database so classic (non-ttk) widgets are created with
    the palette. Only widgets created AFTER this call are affected.

    @param root  the Tk root window (also recolored directly)
    @param p     palette colors {name: color}; "" = not set
    """
    # Root window background (visible in any gap between widgets).
    if p["bg"]:
        root.configure(bg=p["bg"])

    def opt(pattern, value):
        """
        Set one option database entry (skipped if the value is empty).
        Priority 80 = "interactive": beats the widget defaults, but code that
        passes bg= / fg= explicitly still wins.
        """
        if value not in ("", None):
            root.option_add(pattern, value, 80)

    # Dialog windows.
    opt("*Toplevel.background", p["bg"])

    # Log pane (tk.Text): field colors, caret, selection.
    opt("*Text.background", p["field"])
    opt("*Text.foreground", p["fg"])
    opt("*Text.insertBackground", p["fg"])
    opt("*Text.selectBackground", p["select_bg"])
    opt("*Text.selectForeground", p["select_fg"])
    opt("*Text.highlightThickness", 0)
    opt("*Text.relief", "flat")
    opt("*Text.borderWidth", 0)

    # Drop-down and context menus (tk.Menu). A menu bar strip that the window
    # system draws itself ignores these; the menus that open from it use them.
    opt("*Menu.background", p["surface"])
    opt("*Menu.foreground", p["fg"])
    opt("*Menu.activeBackground", p["select_bg"])
    opt("*Menu.activeForeground", p["select_fg"])
    opt("*Menu.disabledForeground", p["fg_disabled"])
    opt("*Menu.borderWidth", 0)
    opt("*Menu.relief", "flat")

    # The list that pops up under a ttk.Combobox is a tk.Listbox.
    for pattern in ("*TCombobox*Listbox", "*Listbox"):
        opt(pattern + ".background", p["field"])
        opt(pattern + ".foreground", p["fg"])
        opt(pattern + ".selectBackground", p["select_bg"])
        opt(pattern + ".selectForeground", p["select_fg"])

    # Classic widgets some dialogs use (swatches, plain labels).
    opt("*Label.background", p["bg"])
    opt("*Label.foreground", p["fg"])
    opt("*Frame.background", p["bg"])
    opt("*Canvas.background", p["bg"])
    opt("*Canvas.highlightThickness", 0)


def apply_theme(root):
    """
    Draw the program with the active theme (see set_theme). Call once, right
    after the root window exists and BEFORE any widget is created: the Tk
    option database is only read when a widget is created.

    The ttk theme named by the theme file is switched to first (empty = keep the
    system one). The palette is applied only if the theme sets at least one
    palette color, so a theme with an empty palette keeps the system look.

    @param root  the Tk root window (the App instance)
    """
    style = ttk.Style(root)
    base = _active["data"]["ttk_theme"]
    if base and base in style.theme_names():
        style.theme_use(base)
    pal = {name: resolve_color(root, "palette", name) for name in GROUPS["palette"]}
    if any(pal.values()):
        _style_ttk(style, pal)
        _style_classic(root, pal)


# --------------------------------------------------------------------------- #
# Settings > Theme > Edit theme...
# --------------------------------------------------------------------------- #


class ThemeDialog(tk.Toplevel):
    """
    Modal editor for every value of a theme file: the theme file (pick an
    existing one, or type a new name to save a copy), its display name, its
    ttk theme and one scrollable column with one section per color group.
    Each color row has a description, the id (group.name), a swatch, a text
    entry (#rrggbb, a color name, or empty = system) and a Pick button for the
    system color chooser.
    Picking another theme file loads it (asking first if there are unsaved
    edits). OK writes the file; Revert reloads the file from disk.
    After wait_window(), .result is the code of the saved theme, or None if
    the dialog was cancelled.
    """

    def __init__(self, parent, code):
        """
        Build and show the dialog; returns when it is closed.

        @param parent  owner window
        @param code    theme code to edit first (the selected theme)
        """
        super().__init__(parent)
        self.withdraw()
        self.title(t("dlg.theme"))
        self.transient(parent)
        self.result = None
        self.codes = [c for c, _name in list_themes()]
        # Theme file loaded in the entries (the file Revert reloads).
        self.code = code if code in self.codes else (self.codes[0] if self.codes else "")
        self.snapshot = None          # values as loaded, to detect unsaved edits
        self.vars = {}                # (group, name) -> StringVar
        self.swatches = {}            # (group, name) -> Label
        self.theme_var = tk.StringVar(value=self.code)
        self.name_var = tk.StringVar()
        self.base_var = tk.StringVar()
        self.plain = self.cget("background")    # swatch color when empty / invalid
        # Same label widths in every section so the columns line up.
        desc_w = max(len(color_label(g, n))
                     for g, names in GROUPS.items() for n in names)
        id_w = max(len("%s.%s" % (g, n))
                   for g, names in GROUPS.items() for n in names)

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        # Buttons are packed first (at the bottom) so they stay visible while
        # the list scrolls.
        btns = ttk.Frame(frm)
        btns.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Button(btns, text=t("btn.revert"), command=self._revert).pack(side="left")
        ttk.Button(btns, text=t("btn.cancel"), command=self.destroy).pack(side="right")
        ttk.Button(btns, text=t("btn.ok"), command=self._ok).pack(side="right", padx=3)

        # Header: theme file, display name, ttk theme.
        head = ttk.Frame(frm)
        head.pack(side="top", fill="x", pady=(0, 8))
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text=t("field.theme")).grid(row=0, column=0, sticky="w", pady=3)
        box = ttk.Combobox(head, textvariable=self.theme_var, values=self.codes)
        box.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        box.bind("<<ComboboxSelected>>", self._on_pick_theme)
        ttk.Label(head, text=t("field.theme_name")).grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(head, textvariable=self.name_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(head, text=t("field.theme_base")).grid(row=2, column=0, sticky="w", pady=3)
        ttk.Combobox(head, textvariable=self.base_var,
                     values=[""] + list(ttk.Style(self).theme_names())).grid(
            row=2, column=1, sticky="ew", padx=(8, 0))

        # Scrollable area: canvas + scrollbar, the sections live in `body`.
        area = ttk.Frame(frm)
        area.pack(side="top", fill="both", expand=True)
        bg = ttk.Style(self).lookup("TFrame", "background") or self.plain
        self.canvas = tk.Canvas(area, highlightthickness=0, background=bg,
                                yscrollincrement=30)
        sb = ttk.Scrollbar(area, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        for group, names in GROUPS.items():
            tab = ttk.LabelFrame(body, text=group_label(group), padding=8)
            tab.pack(fill="x", pady=(0, 8), padx=2)
            for row, name in enumerate(names):
                key = (group, name)
                ttk.Label(tab, text=color_label(group, name),
                          width=desc_w).grid(row=row, column=0, sticky="w", pady=3)
                # The id: group.name, exactly as used in the code and the JSON.
                ttk.Label(tab, text="%s.%s" % (group, name), width=id_w).grid(
                    row=row, column=1, sticky="w", padx=(12, 0))
                box = tk.Label(tab, width=8, relief="solid", borderwidth=1)
                box.grid(row=row, column=2, padx=8)
                self.swatches[key] = box
                var = tk.StringVar()
                self.vars[key] = var
                var.trace_add("write", lambda *a, k=key: self._swatch(k))
                ttk.Entry(tab, textvariable=var, width=12).grid(row=row, column=3)
                ttk.Button(tab, text=t("btn.pick"),
                           command=lambda g=group, n=name: self._pick(g, n)
                           ).grid(row=row, column=4, padx=(5, 0))

        self._load(self.code)

        # Fit the canvas to the list, but never taller than 60% of the screen.
        self.update_idletasks()
        self.canvas.configure(
            width=body.winfo_reqwidth(),
            height=min(body.winfo_reqheight(), int(self.winfo_screenheight() * 0.6)))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind(seq, self._wheel)

        self.bind("<Escape>", lambda e: self.destroy())
        self._center(parent)
        self.deiconify()
        self.grab_set()
        self.wait_window(self)

    def _wheel(self, event):
        """
        Scroll the list with the mouse wheel.

        @param event  Tk wheel event (Button-4/5 on X11, delta elsewhere)
        """
        if event.num == 4:          # X11 wheel up
            step = -1
        elif event.num == 5:        # X11 wheel down
            step = 1
        else:                       # delta based wheel
            step = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(step, "units")

    def _center(self, parent):
        """
        Place the dialog in the middle of its parent window.

        @param parent  window to center on
        """
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))

    def _valid(self, color):
        """
        Test a color string.

        @param color  #rrggbb, a color name, or empty
        @return       True if it is empty (= system) or Tk understands it
        """
        if not color:
            return True
        try:
            self.winfo_rgb(color)
        except tk.TclError:
            return False
        return True

    def _swatch(self, key):
        """
        Fill one swatch with its entry's color ("system" if empty, "invalid" if bad).

        @param key  (group, name) of the row
        """
        color = self.vars[key].get().strip()
        if not color:
            self.swatches[key].configure(background=self.plain, text=t("val.unset"))
        elif self._valid(color):
            self.swatches[key].configure(background=color, text="")
        else:
            self.swatches[key].configure(background=self.plain, text=t("val.invalid"))

    def _pick(self, group, name):
        """
        Open the system color chooser and put the result in the entry.

        @param group  color group of the row
        @param name   color name of the row
        """
        current = self.vars[(group, name)].get().strip()
        opts = {"color": current} if current and self._valid(current) else {}
        _rgb, hexval = colorchooser.askcolor(
            parent=self, title=color_label(group, name), **opts)
        if hexval:
            self.vars[(group, name)].set(hexval)

    def _collect(self):
        """
        @return  everything shown in the dialog as a theme dict (see _normalize)
        """
        out = {"theme_name": self.name_var.get().strip(),
               "ttk_theme": self.base_var.get().strip()}
        for group, names in GROUPS.items():
            out[group] = {n: self.vars[(group, n)].get().strip() for n in names}
        return out

    def _load(self, code):
        """
        Show a theme file in the entries (all empty if code is empty or the file
        is missing).

        @param code  theme code to load
        """
        data = load_theme(code) if code else _normalize({})
        self.name_var.set(data["theme_name"])
        self.base_var.set(data["ttk_theme"])
        for (group, name), var in self.vars.items():
            var.set(data[group][name])
        self.snapshot = self._collect()

    def _dirty(self):
        """
        @return  True if the entries differ from what was loaded
        """
        return self._collect() != self.snapshot

    def _on_pick_theme(self, _event=None):
        """
        The user picked a theme file in the list: load it, after asking if there
        are unsaved edits.

        @param _event  Tk event (unused)
        """
        new = self.theme_var.get().strip()
        if self._dirty() and not messagebox.askyesno(
                t("dlg.theme"), t("msg.discard"), parent=self):
            self.theme_var.set(self.code)
            return
        self.code = new
        self._load(new)

    def _revert(self):
        """Reload the loaded theme file from disk, dropping all edits."""
        self.theme_var.set(self.code)
        self._load(self.code)

    def _ok(self):
        """Check every entry, write the theme file, close with its code in self.result."""
        code = self.theme_var.get().strip()
        if not NAME_RE.match(code):
            messagebox.showerror(t("dlg.theme"), t("msg.theme_bad_name"), parent=self)
            return
        data = self._collect()
        bad = ["%s.%s  (%s)" % (g, n, color_label(g, n))
               for g, names in GROUPS.items() for n in names
               if not self._valid(data[g][n])]
        if bad:
            messagebox.showerror(t("dlg.theme"),
                                 t("msg.theme_invalid", list="\n  ".join(bad)),
                                 parent=self)
            return
        try:
            save_theme(code, data)
        except OSError as exc:
            messagebox.showerror(t("dlg.theme"),
                                 t("msg.theme_save_failed", error=exc), parent=self)
            return
        self.result = code
        self.destroy()
