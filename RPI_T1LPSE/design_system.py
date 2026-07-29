#!/usr/bin/env python3
"""
Shared design system for the industrial demo apps.

Single source of truth for the visual language defined in CLAUDE.md: the colour
palette, the light/dark theme definitions, the spacing scale, and the reusable
Button and Card components. Imported by both gui.py and main_app.py so a palette
change lands in every app at once.

Any host object passed to Button/Card as `app` must satisfy the theme contract:

    app.theme              -> dict, the active THEMES entry
    app.font_btn           -> tkfont.Font used for button captions
    app.on_theme(fn)       -> register a zero-arg callback, run on theme change
    app.parent_bg(widget)  -> background colour of widget.master

ThemeMixin below implements that contract; mix it into your application class.
"""

import tkinter as tk
import tkinter.font as tkfont

# ---------------------------------------------------------------------------
# Colour palette  (CLAUDE.md — Recommended Color Palette)
# ---------------------------------------------------------------------------
PRIMARY = {900: "#003366", 700: "#0055A4", 500: "#0078D4", 300: "#5AA7E6", 100: "#DCEEFF"}
NEUTRAL = {950: "#111827", 900: "#1F2937", 800: "#374151", 700: "#4B5563",
           600: "#6B7280", 500: "#9CA3AF", 400: "#D1D5DB", 300: "#E5E7EB",
           200: "#F3F4F6", 100: "#F9FAFB", 50:  "#FFFFFF"}
SUCCESS = {700: "#15803D", 500: "#22C55E", 100: "#DCFCE7"}
WARNING = {700: "#B45309", 500: "#F59E0B", 100: "#FEF3C7"}
ERROR   = {700: "#B91C1C", 500: "#EF4444", 100: "#FEE2E2"}
INFO    = {700: "#1D4ED8", 500: "#3B82F6", 100: "#DBEAFE"}

# Theme definitions (CLAUDE.md — Theme Definitions)
THEMES = {
    "light": {
        "bg":       NEUTRAL[50],    "surface":  NEUTRAL[100],
        "card":     NEUTRAL[50],    "border":   NEUTRAL[300],
        "text":     NEUTRAL[950],   "text2":    NEUTRAL[700],
        "text_dis": NEUTRAL[500],
        "primary":  PRIMARY[500],   "primary_hover": PRIMARY[700],
        "on_primary": NEUTRAL[50],
        "nav_active_bg": PRIMARY[100], "nav_active_fg": PRIMARY[700],
        "row_alt":  NEUTRAL[100],   "row_hover": PRIMARY[100],
        "header_bg": PRIMARY[900],  "header_fg": NEUTRAL[50],
    },
    "dark": {
        "bg":       NEUTRAL[950],   "surface":  NEUTRAL[900],
        "card":     NEUTRAL[800],   "border":   NEUTRAL[700],
        "text":     NEUTRAL[100],   "text2":    NEUTRAL[400],
        "text_dis": NEUTRAL[500],
        "primary":  PRIMARY[300],   "primary_hover": "#7CBDFF",
        "on_primary": NEUTRAL[950],
        "nav_active_bg": PRIMARY[900], "nav_active_fg": NEUTRAL[50],
        "row_alt":  NEUTRAL[700],   "row_hover": PRIMARY[700],
        "header_bg": PRIMARY[900],  "header_fg": NEUTRAL[50],
    },
}

# Spacing scale (CLAUDE.md — Spacing Scale). Only these values are used.
XS, SM, MD, LG, XL, XXL = 4, 8, 16, 24, 32, 48

RADIUS = 8          # CLAUDE.md: 8px corner radius on buttons

# ---------------------------------------------------------------------------
# Semantic colours for hardware status readouts.
# These are palette-mapped replacements for the ad-hoc literals the demo apps
# used before ("green"/"red"/"orange"/"gray", #2ecc71/#f39c12/#e74c3c) and for
# matplotlib's default C0/C1/C2 series colours.
# ---------------------------------------------------------------------------
SEVERITY = {"success": SUCCESS[500], "warning": WARNING[500],
            "error": ERROR[500],     "info": INFO[500]}

STATUS_COLOR = {"ok": SUCCESS[500], "warn": WARNING[500],
                "error": ERROR[500], "idle": NEUTRAL[500]}

LEVEL_COLOR = {"OK": SUCCESS[500], "WARNING": WARNING[500], "ALARM": ERROR[500]}

AXIS_COLORS       = [PRIMARY[500], WARNING[500], SUCCESS[500]]
COLOR_AXIS_COLORS = [ERROR[500], SUCCESS[500], INFO[500]]


def pick_font(*candidates):
    """Return the first installed font family, so the UI degrades gracefully."""
    available = {f.lower() for f in tkfont.families()}
    for name in candidates:
        if name.lower() in available:
            return name
    return "TkDefaultFont"


def rounded_points(x1, y1, x2, y2, r):
    """Point list for a rounded rectangle drawn as a smoothed polygon."""
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


