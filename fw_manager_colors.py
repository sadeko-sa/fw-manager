#!/usr/bin/env python3
"""
fw_manager_colors.py - Color settings for fw_manager.py.

Holds the status constants, the built-in default colors, the config helpers
(seed / read) and the Settings > Colors... dialog. All text (the status texts,
the dialog text and the description next to each color id) comes from the
language files in lang/ through fw_manager_lang.py.

The color descriptions are the entries "<group>.<n>" of lang/<code>.json.
A missing entry shows the color id instead of a description.

Config layout (fw_manager.json, key "colors"): {group: {name: color}} with the
same groups and names as DEFAULT_COLORS. Values are #rrggbb or a Tk color name.
"""

import tkinter as tk
from tkinter import ttk, messagebox, colorchooser

from fw_manager_lang import t

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

LEGACY_STATUS_IDS = {
    "UP TO DATE": ST_OK,
    "UPDATE AVAILABLE": ST_UPD,
    "NEWER ON DEVICE": ST_NEWER,
    "NO BINARY LINKED": ST_NOBIN,
    "UNKNOWN FW": ST_UNKNOWN,
    "WORKING...": ST_BUSY,
    "DISABLED": ST_DISABLED,
    "?": ST_UNCHECKED,
    "NOT CHECKED": ST_UNCHECKED,
    "UNCHECKED": ST_UNCHECKED,
}
"""Status ids used by older configs -> current status ids (migrated by seed_colors)."""

# Every color the program uses, in groups. These are the built-in defaults;
# the user's values live in the config under "colors" with the same layout.
DEFAULT_COLORS = {
    # Log line text (light colors, for a dark log).
    "log": {
        "err":  "#c75450",    # red: failures
        "warn": "#ffb454",    # amber: needs attention
        "ok":   "#6fdc8c",    # green: success
        "info": "#6cb6ff",    # blue: sent text, upload command
        "dim":  "#8b949e",    # gray: timestamps, raw tool output
    },
    # Device table row background per status.
    "status": {
        ST_OK:       "#1b5e38",   # dark green
        ST_UPD:      "#6b5a12",   # dark yellow
        ST_NEWER:    "#44601f",   # dark olive green
        ST_NOBIN:    "#6b1f1f",   # dark red
        ST_UNKNOWN:  "#6b1f1f",   # dark red
        ST_BUSY:     "#4a2f73",   # dark purple
        ST_UNCHECKED: "#3a3a3a",  # dark gray = not checked yet
        ST_DISABLED: "#262626",   # almost black
    },
    # Device table row text.
    "table": {
        "text":          "#d4d4d4",   # soft light gray
        "disabled_text": "#808080",   # dimmed, for DISABLED rows
    },
    # Selected row in the firmware table.
    "selection": {
        "text":       "#ffffff",
        "background": "#1f6feb",
    },
}
"""Built-in default color for every (group, name); the config uses the same layout."""

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


def seed_colors(cfg):
    """
    Make sure cfg["colors"] lists every color (group -> name -> color) so they
    can be seen and edited in the config file. Colors saved by an earlier
    version under cfg["ui"]["log_colors"] are moved into cfg["colors"]["log"].

    @param cfg  config dict, changed in place
    @return     True if anything was added or moved
    """
    changed = False
    colors = cfg.get("colors")
    if not isinstance(colors, dict):
        colors = cfg["colors"] = {}
        changed = True
    ui = cfg.get("ui")
    old = ui.pop("log_colors", None) if isinstance(ui, dict) else None
    if old is not None:
        changed = True
    for group, defaults in DEFAULT_COLORS.items():
        sub = colors.get(group)
        if not isinstance(sub, dict):
            sub = colors[group] = {}
            changed = True
        if group == "status":
            # Older configs use ids with spaces or capitals (see LEGACY_STATUS_IDS).
            for old_id, new_id in LEGACY_STATUS_IDS.items():
                if old_id in sub:
                    sub.setdefault(new_id, sub.pop(old_id))
                    changed = True
        if group == "log" and isinstance(old, dict):
            for name, color in old.items():
                if name in defaults and name not in sub:
                    sub[name] = color
        for name, color in defaults.items():
            if name not in sub:
                sub[name] = color
                changed = True
    return changed


def cfg_color(cfg, group, name):
    """
    Color for (group, name) from the config.

    @param cfg    config dict
    @param group  color group, e.g. "status"
    @param name   color name inside the group
    @return       the configured string, or the built-in default if it is missing
    """
    default = DEFAULT_COLORS[group][name]
    try:
        value = cfg["colors"][group][name]
    except (KeyError, TypeError):
        return default
    return value if isinstance(value, str) else default


def resolve_color(widget, cfg, group, name):
    """
    Configured color if Tk accepts it.

    @param widget  any Tk widget, used to test the color
    @param cfg     config dict
    @param group   color group
    @param name    color name inside the group
    @return        the configured color, or the built-in default if Tk rejects it
    """
    value = cfg_color(cfg, group, name)
    try:
        widget.winfo_rgb(value)
    except tk.TclError:
        return DEFAULT_COLORS[group][name]
    return value


