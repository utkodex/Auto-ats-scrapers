"""Small Tkinter pop-ups used instead of terminal prompts.

ask_question() shows one application question and returns what the user chose;
notice() shows a message with buttons (e.g. "Next job") and returns the label
of the button pressed; review_letter() edits a cover letter or an LLM-written
answer; confirm_head() and review_head() check classification heads (topics).
Each call opens its own window and blocks until closed.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from urllib.parse import urlparse

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 11, "bold")
FONT_SMALL = ("Segoe UI", 9)
WIDTH = 620
LIST_THRESHOLD = 10  # more options than this -> searchable list instead of buttons

# Result actions
SAVE, BLANK, SKIP = "save", "blank", "skip"


def _sharp_text_on_windows() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def company_of(job_url: str | None) -> str:
    """'reddit' from https://job-boards.greenhouse.io/reddit/jobs/1."""
    if not job_url:
        return ""
    parsed = urlparse(job_url)
    if "for=" in parsed.query:
        return parsed.query.split("for=")[1].split("&")[0]
    return parsed.path.strip("/").split("/")[0]


def _window(title: str, topmost: bool = True) -> tk.Tk:
    _sharp_text_on_windows()
    for attempt in range(3):
        try:
            root = tk.Tk()
            break
        except tk.TclError:  # Tcl sometimes fails to read init.tcl on Windows; a retry works
            if attempt == 2:
                raise
    root.title(title)
    root.configure(padx=18, pady=14)
    root.resizable(False, False)
    root.attributes("-topmost", topmost)  # questions stay above the Chrome window
    ttk.Style(root).configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
    return root


