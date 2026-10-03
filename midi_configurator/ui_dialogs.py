"""Zoomable application prompts with the familiar tkinter.messagebox API."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

try:
    from .ui_scale import ScrollableFrame, get_zoom
except ImportError:
    from ui_scale import ScrollableFrame, get_zoom


class _Dialog(tk.Toplevel):
    def __init__(self, parent, title, message, buttons, close_value, initialvalue=None):
        super().__init__(parent)
        self.zoom = get_zoom(parent)
        self.result, self.close_value = close_value, close_value
        self.previous_grab = self.grab_current()
        self.previous_grab_status = self.previous_grab.grab_status() if self.previous_grab else None
        self.title(title or "Stompilot")
        self.configure(background="#111721")
        if parent.winfo_viewable():
            self.transient(parent.winfo_toplevel())
        self.frame = ttk.Frame(self, padding=16)
        self.frame.pack(fill="both", expand=True)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(2, weight=1)
        self.heading = ttk.Label(self.frame, text=title or "Stompilot", font=self.zoom.font("section"))
        self.heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.zoom.toolbar(self.frame).grid(row=1, column=0, sticky="w", pady=(0, 12))
        self.body = ScrollableFrame(self.frame)
        self.body.grid(row=2, column=0, sticky="nsew")
        self.message = ttk.Label(self.body.content, text=message, justify="left", anchor="nw",
                                 font=self.zoom.font("body"))
        self.message.pack(fill="x", anchor="nw")
        self.entry = None
        if initialvalue is not None:
            self.entry = ttk.Entry(self.frame, font=self.zoom.font("body"))
            self.entry.insert(0, initialvalue)
            self.entry.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.footer = ttk.Frame(self.frame)
        self.footer.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        self.buttons = []
        for text, value in buttons:
            button = ttk.Button(self.footer, text=text, command=lambda answer=value: self._answer(answer))
            button.pack(side="left", padx=4)
            self.buttons.append(button)
        self._button_layout = "left"
        self.frame.bind("<Configure>", self._resize)
        self.footer.bind("<Configure>", self._resize_buttons)
        self.zoom.listen(self, self._zoom_changed)
        self.protocol("WM_DELETE_WINDOW", lambda: self._answer(close_value))
        self.bind("<Escape>", lambda event: self._answer(close_value))
        self.bind("<Return>", lambda event: self._answer(buttons[0][1]))
        self.update_idletasks()
        if self.entry is not None:
            self.entry.select_range(0, "end")
            self.entry.focus_set()

    def _zoom_changed(self, _percent):
        self.frame.configure(padding=self.zoom.px(16))
        self.zoom.fit_window(self, 620, 390 if self.entry is not None else 360)
        self._resize()

    def _resize(self, _event=None):
        width = max(100, self.frame.winfo_width() - self.zoom.px(48))
        self.heading.configure(wraplength=width)
        self.message.configure(wraplength=width)

    def _resize_buttons(self, _event=None):
        total = sum(button.winfo_reqwidth() + self.zoom.px(8) for button in self.buttons)
        direction = "top" if total > self.footer.winfo_width() else "left"
        if direction != self._button_layout:
            self._button_layout = direction
            for button in self.buttons:
                button.pack_forget()
                button.pack(side=direction, fill="x" if direction == "top" else "none", padx=4, pady=2)

    def _answer(self, value):
        self.result = self.entry.get() if self.entry is not None and value is True else value
        self.destroy()

    def destroy(self):
        previous = self.previous_grab
        super().destroy()
        if previous is not None:
            try:
                if previous.winfo_exists():
                    if self.previous_grab_status == "global":
                        previous.grab_set_global()
                    else:
                        previous.grab_set()
            except tk.TclError:
                pass

    def show(self):
        self.wait_visibility()
        self.grab_set()
        self.wait_window()
        return self.result


def _show(title, message, buttons, close_value, parent=None, initialvalue=None, **options):
    owned_root = None
    if parent is None:
        parent = tk._default_root
        if parent is None:
            owned_root = parent = tk.Tk()
            owned_root.withdraw()
    detail = options.get("detail")
    if detail:
        message = f"{message}\n\n{detail}"
    try:
        return _Dialog(parent, title, message or "", buttons, close_value, initialvalue).show()
    finally:
        if owned_root is not None:
            owned_root.destroy()


def showinfo(title=None, message=None, **options):
    return _show(title, message, [("OK", "ok")], "ok", **options)


def showerror(title=None, message=None, **options):
    return _show(title, message, [("OK", "ok")], "ok", **options)


def askyesno(title=None, message=None, **options):
    return _show(title, message, [("Yes", True), ("No", False)], False, **options)


def askyesnocancel(title=None, message=None, **options):
    return _show(title, message, [("Yes", True), ("No", False), ("Cancel", None)], None, **options)


def askstring(title, prompt, initialvalue="", parent=None, **options):
    return _show(title, prompt, [("OK", True), ("Cancel", None)], None,
                 parent=parent, initialvalue=initialvalue, **options)
