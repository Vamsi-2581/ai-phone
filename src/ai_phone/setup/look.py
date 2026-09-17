"""The look of `ai-phone setup`: colours, the pixel wordmark, the step crumbs, the menu and the CSS."""
from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from ai_phone.setup.ui import STEPS, Check

BG = "#06060b"
LINE = "#20223a"
DIM = "#5f6475"
MID = "#9aa0b2"
WHITE = "#f3f4f8"
GREEN = "#3ddc97"
AMBER = "#f5b454"
RED = "#ff6b7a"
ACCENT = "#7c8cff"
DEPTH_NEAR = "#2a2780"
DEPTH_FAR = "#15143f"
STOPS = [(46, 84, 255), (98, 70, 255), (150, 84, 255), (190, 110, 255)]

FONT = {
    "A": [" ##### ", "#######", "##   ##", "##   ##", "##   ##", "#######", "#######", "##   ##", "##   ##", "##   ##", "##   ##", "##   ##"],
    "I": ["######", "######", "  ##  ", "  ##  ", "  ##  ", "  ##  ", "  ##  ", "  ##  ", "  ##  ", "  ##  ", "######", "######"],
    "-": ["      ", "      ", "      ", "      ", "      ", "######", "######", "      ", "      ", "      ", "      ", "      "],
    "P": ["###### ", "#######", "##   ##", "##   ##", "##   ##", "#######", "###### ", "##     ", "##     ", "##     ", "##     ", "##     "],
    "H": ["##   ##", "##   ##", "##   ##", "##   ##", "##   ##", "#######", "#######", "##   ##", "##   ##", "##   ##", "##   ##", "##   ##"],
    "O": [" ##### ", "#######", "##   ##", "##   ##", "##   ##", "##   ##", "##   ##", "##   ##", "##   ##", "##   ##", "#######", " ##### "],
    "N": ["##   ##", "##   ##", "###  ##", "###  ##", "#### ##", "#### ##", "## ####", "## ####", "##  ###", "##  ###", "##   ##", "##   ##"],
    "E": ["#######", "#######", "##     ", "##     ", "##     ", "###### ", "###### ", "##     ", "##     ", "##     ", "#######", "#######"],
}
WORDMARK_MIN_WIDTH = 78  # narrower terminals get a plain title on Welcome
CRUMBS_MIN_WIDTH = 92  # narrower terminals get "● Line  3/7"

MENU_KEYS = "↑↓ move   ⏎ select   1-9 jump   esc back   q quit"
FORM_KEYS = "⏎ next field   esc back   ctrl+c quit"
BUSY_KEYS = "ctrl+c quit"


def gradient(t: float) -> str:
    t = max(0.0, min(1.0, t)) * (len(STOPS) - 1)
    i = min(int(t), len(STOPS) - 2)
    f = t - i
    r, g, b = (round(a + (c - a) * f) for a, c in zip(STOPS[i], STOPS[i + 1]))
    return f"#{r:02x}{g:02x}{b:02x}"