def current_colors(cfg):
    """
    Every color as currently configured.

    @param cfg  config dict
    @return     {group: {name: color}} with the same layout as DEFAULT_COLORS
    """
    return {g: {n: cfg_color(cfg, g, n) for n in names}
            for g, names in DEFAULT_COLORS.items()}


class ColorsDialog(tk.Toplevel):
    """
    Modal editor for every program color: one scrollable column with one
    section per group (the first part of the id).
    colors = {group: {name: color}} (same layout as DEFAULT_COLORS).
    Each row has a description, the id, a swatch of the color, a text entry
    (#rrggbb or a color name) and a Pick button for the system color chooser.
    The swatches only live inside this dialog; nothing in the main window
    changes. After wait_window(), .result is the new colors dict or None if
    cancelled.
    """

    def __init__(self, parent, colors):
        """
        Build and show the dialog; returns when it is closed.

        @param parent  owner window
        @param colors  {group: {name: color}} shown in the entries
        """
        super().__init__(parent)
        self.withdraw()
        self.title(t("dlg.colors"))
        self.transient(parent)
        self.result = None
        self.vars = {}        # (group, name) -> StringVar
        self.swatches = {}    # (group, name) -> Label
        self.plain = self.cget("background")    # swatch color when invalid
        # Same label widths in every section so the columns line up.
        desc_w = max(len(color_label(g, n))
                     for g, names in DEFAULT_COLORS.items() for n in names)
        id_w = max(len("%s.%s" % (g, n))
                   for g, names in DEFAULT_COLORS.items() for n in names)

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        # Buttons are packed first (at the bottom) so they stay visible while
        # the list scrolls.
        btns = ttk.Frame(frm)
        btns.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Button(btns, text=t("btn.reset_colors"), command=self._reset).pack(side="left")
        ttk.Button(btns, text=t("btn.cancel"), command=self.destroy).pack(side="right")
        ttk.Button(btns, text=t("btn.ok"), command=self._ok).pack(side="right", padx=3)

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

        for group, defaults in DEFAULT_COLORS.items():
            tab = ttk.LabelFrame(body, text=group, padding=8)
            tab.pack(fill="x", pady=(0, 8), padx=2)
            for row, name in enumerate(defaults):
                key = (group, name)
                ttk.Label(tab, text=color_label(group, name),
                          width=desc_w).grid(row=row, column=0, sticky="w", pady=3)
                # The id: group.name, exactly as used in the code and the JSON.
                ttk.Label(tab, text="%s.%s" % (group, name), width=id_w).grid(
                    row=row, column=1, sticky="w", padx=(12, 0))
                box = tk.Label(tab, width=8, relief="solid", borderwidth=1)
                box.grid(row=row, column=2, padx=8)
                self.swatches[key] = box
                var = tk.StringVar(value=colors.get(group, {}).get(name, defaults[name]))
                self.vars[key] = var
                var.trace_add("write", lambda *a, k=key: self._swatch(k))
                ttk.Entry(tab, textvariable=var, width=12).grid(row=row, column=3)
                ttk.Button(tab, text=t("btn.pick"),
                           command=lambda g=group, n=name: self._pick(g, n)
                           ).grid(row=row, column=4, padx=(5, 0))
                self._swatch(key)

        # Fit the canvas to the list, but never taller than 60% of the screen.
        self.update_idletasks()
        self.canvas.configure(
            width=body.winfo_reqwidth(),
            height=min(body.winfo_reqheight(), int(self.winfo_screenheight() * 0.6)))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind(seq, self._wheel)

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self._center(parent)
        self.deiconify()
        self.grab_set()
        self.wait_window(self)

    def _wheel(self, event):
        """
        Scroll the list with the mouse wheel.

        @param event  Tk wheel event (Button-4/5 on Linux, delta on Windows/macOS)
        """
        if event.num == 4:          # Linux wheel up
            step = -1
        elif event.num == 5:        # Linux wheel down
            step = 1
        else:                       # Windows / macOS
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

        @param color  #rrggbb or a color name
        @return       True if Tk understands it
        """
        try:
            self.winfo_rgb(color)
        except tk.TclError:
            return False
        return True

    def _swatch(self, key):
        """
        Fill one swatch with its entry's color ('invalid' if it is bad).

        @param key  (group, name) of the row
        """
        color = self.vars[key].get().strip()
        if self._valid(color):
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
        opts = {"color": current} if self._valid(current) else {}
        _rgb, hexval = colorchooser.askcolor(
            parent=self, title=color_label(group, name), **opts)
        if hexval:
            self.vars[(group, name)].set(hexval)

    def _reset(self):
        """Put the built-in default colors into all entries (not saved yet)."""
        for (group, name), var in self.vars.items():
            var.set(DEFAULT_COLORS[group][name])

    def _ok(self):
        """Check every entry, then close with the new colors in self.result."""
        bad = ["%s.%s  (%s)" % (g, n, color_label(g, n))
               for (g, n), v in self.vars.items() if not self._valid(v.get().strip())]
        if bad:
            messagebox.showerror(t("dlg.colors"),
                                 t("msg.colors_invalid", list="\n  ".join(bad)),
                                 parent=self)
            return
        self.result = {g: {n: self.vars[(g, n)].get().strip() for n in names}
                       for g, names in DEFAULT_COLORS.items()}
        self.destroy()
