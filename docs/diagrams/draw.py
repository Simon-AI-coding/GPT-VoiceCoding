"""Draw the README's diagrams as light and dark SVGs in docs/images/.

    python3 docs/diagrams/draw.py

The canvas is 880 wide, GitHub's README column, so a size written here is the size a reader
sees. Every box and edge is read off the code; docs/github-promotion.local.md (sessions 6 and 7)
records where each comes from. Dashes moving along an edge show which way data goes; they stop
for readers who ask for reduced motion.
"""

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

IMAGES = Path(__file__).resolve().parent.parent / "images"
WIDTH = 880
FONT = 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace'


@dataclass(frozen=True)
class Palette:
    background: str
    dots: str
    card: str
    stroke: str
    group: str
    text: str
    dim: str
    line: str
    accent: str
    glow: float


LIGHT = Palette(
    background="#ffffff",
    dots="#dfe3e8",
    card="#f6f8fa",
    stroke="#d0d7de",
    group="#afb8c1",
    text="#1f2328",
    dim="#656d76",
    line="#c4cad1",
    accent="#c26a00",
    glow=0.25,
)
DARK = Palette(
    background="#0d1117",
    dots="#1c232c",
    card="#131a22",
    stroke="#2d3540",
    group="#3d4652",
    text="#e6edf3",
    dim="#7d8590",
    line="#343c47",
    accent="#f0a030",
    glow=0.55,
)
CLAUDE = "#d97757"
CODEX = "#8b95a5"