def lighten(hex_colour: str, amount: float) -> str:
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    r, g, b = (round(v + (255 - v) * amount) for v in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def wordmark(word: str = "AI-PHONE", gap: int = 2) -> Text:
    """Pixel letters drawn with half blocks (square pixels), a blue-to-violet gradient, and a stepped drop
    shadow behind them for depth."""
    layers = [((2, 2), DEPTH_FAR), ((1, 1), DEPTH_NEAR)]
    height = len(FONT["A"])
    width = sum(len(FONT[c][0]) for c in word) + gap * (len(word) - 1)
    W, H = width + 3, height + 3 + (height + 3) % 2
    on = [[False] * W for _ in range(H)]
    x = 0
    for c in word:
        glyph = FONT[c]
        for y, row in enumerate(glyph):
            for dx, ch in enumerate(row):
                if ch == "#":
                    on[y][x + dx] = True
        x += len(glyph[0]) + gap

    def lit(px, py):
        return 0 <= px < W and 0 <= py < H and on[py][px]

    def colour(px, py):
        if lit(px, py):
            base = gradient(px / (width - 1) * 0.8 + py / (height - 1) * 0.2)
            top_edge = lambda qx: lit(qx, py) and not lit(qx, py - 1)
            shine = top_edge(px) and (top_edge(px - 1) or top_edge(px + 1))
            return lighten(base, 0.35) if shine else base
        for (ox, oy), shade in reversed(layers):
            if lit(px - ox, py - oy):
                return shade
        return None

    text = Text(no_wrap=True)
    for r in range(H // 2):
        for px in range(W):
            top, bottom = colour(px, 2 * r), colour(px, 2 * r + 1)
            if top is None and bottom is None:
                text.append(" ")
            elif top == bottom:
                text.append("█", Style(color=top))
            elif bottom is None:
                text.append("▀", Style(color=top))
            elif top is None:
                text.append("▄", Style(color=bottom))
            else:
                text.append("▀", Style(color=top, bgcolor=bottom))
        if r < H // 2 - 1:
            text.append("\n")
    return text


def crumbs(active: int, width: int = 200) -> Text:
    if width < CRUMBS_MIN_WIDTH:
        return Text(f"● {STEPS[active]}  {active + 1}/{len(STEPS)}", style=Style(color=ACCENT, bold=True), no_wrap=True)
    t = Text(no_wrap=True)
    for i, name in enumerate(STEPS):
        if i:
            t.append("  ─  ", Style(color=LINE))
        if i < active:
            t.append(f"✓ {name}", Style(color=GREEN))
        elif i == active:
            t.append(f"● {name}", Style(color=ACCENT, bold=True))
            t.append(" ←", Style(color=ACCENT))
        else:
            t.append(f"○ {name}", Style(color=DIM))
    return t


def checks_text(rows: list[Check]) -> Text:
    t = Text(no_wrap=True)
    label_w = max(len(r.label) for r in rows) + 3
    for i, row in enumerate(rows):
        t.append(row.label.ljust(label_w), Style(color=DIM))
        t.append(row.value, Style(color=WHITE))
        if row.ok is True:
            t.append("  ✓", Style(color=GREEN, bold=True))
        elif row.ok is False:
            t.append("  ✗", Style(color=RED, bold=True))
        else:
            t.append("  ○", Style(color=AMBER))
        if i < len(rows) - 1:
            t.append("\n")
    return t


def eyebrow(step: int) -> Text:
    words = f"STEP {step + 1} OF {len(STEPS)}".split() + ["·", STEPS[step].upper()]
    return Text("   ".join(" ".join(w) for w in words), style=Style(color="#5a64c8", bold=True))


class Menu(Static):
    """Numbered options as (label, hint) pairs; the selected one is marked and drawn in the accent colour."""

    index = reactive(0)

    def __init__(self, options: list[tuple[str, str]], index: int = 0, **kw):
        super().__init__(**kw)
        self.options = options
        self.set_reactive(Menu.index, index)

    def render(self) -> Text:
        t = Text(no_wrap=True)
        pad = max(len(label) for label, _ in self.options)
        for i, (label, hint) in enumerate(self.options):
            sel = i == self.index
            t.append("▸ " if sel else "  ", Style(color=ACCENT, bold=True))
            t.append(f"[{i + 1}] ", Style(color=ACCENT if sel else DIM, bold=sel))
            t.append(label.ljust(pad), Style(color=ACCENT if sel else MID, bold=sel))
            if hint:
                t.append(f"    {hint}", Style(color=DIM if not sel else "#8a93c8"))
            if i < len(self.options) - 1:
                t.append("\n")
        return t


def hint(keys: str = MENU_KEYS) -> Text:
    return Text(keys, style=Style(color="#3c4052"))


CSS = f"""
Screen {{ background: {BG}; align: center top; }}
#page {{ width: 100; max-width: 100%; height: auto; padding: 1 3; }}
#crumbs, #title {{ height: 1; }}
#rule {{ color: {LINE}; margin: 0 0 0 0; }}
#logo {{ width: 100%; text-align: center; margin: 1 0 1 0; }}
.centre {{ width: 100%; text-align: center; }}
#note {{ width: 100%; text-align: center; }}
#checks {{ width: auto; margin: 2 0 1 0; }}
#body {{ width: auto; margin: 1 0 0 0; }}
Center {{ height: auto; }}
#menu {{ width: auto; margin: 2 0 0 0; }}
#hint {{ width: auto; margin: 2 0 0 0; }}
.eyebrow {{ width: 100%; text-align: center; margin: 5 0 1 0; }}
.question {{ width: 100%; text-align: center; }}
.detail {{ width: 100%; text-align: center; margin: 0 0 1 0; }}
.error {{ width: 100%; text-align: center; color: {RED}; margin: 0 0 1 0; }}
Question #menu {{ margin: 2 0 0 0; }}
#form {{ height: auto; margin: 1 0 0 0; width: 66; }}
.label {{ margin: 1 0 0 1; }}
Input {{ background: {BG}; border: round #2a2d4a; color: {WHITE}; padding: 0 1; }}
Input:focus {{ border: round {ACCENT}; background: {BG}; background-tint: $foreground 0%; }}
Input > .input--placeholder {{ color: #3c4052; }}
Input:nocolor > .input--cursor {{ text-style: reverse; }}
#status {{ width: 66; margin: 1 0 0 1; height: 1; }}
ProgressBar {{ width: 66; margin: 1 0 0 0; }}
Bar > .bar--bar {{ color: {ACCENT}; background: {LINE}; }}
Bar > .bar--complete {{ color: {GREEN}; }}
"""
