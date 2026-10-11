"""Drive the head dialogs without a person: gui._show is replaced by a script that
edits widgets and presses buttons, then the dialog's return value is checked."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import pytest

from forms_automation.common import gui

try:
    tk.Tk().destroy()
except tk.TclError:  # no display
    pytest.skip("Tk needs a display", allow_module_level=True)


def widgets(root):
    for child in root.winfo_children():
        yield child
        yield from widgets(child)


def press(root, text):
    button = next(w for w in widgets(root) if isinstance(w, ttk.Button) and w.cget("text") == text)
    button.invoke()


def entry(root, index):
    return [w for w in widgets(root) if isinstance(w, ttk.Entry)][index]


def drive(monkeypatch, script):
    def fake_show(root, focus=None):
        root.update_idletasks()
        script(root)
        try:  # script left it open -> behave like pressing Escape
            root.event_generate("<Escape>")
            root.update()
        except tk.TclError:  # already closed by a button
            pass
    monkeypatch.setattr(gui, "_show", fake_show)


DRAFT = {"id": "datacor_affiliation", "description": "Worked for the company before",
         "examples": ["Have you worked for Datacor before?"], "mode": "reuse", "auto_fill": True}
HEADS = [{"id": "travel", "description": "Travel", "status": "approved"},
         {"id": "gender", "description": "Gender identity", "status": "approved"}]


def test_review_head_approve_with_rename(monkeypatch):
    def script(root):
        box = entry(root, 0)
        box.delete(0, "end")
        box.insert(0, "worked_here_before")
        press(root, "Approve")
    drive(monkeypatch, script)
    action, head = gui.review_head(DRAFT, DRAFT["examples"], HEADS, flag="rename")
    assert action == gui.APPROVE
    assert head["id"] == "worked_here_before" and head["examples"] == DRAFT["examples"]
    assert head["mode"] == "reuse" and head["auto_fill"] is True


def test_review_head_bad_id_shows_error_and_stays_open(monkeypatch):
    seen = {}

    def script(root):
        box = entry(root, 0)
        box.delete(0, "end")
        box.insert(0, "Bad Id")
        press(root, "Approve")
        seen["open"] = bool(root.winfo_exists())
        seen["errors"] = [w.cget("text") for w in widgets(root)
                          if isinstance(w, ttk.Label) and "lower_snake_case" in str(w.cget("text"))]
    drive(monkeypatch, script)
    assert gui.review_head(DRAFT, DRAFT["examples"], HEADS) == (None, None)  # closed -> pending
    assert seen["open"] and seen["errors"]


def test_review_head_duplicate_id_rejected(monkeypatch):
    def script(root):
        box = entry(root, 0)
        box.delete(0, "end")
        box.insert(0, "travel")
        press(root, "Approve")
    drive(monkeypatch, script)
    assert gui.review_head(DRAFT, DRAFT["examples"], HEADS) == (None, None)


def test_review_head_merge_and_reject(monkeypatch):
    def merge(root):
        listbox = next(w for w in widgets(root) if isinstance(w, tk.Listbox))
        listbox.selection_set(1)
        press(root, "Merge into selected")
    drive(monkeypatch, merge)
    assert gui.review_head(DRAFT, DRAFT["examples"], HEADS) == (gui.MERGE, "gender")
    drive(monkeypatch, lambda root: press(root, "Reject"))
    assert gui.review_head(DRAFT, DRAFT["examples"], HEADS) == (gui.REJECT, None)


def test_confirm_head_buttons(monkeypatch):
    pick = {"id": "travel", "description": "Travel", "mode": "reuse"}
    args = ("Can you travel 25%?", "https://job-boards.greenhouse.io/acme/jobs/1", pick, 0.62,
            "Same travel question", "Yes", HEADS)
    drive(monkeypatch, lambda root: press(root, "Use this topic"))
    assert gui.confirm_head(*args) == (gui.CONFIRM, "travel")
    drive(monkeypatch, lambda root: press(root, "It's a new topic"))
    assert gui.confirm_head(*args) == (gui.NEW_HEAD, None)
    drive(monkeypatch, lambda root: press(root, "Don't use a topic"))
    assert gui.confirm_head(*args) == (gui.NO_CLASSIFY, None)
    drive(monkeypatch, lambda root: None)  # Escape
    assert gui.confirm_head(*args) == (gui.NO_CLASSIFY, None)

    def other(root):
        listbox = next(w for w in widgets(root) if isinstance(w, tk.Listbox))
        listbox.selection_set(0)  # "travel" is the pick, so the list starts with gender
        press(root, "Use selected topic")
    drive(monkeypatch, other)
    assert gui.confirm_head(*args) == (gui.OTHER_HEAD, "gender")


def test_review_letter_default_and_answer_labels(monkeypatch):
    drive(monkeypatch, lambda root: press(root, "Upload as PDF"))
    assert gui.review_letter("Cover letter", "Dear team", []) == (gui.UPLOAD, "Dear team")
    drive(monkeypatch, lambda root: press(root, "Fill this answer"))
    assert gui.review_letter("Answer", "I want to learn", [], labels=gui.ANSWER_LABELS) == (
        gui.UPLOAD, "I want to learn")