# ---------------------------------------------------------------------------
# ThemeMixin — implements the theme contract Button and Card depend on
# ---------------------------------------------------------------------------
class ThemeMixin:
    """Theme registry for an application window.

    Mix into a tk.Tk (or any widget) subclass and call init_theme() early in
    __init__. Widgets register a zero-arg callback via on_theme(); every
    callback runs on each theme switch.
    """

    def init_theme(self, theme_name="light", family=None):
        self.theme_name = theme_name
        self.theme = THEMES[theme_name]
        self._theme_hooks = []
        fam = family or pick_font("Segoe UI", "Inter", "DejaVu Sans", "Helvetica")
        self.font_btn = tkfont.Font(family=fam, size=10, weight="bold")

    def on_theme(self, fn):
        """Register fn to run on every theme change (and call it now)."""
        self._theme_hooks.append(fn)

    def parent_bg(self, widget):
        """Background of a widget's parent, so canvases blend in seamlessly."""
        try:
            return widget.master.cget("bg")
        except tk.TclError:
            return self.theme["bg"]

    def toggle_theme(self):
        self.set_theme("dark" if self.theme_name == "light" else "light")

    def set_theme(self, theme_name):
        self.theme_name = theme_name
        self.theme = THEMES[theme_name]
        self.apply_theme()

    def apply_theme(self):
        """Run every registered hook. Override to add app-specific work."""
        for fn in self._theme_hooks:
            fn()


# ---------------------------------------------------------------------------
# Button — solid primary colour, 8px radius, medium weight, visible focus ring
# ---------------------------------------------------------------------------
class Button(tk.Canvas):
    """Canvas-drawn button so the documented 8px corner radius is honoured."""

    def __init__(self, master, app, text, command=None, variant="primary",
                 icon="", height=34, min_width=0):
        self.app, self.command = app, command
        self.variant, self.icon, self.label = variant, icon, text
        self._hover = self._pressed = False
        self._enabled = True

        caption = f"{icon}  {text}" if icon else text
        width = max(min_width, app.font_btn.measure(caption) + 2 * MD)
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, bd=0, takefocus=1)
        # NB: not self._w / self._h — tkinter reserves _w for the widget path name.
        self._bw, self._bh = width, height

        self.bind("<Enter>",           self._on_enter)
        self.bind("<Leave>",           self._on_leave)
        self.bind("<ButtonPress-1>",   self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>",         lambda e: self.render())
        self.bind("<FocusOut>",        lambda e: self.render())
        self.bind("<Return>",          lambda e: self.invoke())
        self.bind("<space>",           lambda e: self.invoke())
        app.on_theme(self.render)

    # -- state -------------------------------------------------------------
    def set_enabled(self, enabled):
        self._enabled = enabled
        self.configure(takefocus=1 if enabled else 0)
        self.render()

    def invoke(self):
        if self._enabled and self.command:
            self.command()

    def _on_enter(self, _):   self._hover = True;  self.render()
    def _on_leave(self, _):   self._hover = self._pressed = False; self.render()
    def _on_press(self, _):
        if self._enabled:
            self._pressed = True
            self.focus_set()
            self.render()

    def _on_release(self, _):
        was = self._pressed
        self._pressed = False
        self.render()
        if was:
            self.invoke()

    # -- drawing -----------------------------------------------------------
    def render(self):
        t = self.app.theme
        self.delete("all")
        self.configure(bg=self.app.parent_bg(self))

        if self.variant == "primary":
            fill = t["primary_hover"] if (self._hover or self._pressed) else t["primary"]
            fg, outline = t["on_primary"], ""
        elif self.variant == "secondary":
            fill = t["row_hover"] if self._hover else t["card"]
            fg, outline = t["text"], t["border"]
        else:  # ghost
            fill = t["row_hover"] if self._hover else self.app.parent_bg(self)
            fg, outline = t["primary"], ""

        if not self._enabled:
            fill, fg, outline = t["surface"], t["text_dis"], t["border"]

        self.create_polygon(rounded_points(1, 1, self._bw - 1, self._bh - 1, RADIUS),
                            smooth=True, fill=fill,
                            outline=outline or fill, width=1)

        caption = f"{self.icon}  {self.label}" if self.icon else self.label
        dy = 1 if self._pressed else 0
        self.create_text(self._bw / 2, self._bh / 2 + dy, text=caption,
                         fill=fg, font=self.app.font_btn)

        # Visible keyboard focus state
        if self.focus_get() is self and self._enabled:
            self.create_polygon(rounded_points(3, 3, self._bw - 3, self._bh - 3, RADIUS - 2),
                                smooth=True, fill="", outline=PRIMARY[300], width=2)


# ---------------------------------------------------------------------------
# Card — subtle border, minimal shadow, 16-24px padding
# ---------------------------------------------------------------------------
class Card(tk.Frame):
    def __init__(self, master, app, pad=LG):
        self._shadow = tk.Frame(master, bd=0)          # 1px offset = minimal shadow
        super().__init__(self._shadow, bd=1, relief="solid")
        self.app, self._pad = app, pad
        super().pack(fill=tk.BOTH, expand=True, padx=(0, 1), pady=(0, 1))

        self.body = tk.Frame(self)
        self.body.pack(fill=tk.BOTH, expand=True, padx=pad, pady=pad)
        app.on_theme(self._apply)

    def _apply(self):
        t = self.app.theme
        self._shadow.configure(bg=t["border"])
        self.configure(bg=t["card"], highlightbackground=t["border"],
                       highlightcolor=t["border"])
        self.body.configure(bg=t["card"])

    # Named grid_in / pack_in (not grid/place) so Tk's own geometry methods
    # stay unshadowed on the inner frame.
    def grid_in(self, **kw): self._shadow.grid(**kw)   # grid the shadow wrapper
    def pack_in(self, **kw): self._shadow.pack(**kw)   # pack the shadow wrapper
    def outer(self):         return self._shadow
