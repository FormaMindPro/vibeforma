"""Snippets tab for the settings dialog: pairs, preview and JSON exchange.

The tab owns the pair list while the window is open. The settings window
passes the live config (the current pairs and the dictionary used by
validation) and a callback that receives the normalized list after every
change, so "Save" writes it to config.json. Editing, previewing and the
JSON import/export stay local; nothing leaves the computer.
"""
import os
import tkinter as tk
from tkinter import filedialog

import snippets
from config import DEFAULTS

BG = "#16181D"
FG = "#D5D9E0"
DIM = "#9AA3B0"
FIELD_BG = "#22262E"
LINE = "#242830"
ACCENT = "#2E86DE"
OK_FG = "#9FE0A8"
WARN_FG = "#E5C07B"
ERROR_FG = "#E06C75"


class SnippetsTab:
    """The Snippets tab: list, form, preview and validation feedback."""

    def __init__(self, win, frame, px, tr, cfg, save=None):
        self.win = win
        self.px = px
        self.tr = tr
        self.cfg = cfg or {}
        self._save = save
        self.entries = snippets.normalize(self.cfg.get("snippets"))
        self._build(frame)
        self._refresh()

    # -- layout ------------------------------------------------------------
    def _make_label(self, parent, text, fg=DIM, size=9, bold=False, wrap=470):
        font = ("Segoe UI", size, "bold") if bold else ("Segoe UI", size)
        return tk.Label(parent, text=text, font=font, fg=fg, bg=BG,
                        justify="left", anchor="w",
                        wraplength=self.px(wrap))

    def _label(self, parent, key, fg=DIM, size=9, bold=False, wrap=470):
        return self._make_label(parent, self.tr(key), fg=fg, size=size,
                                bold=bold, wrap=wrap)

    def _build(self, frame):
        holder = tk.Frame(frame, bg=BG)
        holder.grid(row=0, column=0, sticky="nsew")
        px = self.px
        tk.Label(holder, text=self.tr("tabs.snippets"),
                 font=("Segoe UI", 15, "bold"), fg=FG, bg=BG).pack(
                     anchor="w", padx=px(18), pady=(px(18), px(2)))
        self._label(holder, "settings.sub_snippets").pack(
            anchor="w", padx=px(18), pady=(0, px(8)))
        tk.Frame(holder, bg=LINE, height=px(1)).pack(
            fill="x", padx=px(18), pady=(px(10), px(2)))
        self._label(holder, "snippets.g", bold=True).pack(
            anchor="w", padx=px(18), pady=(px(6), px(2)))
        self._label(holder, "snippets.note").pack(
            anchor="w", padx=px(18), pady=(0, px(4)))

        self.pair_list = tk.Listbox(holder, height=7, width=46, bg=FIELD_BG,
                                    fg=FG, relief="flat", highlightthickness=0,
                                    selectbackground=ACCENT,
                                    font=("Segoe UI", 9))
        self.pair_list.pack(fill="x", padx=px(18), pady=px(4))
        self.pair_list.bind("<<ListboxSelect>>", self._load_selected)

        self.v_trigger = tk.StringVar(value="")
        row = tk.Frame(holder, bg=BG)
        row.pack(fill="x", padx=px(18), pady=px(3))
        tk.Label(row, text=self.tr("snippets.trigger"), font=("Segoe UI", 10),
                 fg=FG, bg=BG, width=16, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=self.v_trigger, bg=FIELD_BG, fg=FG,
                 insertbackground=FG, relief="flat").pack(
                     side="left", fill="x", expand=True)

        row = tk.Frame(holder, bg=BG)
        row.pack(fill="x", padx=px(18), pady=px(3))
        tk.Label(row, text=self.tr("snippets.text"), font=("Segoe UI", 10),
                 fg=FG, bg=BG, width=16, anchor="nw").pack(side="left")
        self.text_box = tk.Text(row, height=3, wrap="word", bg=FIELD_BG,
                                fg=FG, insertbackground=FG, relief="flat",
                                highlightthickness=0, font=("Segoe UI", 9))
        self.text_box.pack(side="left", fill="x", expand=True)

        buttons = tk.Frame(holder, bg=BG)
        buttons.pack(anchor="w", padx=px(18), pady=px(5))
        for text, command in (
                (self.tr("snippets.add"), self._add),
                (self.tr("snippets.update"), self._update),
                (self.tr("snippets.remove"), self._remove),
                (self.tr("snippets.import"), self._import),
                (self.tr("snippets.export"), self._export)):
            tk.Button(buttons, text=text, command=command, bg="#22262E",
                      fg=FG, relief="flat", activebackground="#2A2F38",
                      activeforeground=FG, cursor="hand2").pack(
                          side="left", padx=(0, 6))

        preview_row = tk.Frame(holder, bg=BG)
        preview_row.pack(fill="x", padx=px(18), pady=px(3))
        tk.Button(preview_row, text=self.tr("snippets.preview"),
                  command=self._preview, bg="#22262E", fg=FG, relief="flat",
                  activebackground="#2A2F38", activeforeground=FG,
                  cursor="hand2").pack(side="left")
        self.v_sample = tk.StringVar(value="")
        tk.Entry(preview_row, textvariable=self.v_sample, bg=FIELD_BG,
                 fg=FG, insertbackground=FG, relief="flat").pack(
                     side="left", fill="x", expand=True, padx=(8, 0))

        self.preview_label = self._make_label(holder, "", fg=OK_FG)
        self.preview_label.pack(fill="x", padx=px(18), pady=(px(2), px(2)))
        self.issues_label = self._make_label(holder, "")
        self.issues_label.pack(fill="x", padx=px(18), pady=(px(2), px(2)))
        self.status = self._make_label(holder, "", fg=OK_FG)
        self.status.pack(fill="x", padx=px(18), pady=(px(2), px(14)))

    # -- state -------------------------------------------------------------
    def entries_list(self):
        """The normalized pair list, as "Save" writes it to config.json."""
        return [dict(entry) for entry in self.entries]

    def reset(self):
        """Back to the config default: an empty list, kept until Save."""
        self.v_trigger.set("")
        self._set_text("")
        self.v_sample.set("")
        self.preview_label.configure(text="")
        self._set_entries(snippets.normalize(DEFAULTS.get("snippets")))
        self._say(self.tr("snippets.reset"))

    def _set_entries(self, items):
        self.entries = snippets.normalize(items)
        self._refresh()
        if self._save is not None:
            self._save(self.entries_list())

    def _set_text(self, value):
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", value)

    def _read_form(self):
        return self.v_trigger.get(), self.text_box.get("1.0", "end-1c")

    def _selected_index(self):
        chosen = self.pair_list.curselection()
        if not chosen:
            return None
        index = int(chosen[0])
        return index if 0 <= index < len(self.entries) else None

    def _refresh(self):
        self.pair_list.delete(0, "end")
        for entry in self.entries:
            first = entry["text"].splitlines()[0] if entry["text"] else ""
            if len(first) > 46:
                first = first[:45] + "..."
            self.pair_list.insert("end", entry["trigger"] + "  ->  " + first)
        issues = snippets.validate(self.entries, self.cfg.get("dictionary"))
        if not issues:
            self.issues_label.configure(text=self.tr("snippets.issues_ok"),
                                        fg=OK_FG)
            return
        lines = [self.tr("snippets.issue_" + issue["code"],
                         trigger=issue["trigger"]) for issue in issues]
        errors = any(issue["level"] == "error" for issue in issues)
        self.issues_label.configure(text="\n".join(lines),
                                    fg=ERROR_FG if errors else WARN_FG)

    def _say(self, message, fg=OK_FG):
        self.status.configure(text=message, fg=fg)

    # -- actions -----------------------------------------------------------
    def _add(self):
        trigger, text = self._read_form()
        if not trigger.strip():
            self._say(self.tr("snippets.no_trigger"), ERROR_FG)
            return
        if not text.strip():
            self._say(self.tr("snippets.no_text"), ERROR_FG)
            return
        cleaned = snippets.normalize([{"trigger": trigger, "text": text}])
        if not cleaned:
            self._say(self.tr("snippets.same"), ERROR_FG)
            return
        entry = cleaned[0]
        key = entry["trigger"].casefold()
        items = list(self.entries)
        for position, current in enumerate(items):
            if current["trigger"].casefold() == key:
                items[position] = entry
                break
        else:
            items.append(entry)
        self._set_entries(items)
        self.v_trigger.set("")
        self._set_text("")
        self._say(self.tr("snippets.added", trigger=entry["trigger"]))

    def _update(self):
        index = self._selected_index()
        if index is None:
            self._say(self.tr("snippets.select_pair"), ERROR_FG)
            return
        trigger, text = self._read_form()
        cleaned = snippets.normalize([{"trigger": trigger, "text": text}])
        if not cleaned:
            if not trigger.strip():
                self._say(self.tr("snippets.no_trigger"), ERROR_FG)
            elif not text.strip():
                self._say(self.tr("snippets.no_text"), ERROR_FG)
            else:
                self._say(self.tr("snippets.same"), ERROR_FG)
            return
        entry = cleaned[0]
        key = entry["trigger"].casefold()
        for position, current in enumerate(self.entries):
            if position != index and current["trigger"].casefold() == key:
                self._say(self.tr("snippets.dup_trigger",
                                  trigger=entry["trigger"]), ERROR_FG)
                return
        items = list(self.entries)
        items[index] = entry
        self._set_entries(items)
        self._say(self.tr("snippets.updated", trigger=entry["trigger"]))

    def _remove(self):
        index = self._selected_index()
        if index is None:
            self._say(self.tr("snippets.select_pair"), ERROR_FG)
            return
        trigger = self.entries[index]["trigger"]
        items = list(self.entries)
        del items[index]
        self._set_entries(items)
        self._set_text("")
        self.v_trigger.set("")
        self._say(self.tr("snippets.removed", trigger=trigger))

    def _load_selected(self, _event=None):
        index = self._selected_index()
        if index is None:
            return
        entry = self.entries[index]
        self.v_trigger.set(entry["trigger"])
        self._set_text(entry["text"])

    def _import(self):
        path = filedialog.askopenfilename(
            parent=self.win, title=self.tr("snippets.import"),
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as handle:
                imported = snippets.from_json(handle.read())
        except (OSError, ValueError):
            self._say(self.tr("snippets.bad_file"), ERROR_FG)
            return
        self._set_entries(list(self.entries) + imported)
        self._say(self.tr("snippets.imported", n=len(imported)))

    def _export(self):
        path = filedialog.asksaveasfilename(
            parent=self.win, title=self.tr("snippets.export"),
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(snippets.to_json(self.entries))
        except OSError:
            self._say(self.tr("snippets.bad_file"), ERROR_FG)
            return
        self._say(self.tr("snippets.exported", path=os.path.basename(path)))

    def _preview(self):
        sample = self.v_sample.get()
        if not sample.strip():
            self.preview_label.configure(text=self.tr("snippets.preview_none"),
                                         fg=DIM)
            return
        result, count = snippets.apply(sample, self.entries)
        if count:
            text = self.tr("snippets.preview_result", text=result, n=count)
        else:
            text = self.tr("snippets.preview_same", text=result)
        self.preview_label.configure(text=text, fg=OK_FG)


def build_snippets_tab(win, frame, px, tr, cfg, save=None):
    """Build the Snippets tab inside frame; returns the tab object."""
    return SnippetsTab(win, frame, px, tr, cfg, save)