class Canvas:
    def __init__(self, height: int, title: str):
        self.height = height
        self.title = title
        self.lines: list[str] = []
        self.parts: list = []
        self.keyframes: list[str] = []

    def add(self, markup: str) -> None:
        self.parts.append(markup)

    def glow(self, x, y, w, h) -> None:
        """A blurred accent halo behind the box at the same place, coloured per palette."""
        self.parts.append((x, y, w, h))

    def text(self, x, y, value, size=12, cls="text", anchor="middle", weight=400, spacing=0):
        self.add(
            f'<text x="{x}" y="{y}" class="{cls}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" letter-spacing="{spacing}">{escape(value)}</text>'
        )

    def card(self, x, y, w, h, title, sub=None, cls="card", dot=None):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" class="{cls}"/>')
        cx = x + w / 2
        if sub is None:
            ty = y + h / 2 + 4.5
        else:
            ty = y + h / 2 - 3
        if dot:
            label_w = len(title) * 7.8
            self.add(f'<circle cx="{cx - label_w / 2 - 9}" cy="{ty - 4.5}" r="4" fill="{dot}"/>')
            cx += 7
        self.text(cx, ty, title, size=13, weight=600)
        if sub is not None:
            self.text(x + w / 2, ty + 17, sub, size=11, cls="dim")

    def group(self, x, y, w, h, label, note=""):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" class="group"/>')
        self.add(
            f'<rect x="{x + 14}" y="{y - 9}" width="{(len(label) + len(note)) * 7 + 22}" '
            f'height="18" class="bg"/>'
        )
        self.text(x + 22, y + 4, label, size=12, cls="accent", anchor="start", weight=600)
        if note:
            self.text(
                x + 22 + len(label) * 7.25 + 8, y + 4, note, size=11, cls="dim", anchor="start"
            )

    def edge(self, points, flow="forward", arrow_end=True, arrow_start=False, speed=1.6):
        path = rounded(points)
        markers = ""
        if arrow_end:
            markers += ' marker-end="url(#arrow)"'
        if arrow_start:
            markers += ' marker-start="url(#arrow-back)"'
        self.lines.append(f'<path d="{path}" class="line"{markers}/>')
        directions = {"forward": ["fwd"], "back": ["back"], "both": ["fwd", "back"]}[flow]
        for d in directions:
            self.lines.append(
                f'<path d="{path}" class="flow {d}" style="animation-duration:{speed}s"/>'
            )

    def pulse(self, path, length, index, count, period):
        """A spark that runs once along `path` in its own slot of a repeating sequence."""
        share = 100 / count
        self.keyframes.append(
            f"@keyframes pulse{index} {{ 0% {{ stroke-dashoffset: 10; opacity: 1; }} "
            f"{share * 0.9:.2f}% {{ stroke-dashoffset: {-length}; opacity: 1; }} "
            f"{share * 0.9 + 0.01:.2f}%, 100% {{ stroke-dashoffset: {-length}; opacity: 0; }} }}"
        )
        self.parts.append(
            f'<path d="{path}" class="pulse" style="stroke-dasharray: 10 {length + 20}; '
            f"animation: pulse{index} {period}s linear {index * period / count:.2f}s infinite; "
            f'opacity: 0"/>'
        )

    @staticmethod
    def halo(p: Palette, x, y, w, h) -> str:
        return (
            f'<g opacity="{p.glow}" filter="url(#glow)"><rect x="{x}" y="{y}" width="{w}" '
            f'height="{h}" rx="10" fill="{p.accent}"/></g>'
        )

    def render(self, p: Palette) -> str:
        style = f"""
    text {{ font-family: {FONT}; }}
    .bg {{ fill: {p.background}; }}
    .text {{ fill: {p.text}; }}
    .dim {{ fill: {p.dim}; }}
    .accent {{ fill: {p.accent}; }}
    .card {{ fill: {p.card}; stroke: {p.stroke}; stroke-width: 1; }}
    .core {{ fill: {p.card}; stroke: {p.accent}; stroke-width: 1.5; }}
    .group {{ fill: none; stroke: {p.group}; stroke-width: 1; stroke-dasharray: 4 4; }}
    .line {{ fill: none; stroke: {p.line}; stroke-width: 1.5; }}
    .flow {{ fill: none; stroke: {p.accent}; stroke-width: 1.5; stroke-linecap: round;
             stroke-dasharray: 2 14; animation: flow linear infinite; }}
    .flow.back {{ animation-name: flow-back; }}
    .lifeline {{ stroke: {p.line}; stroke-width: 1; stroke-dasharray: 3 5; }}
    @keyframes flow {{ from {{ stroke-dashoffset: 16; }} to {{ stroke-dashoffset: 0; }} }}
    @keyframes flow-back {{ from {{ stroke-dashoffset: 0; }} to {{ stroke-dashoffset: 16; }} }}
    .pulse {{ fill: none; stroke: {p.accent}; stroke-width: 3; stroke-linecap: round; }}
    .label-bg {{ fill: {p.background}; }}
    {" ".join(self.keyframes)}
    @media (prefers-reduced-motion: reduce) {{ .flow, .pulse {{ animation: none; }} }}
"""
        defs = f"""
  <defs>
    <pattern id="dots" width="20" height="20" patternUnits="userSpaceOnUse">
      <circle cx="1" cy="1" r="1" fill="{p.dots}"/>
    </pattern>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
            orient="auto-start-reverse">
      <path d="M0,1 L9,5 L0,9 z" fill="{p.line}"/>
    </marker>
    <marker id="arrow-back" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7"
            markerHeight="7" orient="auto-start-reverse">
      <path d="M0,1 L9,5 L0,9 z" fill="{p.line}"/>
    </marker>
    <filter id="glow" x="-30%" y="-60%" width="160%" height="220%">
      <feGaussianBlur stdDeviation="10"/>
    </filter>
  </defs>"""
        body = "\n  ".join(
            self.lines
            + [part if isinstance(part, str) else self.halo(p, *part) for part in self.parts]
        )
        size = f'viewBox="0 0 {WIDTH} {self.height}" width="{WIDTH}" height="{self.height}"'
        return f"""<svg xmlns="http://www.w3.org/2000/svg" {size} role="img">
  <title>{escape(self.title)}</title>
  <style>{style}  </style>{defs}
  <rect width="{WIDTH}" height="{self.height}" rx="14" class="bg"/>
  <rect width="{WIDTH}" height="{self.height}" rx="14" fill="url(#dots)"/>
  {body}
</svg>
"""


