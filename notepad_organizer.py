"""
Notepad Organizer - a simple local note-taking app.

Run:   python notepad_organizer.py
Needs: Python 3.8+ (tkinter and sqlite3 ship with Python - nothing to pip install)

Features
  - Notebooks to organize notes (notes can also stay unfiled)
  - Live search across titles and note text
  - Autosave while you type
  - Pin important notes to the top
  - Move notes between notebooks, export a note to .txt / .md
  - All data lives in notes.db next to this script
"""

import os
import re
import sqlite3
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes.db")
AUTOSAVE_MS = 700
SEL_BG = "#cfe3ff"
SEL_FG = "#000000"

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebooks (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    notebook_id INTEGER REFERENCES notebooks(id) ON DELETE SET NULL,
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    pinned      INTEGER NOT NULL DEFAULT 0,
    created     TEXT NOT NULL,
    updated     TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def fmt_short(iso):
    """Today -> '14:03', otherwise 'Oct 15'."""
    dt = datetime.fromisoformat(iso)
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%b %d")


def fmt_long(iso):
    return datetime.fromisoformat(iso).strftime("%b %d, %Y %H:%M")


# --------------------------------------------------------------------------
# Data layer
# --------------------------------------------------------------------------
class Store:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # notebooks
    def notebooks(self):
        return self.conn.execute(
            "SELECT b.id, b.name, COUNT(n.id) AS n FROM notebooks b "
            "LEFT JOIN notes n ON n.notebook_id = b.id "
            "GROUP BY b.id ORDER BY b.name COLLATE NOCASE"
        ).fetchall()

    def add_notebook(self, name):
        with self.conn:
            cur = self.conn.execute("INSERT INTO notebooks(name) VALUES (?)", (name,))
        return cur.lastrowid

    def rename_notebook(self, nb_id, name):
        with self.conn:
            self.conn.execute("UPDATE notebooks SET name = ? WHERE id = ?", (name, nb_id))

    def delete_notebook(self, nb_id):
        with self.conn:
            self.conn.execute("UPDATE notes SET notebook_id = NULL WHERE notebook_id = ?", (nb_id,))
            self.conn.execute("DELETE FROM notebooks WHERE id = ?", (nb_id,))

    def notebook_name(self, nb_id):
        row = self.conn.execute("SELECT name FROM notebooks WHERE id = ?", (nb_id,)).fetchone()
        return row["name"] if row else ""

    # notes
    def count_notes(self):
        return self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]

    def notes(self, notebook_id=None, query=""):
        sql = ("SELECT id, title, substr(body, 1, 80) AS preview, pinned, updated "
               "FROM notes WHERE 1 = 1")
        params = []
        if notebook_id is not None:
            sql += " AND notebook_id = ?"
            params.append(notebook_id)
        if query:
            esc = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
            params += [f"%{esc}%", f"%{esc}%"]
        sql += " ORDER BY pinned DESC, updated DESC"
        return self.conn.execute(sql, params).fetchall()

    def get_note(self, note_id):
        return self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()

    def add_note(self, notebook_id=None, title="", body=""):
        ts = now_iso()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO notes(notebook_id, title, body, created, updated) VALUES (?,?,?,?,?)",
                (notebook_id, title, body, ts, ts),
            )
        return cur.lastrowid

    def update_note(self, note_id, title, body):
        with self.conn:
            self.conn.execute(
                "UPDATE notes SET title = ?, body = ?, updated = ? WHERE id = ?",
                (title, body, now_iso(), note_id),
            )

    def delete_note(self, note_id):
        with self.conn:
            self.conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))

    def toggle_pin(self, note_id):
        with self.conn:
            self.conn.execute("UPDATE notes SET pinned = 1 - pinned WHERE id = ?", (note_id,))

    def move_note(self, note_id, notebook_id):
        with self.conn:
            self.conn.execute("UPDATE notes SET notebook_id = ? WHERE id = ?", (notebook_id, note_id))

    def seed_if_empty(self):
        if self.count_notes() or self.notebooks():
            return
        personal = self.add_notebook("Personal")
        self.add_notebook("Work")
        self.add_note(
            personal,
            "Welcome to Notepad Organizer",
            "Your notes save automatically as you type.\n\n"
            "- Create notebooks on the left to organize notes\n"
            "- Use the search box to find text in any note\n"
            "- Pin a note (Ctrl+P) to keep it at the top\n"
            "- Use the dropdown at the top right to move a note to another notebook\n"
            "- Ctrl+N new note, Ctrl+F search, Ctrl+S save now",
        )


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
class NotesApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Notepad Organizer")
        self.geometry("1100x680")
        self.minsize(820, 500)

        self.store = Store(DB_PATH)
        self.current_nb = None      # None = "All Notes"
        self.current_id = None      # note open in the editor
        self.nb_ids = []            # listbox row -> notebook id
        self.note_ids = []          # listbox row -> note id
        self.combo_ids = []         # combobox row -> notebook id
        self._loading = False
        self._save_job = None

        self.search_var = tk.StringVar()
        self.title_var = tk.StringVar()
        self.meta_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.counts_var = tk.StringVar()

        self._build_menu()
        self._build_ui()
        self._bind_keys()

        self.search_var.trace_add("write", lambda *_: self.refresh_notes())
        self.title_var.trace_add("write", self.on_title_change)

        self.store.seed_if_empty()
        self.refresh_notebooks()
        self.refresh_notes()
        if self.note_ids:
            self.load_note(self.note_ids[0])
            self.highlight(self.current_id)
        else:
            self.load_note(None)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------------------------------------------------------- build
    def _build_menu(self):
        bar = tk.Menu(self)
        file_m = tk.Menu(bar, tearoff=0)
        file_m.add_command(label="New Note", accelerator="Ctrl+N", command=self.new_note)
        file_m.add_command(label="Save Now", accelerator="Ctrl+S", command=self.save_now)
        file_m.add_command(label="Export Note...", command=self.export_note)
        file_m.add_separator()
        file_m.add_command(label="Exit", command=self.on_close)
        bar.add_cascade(label="File", menu=file_m)

        edit_m = tk.Menu(bar, tearoff=0)
        edit_m.add_command(label="Undo", command=lambda: self._edit("edit_undo"))
        edit_m.add_command(label="Redo", command=lambda: self._edit("edit_redo"))
        edit_m.add_separator()
        edit_m.add_command(label="Find in Notes", accelerator="Ctrl+F", command=self.focus_search)
        bar.add_cascade(label="Edit", menu=edit_m)

        note_m = tk.Menu(bar, tearoff=0)
        note_m.add_command(label="Pin / Unpin", accelerator="Ctrl+P", command=self.toggle_pin)
        note_m.add_command(label="Delete Note", command=self.delete_note)
        bar.add_cascade(label="Note", menu=note_m)

        nb_m = tk.Menu(bar, tearoff=0)
        nb_m.add_command(label="New Notebook...", command=self.add_notebook)
        nb_m.add_command(label="Rename Notebook...", command=self.rename_notebook)
        nb_m.add_command(label="Delete Notebook", command=self.delete_notebook)
        bar.add_cascade(label="Notebook", menu=nb_m)

        self.config(menu=bar)

    def _build_ui(self):
        ui_font = tkfont.nametofont("TkDefaultFont").copy()
        ui_font.configure(size=11)
        small_bold = ui_font.copy()
        small_bold.configure(size=9, weight="bold")
        title_font = ui_font.copy()
        title_font.configure(size=18, weight="bold")
        editor_font = tkfont.nametofont("TkTextFont").copy()
        editor_font.configure(size=12)
        self._fonts = (ui_font, small_bold, title_font, editor_font)  # keep references

        # status bar (packed first so it stays at the bottom)
        status = ttk.Frame(self)
        status.pack(side="bottom", fill="x")
        ttk.Separator(status).pack(fill="x")
        ttk.Label(status, textvariable=self.status_var).pack(side="left", padx=10, pady=3)
        ttk.Label(status, textvariable=self.counts_var).pack(side="right", padx=10, pady=3)

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # --- sidebar: notebooks
        side = ttk.Frame(paned)
        ttk.Label(side, text="NOTEBOOKS", font=small_bold).pack(anchor="w", padx=10, pady=(12, 4))
        self.nb_list = tk.Listbox(
            side, width=22, exportselection=False, activestyle="none", borderwidth=0,
            highlightthickness=0, font=ui_font, selectbackground=SEL_BG, selectforeground=SEL_FG,
        )
        self.nb_list.pack(fill="both", expand=True, padx=6)
        self.nb_list.bind("<<ListboxSelect>>", self.on_nb_select)
        btns = ttk.Frame(side)
        btns.pack(fill="x", padx=6, pady=8)
        ttk.Button(btns, text="+ Add", command=self.add_notebook).pack(side="left", expand=True, fill="x")
        ttk.Button(btns, text="Rename", command=self.rename_notebook).pack(side="left", expand=True, fill="x")
        ttk.Button(btns, text="Delete", command=self.delete_notebook).pack(side="left", expand=True, fill="x")
        paned.add(side, weight=0)

        # --- middle: search + note list
        mid = ttk.Frame(paned)
        ttk.Button(mid, text="+ New note", command=self.new_note).pack(fill="x", padx=6, pady=(10, 6))
        ttk.Label(mid, text="SEARCH", font=small_bold).pack(anchor="w", padx=8)
        self.search_entry = ttk.Entry(mid, textvariable=self.search_var)
        self.search_entry.pack(fill="x", padx=6, pady=(2, 6))
        list_frame = ttk.Frame(mid)
        list_frame.pack(fill="both", expand=True, padx=(6, 0))
        self.note_list = tk.Listbox(
            list_frame, width=34, exportselection=False, activestyle="none", borderwidth=0,
            highlightthickness=0, font=ui_font, selectbackground=SEL_BG, selectforeground=SEL_FG,
        )
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.note_list.yview)
        self.note_list.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.note_list.pack(side="left", fill="both", expand=True)
        self.note_list.bind("<<ListboxSelect>>", self.on_note_select)
        self.note_list.bind("<Button-3>", self.on_note_context)
        self.note_list.bind("<Button-2>", self.on_note_context)  # macOS
        paned.add(mid, weight=0)

        # --- right: editor
        right = ttk.Frame(paned)
        header = ttk.Frame(right)
        header.pack(fill="x", padx=14, pady=(12, 0))
        self.title_entry = ttk.Entry(header, textvariable=self.title_var, font=title_font)
        self.title_entry.pack(side="left", fill="x", expand=True)
        self.nb_combo = ttk.Combobox(header, state="readonly", width=18)
        self.nb_combo.pack(side="right", padx=(10, 0))
        self.nb_combo.bind("<<ComboboxSelected>>", self.on_move)
        ttk.Label(right, textvariable=self.meta_var, foreground="#777777").pack(anchor="w", padx=16, pady=(4, 6))
        ttk.Separator(right).pack(fill="x", padx=14)

        text_frame = ttk.Frame(right)
        text_frame.pack(fill="both", expand=True, padx=(14, 0), pady=6)
        self.body = tk.Text(
            text_frame, wrap="word", undo=True, borderwidth=0, highlightthickness=0,
            font=editor_font, padx=4, pady=6, spacing3=3,
        )
        tsb = ttk.Scrollbar(text_frame, orient="vertical", command=self.body.yview)
        self.body.config(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self.body.pack(side="left", fill="both", expand=True)
        self.body.bind("<<Modified>>", self.on_body_modified)
        paned.add(right, weight=1)

        # right-click menu for notes
        self.ctx = tk.Menu(self, tearoff=0)
        self.ctx.add_command(label="Pin / Unpin", command=self.toggle_pin)
        self.ctx.add_command(label="Export...", command=self.export_note)
        self.ctx.add_separator()
        self.ctx.add_command(label="Delete", command=self.delete_note)

    def _bind_keys(self):
        def wrap(fn):
            return lambda _e: (fn(), "break")[1]

        shortcuts = {
            "<Control-n>": self.new_note,
            "<Control-f>": self.focus_search,
            "<Control-s>": self.save_now,
            "<Control-p>": self.toggle_pin,
        }
        for seq, fn in shortcuts.items():
            self.bind_all(seq, wrap(fn))
            # widget-level bindings stop Tk's built-in Emacs-style keys (Ctrl+N = down, etc.)
            for w in (self.body, self.title_entry, self.search_entry):
                w.bind(seq, wrap(fn))
        self.search_entry.bind("<Escape>", lambda _e: self.search_var.set(""))
        self.search_entry.bind("<Return>", self.open_first_result)

    # ------------------------------------------------------------- helpers
    def _edit(self, method):
        try:
            getattr(self.body, method)()
        except tk.TclError:
            pass

    def focus_search(self):
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")

    def set_editor_enabled(self, on):
        self.title_entry.config(state="normal" if on else "disabled")
        self.body.config(state="normal" if on else "disabled")
        self.nb_combo.config(state="readonly" if on else "disabled")

    def highlight(self, note_id):
        self.note_list.selection_clear(0, "end")
        if note_id in self.note_ids:
            i = self.note_ids.index(note_id)
            self.note_list.selection_set(i)
            self.note_list.see(i)

    def display_title(self, row):
        title = row["title"].strip()
        if title:
            return title
        preview = row["preview"].strip()
        first = preview.splitlines()[0][:40] if preview else ""
        return first or "Untitled note"

    # ----------------------------------------------------------- refreshing
    def refresh_notebooks(self):
        rows = self.store.notebooks()
        self.nb_ids = [None] + [r["id"] for r in rows]
        self.nb_list.delete(0, "end")
        self.nb_list.insert("end", f"All Notes ({self.store.count_notes()})")
        for r in rows:
            self.nb_list.insert("end", f"{r['name']} ({r['n']})")
        if self.current_nb not in self.nb_ids:
            self.current_nb = None
        idx = self.nb_ids.index(self.current_nb)
        self.nb_list.selection_clear(0, "end")
        self.nb_list.selection_set(idx)

        self.combo_ids = [None] + [r["id"] for r in rows]
        self.nb_combo["values"] = ["(No notebook)"] + [r["name"] for r in rows]
        self.sync_combo()

    def sync_combo(self):
        if self.current_id is None:
            self.nb_combo.set("")
            return
        note = self.store.get_note(self.current_id)
        nb = note["notebook_id"] if note else None
        self.nb_combo.current(self.combo_ids.index(nb) if nb in self.combo_ids else 0)

    def refresh_notes(self):
        rows = self.store.notes(self.current_nb, self.search_var.get().strip())
        self.note_ids = [r["id"] for r in rows]
        self.note_list.delete(0, "end")
        for r in rows:
            star = "\u2605 " if r["pinned"] else ""
            self.note_list.insert("end", f"{star}{self.display_title(r)}   \u00b7  {fmt_short(r['updated'])}")
        self.highlight(self.current_id)

    # -------------------------------------------------------------- editor
    def load_note(self, note_id):
        self._cancel_save()
        self._loading = True
        self.body.config(state="normal")
        self.body.delete("1.0", "end")
        if note_id is None:
            self.current_id = None
            self.title_var.set("")
            self.meta_var.set("")
            self.nb_combo.set("")
            self.set_editor_enabled(False)
        else:
            note = self.store.get_note(note_id)
            self.current_id = note_id
            self.title_var.set(note["title"])
            self.body.insert("1.0", note["body"])
            self.body.edit_reset()
            self.body.edit_modified(False)
            self.set_editor_enabled(True)
            self.sync_combo()
            self.update_meta()
        self._loading = False
        self.update_counts()

    def update_meta(self):
        if self.current_id is None:
            return
        note = self.store.get_note(self.current_id)
        self.meta_var.set(f"Created {fmt_long(note['created'])}   \u00b7   Edited {fmt_long(note['updated'])}")

    def update_counts(self):
        if self.current_id is None:
            self.counts_var.set("")
            return
        text = self.body.get("1.0", "end-1c")
        self.counts_var.set(f"{len(text.split())} words  \u00b7  {len(text)} characters")

    # ------------------------------------------------------------- autosave
    def on_body_modified(self, _event=None):
        if self.body.edit_modified():
            self.body.edit_modified(False)
            if not self._loading and self.current_id is not None:
                self.update_counts()
                self.schedule_save()

    def on_title_change(self, *_):
        if not self._loading and self.current_id is not None:
            self.schedule_save()

    def schedule_save(self):
        self._cancel_save()
        self.status_var.set("Editing...")
        self._save_job = self.after(AUTOSAVE_MS, self.save_current)

    def _cancel_save(self):
        if self._save_job is not None:
            self.after_cancel(self._save_job)
            self._save_job = None

    def flush_save(self):
        if self._save_job is not None:
            self._cancel_save()
            self.save_current()

    def save_current(self):
        self._save_job = None
        if self.current_id is None:
            return
        self.store.update_note(
            self.current_id, self.title_var.get().strip(), self.body.get("1.0", "end-1c")
        )
        self.status_var.set("Saved " + datetime.now().strftime("%H:%M:%S"))
        self.update_meta()
        self.refresh_notes()

    def save_now(self):
        self._cancel_save()
        self.save_current()

    # ---------------------------------------------------------------- notes
    def on_note_select(self, _event=None):
        sel = self.note_list.curselection()
        if not sel:
            return
        nid = self.note_ids[sel[0]]
        if nid == self.current_id:
            return
        self.flush_save()
        self.load_note(nid)
        self.highlight(nid)

    def open_first_result(self, _event=None):
        if self.note_ids:
            self.flush_save()
            self.load_note(self.note_ids[0])
            self.highlight(self.current_id)
            self.body.focus_set()

    def on_note_context(self, event):
        i = self.note_list.nearest(event.y)
        box = self.note_list.bbox(i) if i >= 0 else None
        if i < 0 or i >= len(self.note_ids) or not box or not (box[1] <= event.y <= box[1] + box[3]):
            return
        nid = self.note_ids[i]
        if nid != self.current_id:
            self.flush_save()
            self.load_note(nid)
        self.highlight(nid)
        self.ctx.tk_popup(event.x_root, event.y_root)

    def new_note(self):
        self.flush_save()
        if self.search_var.get():
            self.search_var.set("")
        nid = self.store.add_note(self.current_nb)
        self.load_note(nid)
        self.refresh_notebooks()
        self.refresh_notes()
        self.title_entry.focus_set()
        self.status_var.set("New note created")

    def delete_note(self):
        if self.current_id is None:
            return
        if not messagebox.askyesno("Delete note", "Delete this note permanently?", parent=self):
            return
        self._cancel_save()
        self.store.delete_note(self.current_id)
        self.current_id = None
        self.refresh_notebooks()
        self.refresh_notes()
        self.load_note(self.note_ids[0] if self.note_ids else None)
        self.highlight(self.current_id)
        self.status_var.set("Note deleted")

    def toggle_pin(self):
        if self.current_id is None:
            return
        self.flush_save()
        self.store.toggle_pin(self.current_id)
        self.refresh_notes()
        self.status_var.set("Pin toggled")

    def on_move(self, _event=None):
        if self.current_id is None:
            return
        new_nb = self.combo_ids[self.nb_combo.current()]
        self.flush_save()
        self.store.move_note(self.current_id, new_nb)
        if self.current_nb is not None:      # follow the note into its new notebook
            self.current_nb = new_nb
        self.refresh_notebooks()
        self.refresh_notes()
        self.status_var.set("Note moved")

    def export_note(self):
        if self.current_id is None:
            return
        self.save_now()
        note = self.store.get_note(self.current_id)
        safe = re.sub(r'[\\/:*?"<>|]+', "_", note["title"].strip() or "note")
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".txt", initialfile=safe,
            filetypes=[("Text file", "*.txt"), ("Markdown", "*.md"), ("All files", "*.*")],
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{note['title']}\n\n{note['body']}\n")
        self.status_var.set(f"Exported to {os.path.basename(path)}")

    # ------------------------------------------------------------ notebooks
    def on_nb_select(self, _event=None):
        sel = self.nb_list.curselection()
        if sel:
            self.switch_notebook(self.nb_ids[sel[0]])

    def switch_notebook(self, nb_id):
        self.flush_save()
        self.current_nb = nb_id
        self.refresh_notebooks()
        self.refresh_notes()
        if self.current_id not in self.note_ids:
            self.load_note(self.note_ids[0] if self.note_ids else None)
            self.highlight(self.current_id)

    def add_notebook(self):
        name = simpledialog.askstring("New notebook", "Notebook name:", parent=self)
        if not name or not name.strip():
            return
        try:
            nb_id = self.store.add_notebook(name.strip())
        except sqlite3.IntegrityError:
            messagebox.showinfo("Notebook exists", "A notebook with that name already exists.", parent=self)
            return
        self.switch_notebook(nb_id)

    def rename_notebook(self):
        if self.current_nb is None:
            messagebox.showinfo("Rename notebook", "Select a notebook first.", parent=self)
            return
        name = simpledialog.askstring(
            "Rename notebook", "New name:", parent=self,
            initialvalue=self.store.notebook_name(self.current_nb),
        )
        if not name or not name.strip():
            return
        try:
            self.store.rename_notebook(self.current_nb, name.strip())
        except sqlite3.IntegrityError:
            messagebox.showinfo("Notebook exists", "A notebook with that name already exists.", parent=self)
            return
        self.refresh_notebooks()

    def delete_notebook(self):
        if self.current_nb is None:
            messagebox.showinfo("Delete notebook", "Select a notebook first.", parent=self)
            return
        name = self.store.notebook_name(self.current_nb)
        if not messagebox.askyesno(
            "Delete notebook",
            f"Delete the notebook '{name}'?\n\nIts notes are kept and will still appear under All Notes.",
            parent=self,
        ):
            return
        self.flush_save()
        self.store.delete_notebook(self.current_nb)
        self.switch_notebook(None)

    # ---------------------------------------------------------------- close
    def on_close(self):
        self.flush_save()
        self.store.close()
        self.destroy()


if __name__ == "__main__":
    NotesApp().mainloop()