def _show(root: tk.Tk, focus: tk.Widget | None = None) -> None:
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
    y = max(40, (root.winfo_screenheight() - root.winfo_reqheight()) // 3)
    root.geometry(f"+{x}+{y}")
    root.lift()
    root.focus_force()
    if focus is not None:
        focus.focus_set()
    root.mainloop()


class QuestionDialog:
    def __init__(self, question: str, options: list[str] | None = None, required: bool = False,
                 multi: bool = False, kind: str = "text", job: str | None = None):
        self.options = options or []
        self.multi = multi
        self.result: tuple[str, object] = (BLANK, None)

        root = self.root = _window("Application question")
        company = company_of(job)
        meta = " · ".join(part for part in (company, "required" if required else "optional") if part)
        ttk.Label(root, text=meta.upper(), font=FONT_SMALL, foreground="#666").pack(anchor="w")
        ttk.Label(root, text=question, font=FONT_BOLD, wraplength=WIDTH, justify="left").pack(
            anchor="w", pady=(2, 10))

        self.custom: tk.Entry | tk.Text | None = None
        if not self.options:
            focus = self._free_text(kind)
        elif len(self.options) > LIST_THRESHOLD:
            focus = self._searchable_list()
        else:
            focus = self._buttons()

        self.error = ttk.Label(root, text="", font=FONT_SMALL, foreground="#b00020")
        self.error.pack(anchor="w", pady=(6, 0))
        ttk.Label(root, text="Saved to answers.json and reused whenever a form asks this again.",
                  font=FONT_SMALL, foreground="#666").pack(anchor="w", pady=(2, 10))

        bar = ttk.Frame(root)
        bar.pack(fill="x")
        ttk.Button(bar, text="Save answer", style="Accent.TButton", command=self.on_save).pack(side="right")
        ttk.Button(bar, text="Leave blank for now", command=self.on_blank).pack(side="right", padx=6)
        ttk.Button(bar, text="Never fill this", command=self.on_skip).pack(side="left")

        root.protocol("WM_DELETE_WINDOW", self.on_blank)
        root.bind("<Escape>", lambda _e: self.on_blank())
        # Enter saves, except inside a multi-line box (Ctrl+Enter there).
        root.bind("<Return>", lambda e: None if isinstance(e.widget, tk.Text) else self.on_save())
        root.bind("<Control-Return>", lambda _e: self.on_save())
        self.focus = focus

    # -- answer widgets ---------------------------------------------------

    def _free_text(self, kind: str) -> tk.Widget:
        if kind == "textarea":
            self.custom = tk.Text(self.root, width=70, height=7, font=FONT, wrap="word")
            ttk.Label(self.root, text="Ctrl+Enter to save", font=FONT_SMALL, foreground="#666").pack(anchor="e")
        else:
            self.custom = ttk.Entry(self.root, width=80, font=FONT)
        self.custom.pack(fill="x")
        return self.custom

    def _buttons(self) -> tk.Widget:
        frame = ttk.Frame(self.root)
        frame.pack(fill="x")
        if self.multi:
            self.checks = [tk.BooleanVar() for _ in self.options]
            for var, option in zip(self.checks, self.options, strict=True):
                ttk.Checkbutton(frame, text=option, variable=var).pack(anchor="w", pady=2)
        else:
            self.choice = tk.IntVar(value=-1)
            for i, option in enumerate(self.options):
                ttk.Radiobutton(frame, text=option, value=i, variable=self.choice).pack(anchor="w", pady=2)
        self._custom_entry()
        return frame.winfo_children()[0]

    def _searchable_list(self) -> tk.Widget:
        self.picked: set[str] = set()
        search = ttk.Entry(self.root, width=80, font=FONT)
        search.pack(fill="x")
        ttk.Label(self.root, text="Type to filter", font=FONT_SMALL, foreground="#666").pack(anchor="w")
        frame = ttk.Frame(self.root)
        frame.pack(fill="x", pady=(4, 0))
        self.listbox = tk.Listbox(frame, height=12, font=FONT, exportselection=False,
                                  selectmode="multiple" if self.multi else "browse")
        scroll = ttk.Scrollbar(frame, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.visible = list(self.options)

        def refill(*_):
            term = search.get().lower()
            self.visible = [o for o in self.options if term in o.lower()]
            self.listbox.delete(0, "end")
            for i, option in enumerate(self.visible):
                self.listbox.insert("end", option)
                if option in self.picked:
                    self.listbox.selection_set(i)

        def remember(_e=None):
            shown = set(self.visible)
            chosen = {self.visible[i] for i in self.listbox.curselection()}
            self.picked = (self.picked - shown) | chosen if self.multi else chosen

        search.bind("<KeyRelease>", refill)
        self.listbox.bind("<<ListboxSelect>>", remember)
        self.listbox.bind("<Double-Button-1>", lambda _e: (remember(), self.on_save()))
        refill()
        self._custom_entry()
        return search

    def _custom_entry(self) -> None:
        ttk.Label(self.root, text="Or type your own answer:", font=FONT_SMALL).pack(anchor="w", pady=(8, 0))
        self.custom = ttk.Entry(self.root, width=80, font=FONT)
        self.custom.pack(fill="x")

    # -- reading the answer -----------------------------------------------

    def _typed(self) -> str:
        if self.custom is None:
            return ""
        if isinstance(self.custom, tk.Text):
            return self.custom.get("1.0", "end").strip()
        return self.custom.get().strip()

    def _value(self):
        typed = self._typed()
        if typed:
            return typed
        if hasattr(self, "checks"):
            picked = [o for o, var in zip(self.options, self.checks, strict=True) if var.get()]
        elif hasattr(self, "choice"):
            picked = [self.options[self.choice.get()]] if self.choice.get() >= 0 else []
        elif hasattr(self, "picked"):
            picked = [o for o in self.options if o in self.picked]
        else:
            picked = []
        if not picked:
            return None
        return picked if self.multi else picked[0]

    # -- buttons ----------------------------------------------------------

    def on_save(self) -> None:
        value = self._value()
        if value is None:
            self.error.configure(text="Pick an option or type an answer (or choose 'Leave blank for now').")
            return
        self.result = (SAVE, value)
        self.root.destroy()

    def on_blank(self) -> None:
        self.result = (BLANK, None)
        self.root.destroy()

    def on_skip(self) -> None:
        self.result = (SKIP, None)
        self.root.destroy()

    def run(self) -> tuple[str, object]:
        _show(self.root, self.focus)
        return self.result


def ask_question(question: str, options: list[str] | None = None, required: bool = False,
                 multi: bool = False, kind: str = "text", job: str | None = None) -> tuple[str, object]:
    """Returns (SAVE, answer), (BLANK, None) or (SKIP, None)."""
    return QuestionDialog(question, options, required, multi, kind, job).run()


def notice(title: str, heading: str, lines: list[str], buttons: list[str],
           poll=None, poll_ms: int = 1500) -> str:
    """Message window; returns the label of the button pressed (last one on close).

    poll: optional callable run every poll_ms while the window is open; the text it
    returns (if any) is shown in a status line - e.g. "Security code entered".
    """
    root = _window(title, topmost=False)
    pressed = {"label": buttons[-1]}
    ttk.Label(root, text=heading, font=FONT_BOLD, wraplength=WIDTH, justify="left").pack(anchor="w")
    for line in lines:
        ttk.Label(root, text=line, font=FONT, wraplength=WIDTH, justify="left").pack(anchor="w", pady=1)
    status = ttk.Label(root, text="", font=("Segoe UI", 10, "bold"), foreground="#1565c0",
                       wraplength=WIDTH, justify="left")
    status.pack(anchor="w", pady=(8, 0))

    def tick() -> None:
        try:
            message = poll()
        except Exception as exc:  # never let a polling error close the window
            message = f"(status check failed: {exc})"
        if message and message != status.cget("text"):
            status.configure(text=message)
            root.lift()  # bring the update to the user's attention
        root.after(poll_ms, tick)

    if poll is not None:
        root.after(poll_ms, tick)
    bar = ttk.Frame(root)
    bar.pack(fill="x", pady=(14, 0))

    def press(label: str) -> None:
        pressed["label"] = label
        root.destroy()

    first = None
    for i, label in enumerate(buttons):
        button = ttk.Button(bar, text=label, command=lambda lbl=label: press(lbl),
                            style="Accent.TButton" if i == 0 else "TButton")
        button.pack(side="right", padx=(6, 0))
        first = first or button
    root.protocol("WM_DELETE_WINDOW", lambda: press(buttons[-1]))
    root.bind("<Return>", lambda _e: press(buttons[0]))
    _show(root, first)
    return pressed["label"]


# Cover-letter review actions
UPLOAD, REGENERATE, SKIP_LETTER = "upload", "regenerate", "skip"


LETTER_LABELS = {
    "title": "Cover letter - review before upload",
    "upload": "Upload as PDF",
    "regenerate": "Regenerate",
    "skip": "Skip cover letter",
    "hint": "Edit the letter directly. Upload saves it as a PDF and attaches it to the Cover Letter field "
            "(Ctrl+Enter).",
}
# The same window reviewing an LLM-written answer to a form question.
ANSWER_LABELS = {
    "title": "Written answer - review before filling",
    "upload": "Fill this answer",
    "regenerate": "Write again",
    "skip": "Leave blank",
    "hint": "Edit the answer directly. It's filled into the form and kept in answers.json under "
            "\"generated\" (Ctrl+Enter).",
}


def review_letter(heading: str, letter: str, warnings: list[str],
                  labels: dict | None = None) -> tuple[str, str]:
    """Editable cover-letter preview with the automatic checks listed above it.

    Returns (UPLOAD, final_text), (REGENERATE, text) or (SKIP_LETTER, text).
    labels: button/title texts (ANSWER_LABELS for generated answers).
    """
    labels = {**LETTER_LABELS, **(labels or {})}
    root = _window(labels["title"])
    result = {"action": SKIP_LETTER, "text": letter}
    ttk.Label(root, text=heading, font=FONT_BOLD, wraplength=760, justify="left").pack(anchor="w")

    if warnings:
        box = ttk.Frame(root, padding=(0, 6, 0, 6))
        box.pack(fill="x")
        ttk.Label(box, text="Check these before uploading:", font=("Segoe UI", 10, "bold"),
                  foreground="#9a5b00").pack(anchor="w")
        for warning in warnings:
            ttk.Label(box, text=f"\u2022 {warning}", font=FONT_SMALL, foreground="#9a5b00",
                      wraplength=760, justify="left").pack(anchor="w")
    else:
        ttk.Label(root, text="No problems found by the automatic checks - still read it through.",
                  font=FONT_SMALL, foreground="#2e7d32").pack(anchor="w", pady=(4, 6))

    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    text = tk.Text(frame, width=96, height=22, font=("Segoe UI", 10), wrap="word", undo=True,
                   padx=8, pady=6)
    scroll = ttk.Scrollbar(frame, command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    text.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    text.insert("1.0", letter)

    ttk.Label(root, text=labels["hint"], font=FONT_SMALL, foreground="#666",
              wraplength=760, justify="left").pack(anchor="w", pady=(6, 10))

    def finish(action: str) -> None:
        result["action"], result["text"] = action, text.get("1.0", "end").strip()
        root.destroy()

    bar = ttk.Frame(root)
    bar.pack(fill="x")
    ttk.Button(bar, text=labels["upload"], style="Accent.TButton",
               command=lambda: finish(UPLOAD)).pack(side="right")
    ttk.Button(bar, text=labels["regenerate"], command=lambda: finish(REGENERATE)).pack(side="right", padx=6)
    ttk.Button(bar, text=labels["skip"], command=lambda: finish(SKIP_LETTER)).pack(side="left")
    root.protocol("WM_DELETE_WINDOW", lambda: finish(SKIP_LETTER))
    root.bind("<Control-Return>", lambda _e: finish(UPLOAD))
    _show(root, text)
    return result["action"], result["text"]


# Classification head dialogs. Neither shows the stored answers of other questions.
CONFIRM, OTHER_HEAD, NEW_HEAD, NO_CLASSIFY = "confirm", "other", "new", "none"
APPROVE, MERGE, REJECT = "approve", "merge", "reject"


def _head_picker(parent: tk.Widget, heads: list[dict], height: int = 6):
    """Searchable list of heads; returns a function giving the selected head id (or None)."""
    search = ttk.Entry(parent, width=80, font=FONT)
    search.pack(fill="x")
    frame = ttk.Frame(parent)
    frame.pack(fill="x", pady=(2, 0))
    listbox = tk.Listbox(frame, height=height, font=FONT, exportselection=False)
    scroll = ttk.Scrollbar(frame, command=listbox.yview)
    listbox.configure(yscrollcommand=scroll.set)
    listbox.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    shown: list[dict] = []

    def refill(*_):
        term = search.get().lower()
        shown[:] = [h for h in heads if term in h["id"] or term in h.get("description", "").lower()]
        listbox.delete(0, "end")
        for h in shown:
            listbox.insert("end", f"{h['id']} - {h.get('description', '')}")

    search.bind("<KeyRelease>", refill)
    refill()

    def selected() -> str | None:
        picked = listbox.curselection()
        return shown[picked[0]]["id"] if picked else None

    selected.listbox = listbox  # for tests
    return selected


def confirm_head(question: str, job: str | None, pick: dict, confidence: float, reason: str,
                 answer_preview: str | None, heads: list[dict]) -> tuple[str, str | None]:
    """Ask whether Jev's head for a question is right (low confidence or auto_fill off).

    Returns (CONFIRM, pick_id), (OTHER_HEAD, chosen_id), (NEW_HEAD, None) or (NO_CLASSIFY, None).
    """
    root = _window("Which topic is this question?")
    result: dict = {"value": (NO_CLASSIFY, None)}
    ttk.Label(root, text=company_of(job).upper(), font=FONT_SMALL, foreground="#666").pack(anchor="w")
    ttk.Label(root, text=question, font=FONT_BOLD, wraplength=WIDTH, justify="left").pack(anchor="w", pady=(2, 10))
    ttk.Label(root, text=f"Jev thinks this is:  {pick['id']}   (confidence {confidence:.2f})",
              font=("Segoe UI", 10, "bold")).pack(anchor="w")
    if pick.get("description"):
        ttk.Label(root, text=pick["description"], font=FONT_SMALL, foreground="#666",
                  wraplength=WIDTH, justify="left").pack(anchor="w")
    if reason:
        ttk.Label(root, text=f"“{reason}”", font=FONT, wraplength=WIDTH,
                  justify="left").pack(anchor="w", pady=(2, 0))
    if pick.get("mode") == "generate":
        preview = "writes a new answer for this job (you review it first)"
    else:
        preview = answer_preview if answer_preview is not None else "nothing saved yet - you'll be asked"
    ttk.Label(root, text=f"Will fill: {preview}", font=FONT, foreground="#1565c0",
              wraplength=WIDTH, justify="left").pack(anchor="w", pady=(6, 10))

    ttk.Label(root, text="Or pick another topic:", font=FONT_SMALL).pack(anchor="w")
    others = [h for h in heads if h["id"] != pick["id"]]
    selected = _head_picker(root, others)
    error = ttk.Label(root, text="", font=FONT_SMALL, foreground="#b00020")
    error.pack(anchor="w", pady=(4, 6))

    def finish(value) -> None:
        result["value"] = value
        root.destroy()

    def use_other() -> None:
        chosen = selected()
        if chosen is None:
            error.configure(text="Select a topic in the list first.")
            return
        finish((OTHER_HEAD, chosen))

    bar = ttk.Frame(root)
    bar.pack(fill="x")
    ttk.Button(bar, text="Use this topic", style="Accent.TButton",
               command=lambda: finish((CONFIRM, pick["id"]))).pack(side="right")
    ttk.Button(bar, text="Use selected topic", command=use_other).pack(side="right", padx=6)
    ttk.Button(bar, text="It's a new topic", command=lambda: finish((NEW_HEAD, None))).pack(side="left")
    ttk.Button(bar, text="Don't use a topic",
               command=lambda: finish((NO_CLASSIFY, None))).pack(side="left", padx=6)
    selected.listbox.bind("<Double-Button-1>", lambda _e: use_other())
    root.protocol("WM_DELETE_WINDOW", lambda: finish((NO_CLASSIFY, None)))
    root.bind("<Escape>", lambda _e: finish((NO_CLASSIFY, None)))
    _show(root)
    return result["value"]


def review_head(draft: dict, members: list[str], heads: list[dict], flag: str | None = None,
                progress: str = "") -> tuple[str | None, dict | str | None]:
    """Approve, edit, merge or reject a new head.

    Returns (APPROVE, edited_head), (MERGE, target_id), (REJECT, None),
    or (None, None) when closed - the head stays pending.
    """
    from forms_automation.db.head_store import valid_id

    title = "New topic - review" + (f"  ({progress})" if progress else "")
    root = _window(title)
    result: dict = {"value": (None, None)}
    taken = {h["id"] for h in heads} - {draft["id"]}

    hints = {"rename": "The name contains a company name - rename it to the fact it stores.",
             "unplaced": "Llama didn't place this question; check the name and description."}
    if flag in hints:
        ttk.Label(root, text=hints[flag], font=("Segoe UI", 10, "bold"),
                  foreground="#9a5b00").pack(anchor="w", pady=(0, 6))
    ttk.Label(root, text="Questions in this topic:", font=FONT_SMALL, foreground="#666").pack(anchor="w")
    for question in members:
        ttk.Label(root, text=f"• {question}", font=FONT, wraplength=WIDTH,
                  justify="left").pack(anchor="w")

    form = ttk.Frame(root)
    form.pack(fill="x", pady=(10, 0))
    ttk.Label(form, text="Name (id)", font=FONT_SMALL).grid(row=0, column=0, sticky="w")
    id_box = ttk.Entry(form, width=50, font=FONT)
    id_box.insert(0, draft["id"])
    id_box.grid(row=0, column=1, sticky="we", pady=2)
    ttk.Label(form, text="Description", font=FONT_SMALL).grid(row=1, column=0, sticky="w")
    desc_box = ttk.Entry(form, width=70, font=FONT)
    desc_box.insert(0, draft.get("description", ""))
    desc_box.grid(row=1, column=1, sticky="we", pady=2)
    ttk.Label(form, text="Examples\n(one per line)", font=FONT_SMALL).grid(row=2, column=0, sticky="nw")
    examples_box = tk.Text(form, width=70, height=4, font=FONT, wrap="word")
    examples_box.insert("1.0", "\n".join(draft.get("examples") or members))
    examples_box.grid(row=2, column=1, sticky="we", pady=2)
    mode = tk.StringVar(value=draft.get("mode", "reuse"))
    modes = ttk.Frame(form)
    modes.grid(row=3, column=1, sticky="w", pady=2)
    ttk.Label(form, text="Answer", font=FONT_SMALL).grid(row=3, column=0, sticky="w")
    ttk.Radiobutton(modes, text="reuse the saved answer", value="reuse", variable=mode).pack(side="left")
    ttk.Radiobutton(modes, text="write a new one per job", value="generate",
                    variable=mode).pack(side="left", padx=8)
    auto_fill = tk.BooleanVar(value=draft.get("auto_fill", draft.get("mode", "reuse") == "reuse"))
    ttk.Checkbutton(form, text="Fill without asking when Jev is confident",
                    variable=auto_fill).grid(row=4, column=1, sticky="w", pady=2)

    error = ttk.Label(root, text="", font=FONT_SMALL, foreground="#b00020")
    error.pack(anchor="w", pady=(4, 0))

    merge_targets = [h for h in heads if h["id"] != draft["id"] and h.get("status") == "approved"]
    selected = None
    if merge_targets:
        ttk.Label(root, text="Or merge into an existing topic:", font=FONT_SMALL).pack(anchor="w", pady=(8, 0))
        selected = _head_picker(root, merge_targets, height=4)

    def finish(value) -> None:
        result["value"] = value
        root.destroy()

    def approve() -> None:
        head_id = id_box.get().strip()
        if not valid_id(head_id):
            error.configure(text="Name must be lower_snake_case, 2-41 characters, starting with a letter.")
            return
        if head_id in taken:
            error.configure(text=f"A topic named '{head_id}' already exists - merge into it instead.")
            return
        examples = [line.strip() for line in examples_box.get("1.0", "end").splitlines() if line.strip()]
        finish((APPROVE, {**draft, "id": head_id, "description": desc_box.get().strip(),
                          "examples": examples, "mode": mode.get(), "auto_fill": auto_fill.get()}))

    def merge() -> None:
        target = selected() if selected else None
        if target is None:
            error.configure(text="Select a topic to merge into first.")
            return
        finish((MERGE, target))

    bar = ttk.Frame(root)
    bar.pack(fill="x", pady=(10, 0))
    ttk.Button(bar, text="Approve", style="Accent.TButton", command=approve).pack(side="right")
    if merge_targets:
        ttk.Button(bar, text="Merge into selected", command=merge).pack(side="right", padx=6)
    ttk.Button(bar, text="Reject", command=lambda: finish((REJECT, None))).pack(side="left")
    ttk.Button(bar, text="Decide later", command=lambda: finish((None, None))).pack(side="left", padx=6)
    root.protocol("WM_DELETE_WINDOW", lambda: finish((None, None)))
    root.bind("<Escape>", lambda _e: finish((None, None)))
    _show(root, id_box)
    return result["value"]