def rounded(points, radius=10):
    """An orthogonal polyline with its corners rounded."""
    d = f"M{points[0][0]},{points[0][1]}"
    for i in range(1, len(points) - 1):
        (x0, y0), (x1, y1), (x2, y2) = points[i - 1], points[i], points[i + 1]
        r = min(radius, abs(x1 - x0 + y1 - y0) / 2, abs(x2 - x1 + y2 - y1) / 2)
        ax = x1 - r * sign(x1 - x0)
        ay = y1 - r * sign(y1 - y0)
        bx = x1 + r * sign(x2 - x1)
        by = y1 + r * sign(y2 - y1)
        d += f" L{ax},{ay} Q{x1},{y1} {bx},{by}"
    d += f" L{points[-1][0]},{points[-1][1]}"
    return d


def sign(v):
    return (v > 0) - (v < 0)


def architecture() -> Canvas:
    c = Canvas(600, "GPT-VoiceCoding architecture")
    c.text(40, 30, "gpt-voicecoding", size=11, cls="dim", anchor="start", spacing=1)
    c.text(840, 30, "architecture", size=11, cls="dim", anchor="end", spacing=1)

    # Your terminals, and the Codex daemon they join.
    c.group(40, 62, 400, 96, "~/terminals", "started by you")
    c.card(60, 94, 170, 44, "claude code", dot=CLAUDE)
    c.card(250, 94, 170, 44, "codex", dot=CODEX)
    c.card(520, 86, 320, 60, "codex app-server", "started by us, joined by your sessions")

    # The engine.
    c.group(40, 206, 800, 270, "engine", "python · asyncio")
    c.card(60, 236, 170, 44, "agent adapters")
    c.card(660, 236, 160, 44, "call adapter")
    c.glow(300, 302, 280, 76)
    c.card(300, 302, 280, 76, "BRIDGE CORE", "every policy · one source of truth", cls="core")
    c.card(60, 414, 200, 40, "companion channel")
    c.card(330, 414, 220, 40, "control plane", None)

    # Outside the engine.
    c.card(60, 520, 220, 56, "telegram", "reply from your phone")
    c.card(300, 520, 200, 56, "menu-bar app", "duty lamp · control panel")
    c.card(520, 520, 110, 56, "bridgectl", "cli")
    c.card(650, 520, 190, 56, "live call", "voice speaks · agent acts")

    # Edges, in the direction data moves.
    c.edge([(145, 138), (145, 236)], flow="both", arrow_start=True)
    c.text(155, 186, "hooks · roster · inbox socket", size=11, cls="dim", anchor="start")
    c.edge([(420, 116), (520, 116)], flow="both", arrow_end=False)
    c.edge([(580, 146), (580, 258), (230, 258)], flow="both")
    c.edge([(740, 146), (740, 236)], speed=1.2)
    c.text(730, 186, "realtime route", size=11, cls="dim", anchor="end")
    c.edge([(145, 280), (145, 340), (300, 340)], flow="both", arrow_end=False)
    c.edge([(580, 340), (620, 340), (620, 258), (660, 258)], flow="both", arrow_end=False)
    c.edge([(360, 378), (360, 396), (160, 396), (160, 414)], flow="both", arrow_end=False)
    c.edge([(440, 378), (440, 414)], flow="both", arrow_end=False)
    c.edge([(160, 454), (160, 520)], flow="both", arrow_end=False)
    c.edge([(400, 454), (400, 520)], flow="both", arrow_end=False)
    c.edge([(500, 454), (500, 490), (575, 490), (575, 520)], flow="both", arrow_end=False)
    c.edge([(740, 280), (740, 520)], flow="both", arrow_end=False)
    return c


ACTORS = ["session", "bridge core", "call keeper", "call agent", "voice", "you", "telegram"]
COLUMN = {name: 80 + 120 * i for i, name in enumerate(ACTORS)}

