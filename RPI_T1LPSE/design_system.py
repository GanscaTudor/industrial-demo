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

import math
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

# Fills (dots, plot lines, swatches) -- the 500 shades read well at any size
# because they cover area rather than thin glyph strokes.
STATUS_COLOR = {"ok": SUCCESS[500], "warn": WARNING[500],
                "error": ERROR[500], "idle": NEUTRAL[500]}

# Small bold *text* needs 4.5:1 against the card it sits on, and the shade that
# achieves that flips with the theme: SUCCESS[700] is 5.0:1 on a white card but
# only 2.1:1 on the dark card, while SUCCESS[500] is the exact reverse. So text
# status colours are per-theme; use status_text_color(), not STATUS_COLOR, for
# anything rendered as type.
STATUS_TEXT_COLOR = {
    "light": {"ok": SUCCESS[700], "warn": WARNING[700],
              "error": ERROR[700],  "idle": NEUTRAL[600]},
    "dark":  {"ok": SUCCESS[500], "warn": WARNING[500],
              "error": ERROR[500],  "idle": NEUTRAL[500]},
}


def status_text_color(theme_name, key):
    """Accessible text colour for a status key under the named theme."""
    return STATUS_TEXT_COLOR[theme_name][key]

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


# ---------------------------------------------------------------------------
# FanIndicator — rotating fan glyph for commanded-state display
# ---------------------------------------------------------------------------

# Blade profile in polar form: (radius as a fraction of the rotor radius,
# angular offset in degrees from the blade's own axis). Drawn as a smoothed
# polygon, so these are control points rather than literal vertices -- the curve
# runs inside them, so the extremes are pushed out further than the shape you
# want back.
#
# The asymmetry is the whole point: the leading edge sweeps forward while the
# trailing edge stays raked back, so the blade reads as an angled airfoil moving
# in a definite direction. A profile symmetric about the blade axis renders as a
# flower -- correct as geometry, useless as an indicator.
_BLADE_PROFILE = [
    (0.16,  -6),    # root, leading side
    (0.62, -30),    # leading edge sweeping forward
    (0.95, -26),
    (1.00,  -4),    # tip
    (0.92,  10),
    (0.60,  12),    # trailing edge raked back toward the hub
    (0.20,  20),
]


class FanIndicator(tk.Canvas):
    """Rotating fan glyph: a commanded-state indicator, not a measurement.

    The spin rate is deliberately fixed and unrelated to the duty cycle. Nothing
    in this demo measures fan speed -- the RPM figure shown next to this widget
    is duty rescaled by a constant -- so animating proportionally to it would
    dress a typed-in number up as telemetry.

    The rate is also capped well below the aliasing limit. `blades` blades give
    the rotor a 360/blades visual period, so any frame step past half of that
    reads as rotation the other way (the wagon-wheel effect). At the defaults
    each frame advances 21.6 deg against a 36 deg budget.

    Tk has no canvas rotation transform, so each frame recomputes blade vertices
    and pushes them with itemcoords. Only the geometry is touched per frame;
    colours change on the much rarer theme/state transitions via render().

    The glyph is drawn to the size actually allocated, not to a fixed request:
    it lives in a weight-shared grid cell whose height tracks the window, so a
    hard-coded size gets clipped from the bottom when the cell is shorter than
    the request (and the request is what winfo_height() keeps reporting, which
    hides it from geometry assertions -- only a screenshot shows the clip).
    `max_size` caps it so it does not balloon on a tall window.
    """

    def __init__(self, master, app, max_size=150, min_size=44, blades=5,
                 rev_per_s=1.2, interval_ms=50):
        # width/height are a starting request only; <Configure> takes over.
        super().__init__(master, width=max_size, height=min_size,
                         highlightthickness=0, bd=0)
        self.app = app
        self._max_size, self._min_size = max_size, min_size
        self._size = min_size
        self._blades = blades
        self._interval = interval_ms
        self._step = rev_per_s * 360.0 * interval_ms / 1000.0
        self._phase = 0.0
        self._running = False
        self._enabled = False
        self._job = None
        self._blade_items = []

        self.bind("<Configure>", self._on_configure)
        # A pending after() would otherwise fire into a dead widget when the
        # root window closes; panels also call stop() from their cleanup().
        self.bind("<Destroy>", self._on_destroy)
        app.on_theme(self.render)
        self.render()

    def _on_configure(self, event):
        """Fit the glyph to the allocated box, clamped to [min, max]."""
        size = max(self._min_size,
                   min(self._max_size, event.width, event.height))
        if size != self._size:
            self._size = size
            self.render()

    # -- state -------------------------------------------------------------
    def start(self):
        if self._running:
            return
        self._running = True
        self.render()
        self._tick()

    def stop(self):
        self._running = False
        self._cancel()
        self.render()

    def set_enabled(self, enabled):
        """Disabled means 'no board attached', which is distinct from stopped."""
        self._enabled = enabled
        if not enabled:
            self._running = False
            self._cancel()
        self.render()

    def _cancel(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except (tk.TclError, ValueError):
                pass       # interpreter already tearing down
            self._job = None

    def _on_destroy(self, _event):
        self._running = False
        self._cancel()

    # -- animation ---------------------------------------------------------
    def _tick(self):
        if not self._running:
            return
        self._phase = (self._phase + self._step) % 360.0
        for i, item in enumerate(self._blade_items):
            try:
                self.coords(item, self._blade_points(i))
            except tk.TclError:
                return     # widget went away mid-flight
        self._job = self.after(self._interval, self._tick)

    # -- drawing -----------------------------------------------------------
    def _centre(self):
        """Centre of the allocated box, which may be wider than the glyph."""
        w = self.winfo_width()  or self._size
        h = self.winfo_height() or self._size
        return w / 2.0, h / 2.0

    def _blade_points(self, index):
        """Flat [x0, y0, x1, y1, ...] for blade `index` at the current phase."""
        cx, cy = self._centre()
        r = self._size * 0.36                  # rotor radius, inside the ring
        base = self._phase + index * 360.0 / self._blades
        pts = []
        for frac, off in _BLADE_PROFILE:
            a = math.radians(base + off)
            pts.extend((cx + r * frac * math.cos(a),
                        cy + r * frac * math.sin(a)))
        return pts

    def render(self):
        t = self.app.theme
        self.delete("all")
        self.configure(bg=self.app.parent_bg(self))
        self._blade_items = []

        if not self._enabled:
            accent = t["text_dis"]
        elif self._running:
            accent = t["primary"]
        else:
            accent = t["text2"]

        cx, cy = self._centre()
        ring_r = self._size * 0.46

        # Housing: the fan reads as mounted hardware rather than a loose glyph.
        self.create_oval(cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r,
                         outline=t["border"], width=2)

        for i in range(self._blades):
            self._blade_items.append(
                self.create_polygon(self._blade_points(i), smooth=True,
                                    fill=accent, outline=accent, width=1))

        hub_r = self._size * 0.085
        self.create_oval(cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r,
                         fill=t["card"], outline=accent, width=2)
