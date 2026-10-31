#!/usr/bin/env python3
"""
fw_manager_lang.py - Program text for fw_manager.py and fw_manager_colors.py.

All text the user sees (menus, buttons, column headings, dialogs, log lines,
status texts, color descriptions) lives in lang/<code>.json next to this script.

lang/<code>.json layout (flat, one key per text, plus "language.name"):
    {"language.name": "English", "<key>": "<text with {placeholders}>", ...}
Placeholders are filled by t(), e.g. t("out.scanning", count=3) with the text
"Scanning {count} device(s)..." gives "Scanning 3 device(s)...".

The language is chosen in Settings > Language and saved in the config as
"language"; it is used from the next start. Entries missing in a language fall
back to DEFAULT_LANG, then to the key itself.
"""

import json
import os

LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang")
"""Folder with one flat JSON file per language, named <code>.json (en.json, de.json, ...)."""

DEFAULT_LANG = "en"
"""Language code used when none is configured; also fills in missing entries."""

_labels = {}
"""Active texts: key -> text (see set_language)."""


def lang_path(code):
    """
    Path of the text file for a language.

    @param code  language code, e.g. "en"
    @return      path of <code>.json inside LANG_DIR
    """
    return os.path.join(LANG_DIR, code + ".json")


def load_labels(path):
    """
    Read the texts from one language file.

    @param path  flat JSON file with the layout {"<key>": text}
    @return      {"<key>": text}; empty if the file is missing or invalid
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def list_languages():
    """
    Find the available languages.

    @return  list of (code, display name) sorted by display name; the name is the
             "language.name" entry of the file, or the code if it has none
    """
    found = []
    try:
        files = sorted(os.listdir(LANG_DIR))
    except OSError:
        return found
    for fn in files:
        if not fn.lower().endswith(".json"):
            continue
        code = fn[:-5]
        name = load_labels(os.path.join(LANG_DIR, fn)).get("language.name")
        found.append((code, name if isinstance(name, str) and name else code))
    return sorted(found, key=lambda item: item[1].lower())


def resolve_language(code):
    """
    Check a language code without activating it.

    @param code  language code, e.g. "en"; may be empty or unknown
    @return      code if its file exists and is valid, else DEFAULT_LANG
    """
    if isinstance(code, str) and code and code != DEFAULT_LANG \
            and load_labels(lang_path(code)):
        return code
    return DEFAULT_LANG


def set_language(code):
    """
    Make a language the active one. Entries missing in that language fall back
    to DEFAULT_LANG.

    @param code  language code, e.g. "en"; empty or unknown means DEFAULT_LANG
    @return      the code that is now active
    """
    code = resolve_language(code)
    labels = load_labels(lang_path(DEFAULT_LANG))
    if code != DEFAULT_LANG:
        labels.update(load_labels(lang_path(code)))
    _labels.clear()
    _labels.update(labels)
    return code


def t(key, **fields):
    """
    Text for a key in the active language.

    @param key     entry name in the language file, e.g. "btn.scan_all"
    @param fields  values for the {placeholders} of the text
    @return        the text with the placeholders filled in; the key itself if
                   there is no such entry; the unfilled text if a placeholder
                   does not match
    """
    text = _labels.get(key)
    if not isinstance(text, str):
        return key
    if not fields:
        return text
    try:
        return text.format(**fields)
    except (KeyError, IndexError, ValueError):
        return text


set_language(DEFAULT_LANG)    # so t() works before the program picks a language