# What happens when a Session stops, read off core/bridge.py:_session_stopped: the reading goes
# to the roster, the Stop Notice goes out as an Anchor, then the Call Keeper is woken, and it
# briefs from the roster when it dials (ADR 0017). ("group", label, note) opens a box and
# ("end", "", "") closes it.
STOP = [
    ("session", "bridge core", "stops, waiting on a decision"),
    ("bridge core", "bridge core", "writes the reading to the roster"),
    ("bridge core", "telegram", "stop notice, as an anchor you can reply to"),
    ("bridge core", "call keeper", "wake"),
    ("group", "on the call", ""),
    ("call keeper", "voice", "dials, briefed from the roster as it stands"),
    ("voice", "you", "speaks the session brief"),
    ("you", "voice", '"use email login, and give me the steps"'),
    ("voice", "call agent", "hands the job over"),
    ("call agent", "bridge core", "relay"),
    ("end", "", ""),
    ("group", "or on telegram", "no call needed"),
    ("telegram", "you", "stop notice"),
    ("you", "telegram", "replies to it"),
    ("telegram", "bridge core", "inbound text, naming the anchor's session"),
    ("end", "", ""),
    ("bridge core", "session", "answer relay"),
]
STEP = 40


def sequence() -> Canvas:
    rows = []
    y = 136
    for item in STOP:
        if item[0] == "group":
            y += 14
            rows.append((y, item))
            y += 34
        elif item[0] == "end":
            rows.append((y - 16, item))
            y += 18
        else:
            rows.append((y, item))
            y += STEP + (16 if item[0] == item[1] else 0)
    height = y + 20
    c = Canvas(height, "What happens when a session stops")
    c.text(40, 30, "gpt-voicecoding", size=11, cls="dim", anchor="start", spacing=1)
    c.text(840, 30, "a session stops", size=11, cls="dim", anchor="end", spacing=1)

    for name, x in COLUMN.items():
        c.lines.append(f'<line x1="{x}" y1="90" x2="{x}" y2="{height - 24}" class="lifeline"/>')
        cls = "core" if name == "bridge core" else "card"
        if name == "bridge core":
            c.glow(x - 52, 56, 104, 34)
        c.card(x - 52, 56, 104, 34, name, cls=cls)

    messages = [row for row in rows if row[1][0] not in ("group", "end")]
    period = len(messages) * 0.9
    opened = None
    for y, (source, target, label) in rows:
        if source == "group":
            opened = (y, target, label)
            continue
        if source == "end":
            top, name, note = opened
            left, right = COLUMN["bridge core"] - 30, 860
            c.lines.append(
                f'<rect x="{left}" y="{top}" width="{right - left}" height="{y - top}" rx="10" '
                f'class="group"/>'
            )
            label_w = (len(name) + len(note)) * 7 + 24
            c.add(f'<rect x="{left + 12}" y="{top - 9}" width="{label_w}" height="18" class="bg"/>')
            c.text(left + 20, top + 4, name, size=12, cls="accent", anchor="start", weight=600)
            if note:
                c.text(
                    left + 20 + len(name) * 7.25 + 8,
                    top + 4,
                    note,
                    size=11,
                    cls="dim",
                    anchor="start",
                )
            continue
        x1, x2 = COLUMN[source], COLUMN[target]
        index = messages.index((y, (source, target, label)))
        if source == target:
            path = rounded([(x1, y), (x1 + 34, y), (x1 + 34, y + 16), (x1 + 4, y + 16)], 6)
            length = 34 + 16 + 30
            c.add(f'<path d="{path}" class="line" marker-end="url(#arrow)"/>')
            c.text(x1 + 44, y + 12, label, size=11, cls="dim", anchor="start")
        else:
            end = x2 - 4 * sign(x2 - x1)
            path = f"M{x1},{y} L{end},{y}"
            length = abs(end - x1)
            c.add(f'<path d="{path}" class="line" marker-end="url(#arrow)"/>')
            mid = (x1 + x2) / 2
            width = len(label) * 6.7 + 12
            c.add(
                f'<rect x="{mid - width / 2}" y="{y - 19}" width="{width}" height="15" '
                f'rx="3" class="label-bg"/>'
            )
            c.text(mid, y - 8, label, size=11, cls="dim")
        c.pulse(path, length, index, len(messages), period)
    return c


def write(name: str, canvas: Canvas) -> None:
    for mode, palette in (("light", LIGHT), ("dark", DARK)):
        (IMAGES / f"{name}-{mode}.svg").write_text(canvas.render(palette))


if __name__ == "__main__":
    write("architecture", architecture())
    write("session-stops", sequence())
