"""Render a ChartDataset to a PNG (the JSON dataset is the table-view twin).

Design rules (dataviz skill): one validated categorical palette applied in a FIXED slot
order; a series keeps its colour wherever it appears (colour follows the entity, never its
rank); marker shapes are a secondary encoding (aqua/yellow sit below 3:1 on the light
surface, so shape + legend + direct labels carry identity); one y-axis only; solid
hairline gridlines; thin marks; CI bands on numeric x, dodged whiskers on categories;
never more than 8 series.

Honesty: mock and synthetic data are watermarked (diagonal + corner badge); the footer
states the target and that the table view is the JSON.
"""
from __future__ import annotations

import io
import re
import textwrap
from typing import Sequence

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, PercentFormatter

from fairdrop_sim.models import ChartDataset, Series

# --- reference palette (validated light mode: scripts/validate_palette.js, slots 1-4 pass) ---
SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, CRITICAL = "#e1e0d9", "#c3c2b7", "#d03b3b"
SLOTS = {1: "#2a78d6", 2: "#eb6834", 3: "#1baf7a", 4: "#eda100", 5: "#e87ba4", 6: "#008300",
         7: "#4a3aa7", 8: "#e34948"}
MARKERS = {1: "o", 2: "s", 3: "^", 4: "D", 5: "v", 6: "P", 7: "X", 8: "h"}
FONTS = ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"]
PCT = re.compile(r"^p\d+$")


class TooManySeries(ValueError):
    pass


def is_baseline(name: str) -> bool:
    n = name.lower()
    return "baseline" in n or "neutral" in n


def preferred_slot(name: str) -> int | None:
    """Stable entity -> slot rules, so FCFS is always orange, 'no defences' always blue, etc."""
    n = name.lower().strip()
    if n.startswith("fcfs") or "first come" in n:
        return 2
    if n == "none" or "no defence" in n or n.endswith(": none"):
        return 1
    if n == "all" or "all defence" in n or "all layers" in n or n.endswith(": all"):
        return 3
    if n in ("rate_limit+pow", "rate limit + pow") or n.endswith(": rate_limit+pow"):
        return 4
    if "under attack" in n or n.endswith("attack") or n.startswith("recall") or n.endswith(": recall"):
        return 2
    if n.startswith("normal") or n.startswith("precision") or n.endswith(": precision") or n.startswith("spike"):
        return 1
    if n.startswith("false-positive") or n.endswith(": false-positive rate"):
        return 3
    if n.startswith("uniform"):
        return 2
    return None


def assign_slots(names: Sequence[str]) -> dict[str, int]:
    """name -> slot (1..8). Baselines get no slot. Raises past 8 series (fold into Other)."""
    real = [n for n in names if not is_baseline(n)]
    if len(real) > 8:
        raise TooManySeries(f"{len(real)} series; a 9th categorical hue is never generated. "
                            "Fold the tail into 'Other' or facet the chart.")
    out: dict[str, int] = {}
    used: set[int] = set()
    for n in real:  # preferred slots first, so a later preferred name can't be displaced
        s = preferred_slot(n)
        if s is not None and s not in used:
            out[n] = s
            used.add(s)
    for n in real:
        if n not in out:
            free = next(s for s in range(1, 9) if s not in used)
            out[n] = free
            used.add(free)
    return out


def watermark_text(chart: ChartDataset) -> str | None:
    if chart.synthetic:
        return "SYNTHETIC DATA"
    if chart.target == "mock":
        return "MOCK DATA"
    return None


def _font() -> str:
    from matplotlib import font_manager

    have = {f.name for f in font_manager.fontManager.ttflist}
    return next((f for f in FONTS if f in have), "DejaVu Sans")


def _percent_axis(chart: ChartDataset) -> bool:
    ys = [p.y for s in chart.series for p in s.points]
    text = (chart.y_label + chart.chart_id).lower()
    return bool(ys) and min(ys) >= 0 and max(ys) <= 1.05 and "ms" not in text and "latency" not in text


def _x_positions(chart: ChartDataset) -> tuple[list, bool]:
    """(ordered x values, numeric?)"""
    seen: list = []
    for s in chart.series:
        for p in s.points:
            if p.x not in seen:
                seen.append(p.x)
    numeric = chart.x_scale != "category" and all(isinstance(x, (int, float)) for x in seen)
    if numeric:
        seen.sort()
    return seen, numeric


def render_png(chart: ChartDataset, width_in: float = 8.0, height_in: float = 5.0, dpi: int = 160) -> bytes:
    fig = Figure(figsize=(width_in, height_in), dpi=dpi, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    font = _font()
    ax = fig.add_axes([0.095, 0.2, 0.86, 0.6], facecolor=SURFACE)

    names = [s.name for s in chart.series]
    slots = assign_slots(names)
    xs, numeric = _x_positions(chart)
    pos = {x: (float(x) if numeric else float(i)) for i, x in enumerate(xs)}
    curves = [s for s in chart.series if not is_baseline(s.name)]
    connect = numeric or all(PCT.match(str(x)) for x in xs)
    dodge = 0.0 if connect or len(curves) < 2 else 0.18
    if connect and curves:  # leave room on the right for the direct end labels
        ax.set_position([0.095, 0.2, 0.69, 0.6])

    for si, s in enumerate(curves):
        slot = slots[s.name]
        color, marker = SLOTS[slot], MARKERS[slot]
        off = (si - (len(curves) - 1) / 2) * dodge
        pts = sorted(s.points, key=lambda p: pos[p.x])
        px = [pos[p.x] + off for p in pts]
        py = [p.y for p in pts]
        has_ci = [p for p in pts if p.ci_low is not None and p.ci_high is not None]
        if connect:
            ax.plot(px, py, color=color, lw=2.0, zorder=3, solid_capstyle="round")
            if numeric and len(has_ci) == len(pts) and len(pts) > 1:
                ax.fill_between(px, [p.ci_low for p in pts], [p.ci_high for p in pts], color=color, alpha=0.16,
                                lw=0, zorder=2)
        if has_ci and (not connect or not numeric or len(has_ci) != len(pts)):
            lo = [p.y - p.ci_low for p in has_ci]
            hi = [p.ci_high - p.y for p in has_ci]
            ax.errorbar([pos[p.x] + off for p in has_ci], [p.y for p in has_ci], yerr=[lo, hi], fmt="none",
                        ecolor=color, elinewidth=1.4, capsize=3, zorder=3)
        ax.plot(px, py, linestyle="none", marker=marker, ms=7, mfc=color, mec=SURFACE,
                mew=1.5, zorder=4)

    for s in chart.series:  # baselines: neutral reference, dashed, no markers
        if is_baseline(s.name):
            pts = sorted(s.points, key=lambda p: pos[p.x])
            ax.plot([pos[p.x] for p in pts], [p.y for p in pts], color=MUTED, lw=1.4, ls=(0, (4, 3)), zorder=2)

    # axes chrome: hairline solid grid, recessive spines
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color(AXIS)
    ax.grid(axis="y", color=GRID, lw=0.8, ls="-", zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(colors=MUTED, labelsize=8.5, length=3, width=0.8, color=AXIS)
    if numeric and chart.x_scale == "log" and all(x > 0 for x in xs):
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:g}"))
        ax.set_xticks(xs)
        ax.minorticks_off()
        ax.margins(x=0.08)
    elif numeric:
        ax.set_xticks(xs)
        ax.margins(x=0.06)
    else:
        ax.set_xticks(list(pos.values()))
        ax.set_xticklabels([str(x) for x in xs])
        ax.set_xlim(-0.6, len(xs) - 0.4)
    if chart.y_scale == "log":
        ax.set_yscale("log")
    else:
        ax.set_ylim(bottom=0)
        if _percent_axis(chart):
            top = max(p.ci_high if p.ci_high is not None else p.y for s in chart.series for p in s.points)
            ax.set_ylim(0, min(1.0, max(top * 1.15, 0.05)))
            ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0 if top > 0.1 else 1))
        else:
            ax.margins(y=0.08)
            ax.set_ylim(bottom=0)
    ax.set_xlabel(chart.x_label, color=INK2, fontsize=9, labelpad=6, fontfamily=font)
    ax.set_ylabel(chart.y_label, color=INK2, fontsize=9, labelpad=6, fontfamily=font)
    for lab in ax.get_xticklabels() + ax.get_yticklabels():
        lab.set_fontfamily(font)

    # direct end labels (connected charts only), nudged apart so they never collide; the text
    # wears ink, not the series colour (the marker in the legend carries identity)
    if connect and curves:
        ends = sorted(((max(s.points, key=lambda p: pos[p.x]), s) for s in curves), key=lambda t: t[0].y)
        floor_px = None
        for p, s in ends:
            y_px = ax.transData.transform((pos[p.x], p.y))[1]
            target_px = y_px if floor_px is None else max(y_px, floor_px + 7.5 * dpi / 72 * 1.3)
            floor_px = target_px
            ax.annotate(s.name, xy=(pos[p.x], p.y), xytext=(10, (target_px - y_px) * 72 / dpi),
                        textcoords="offset points", fontsize=7.5, color=INK2, va="center", fontfamily=font,
                        annotation_clip=False)

    # title / legend / footer
    fig.text(0.095, 0.945, chart.title, color=INK, fontsize=13, fontweight="bold", fontfamily=font, va="top")
    if len(chart.series) >= 2:
        from matplotlib.lines import Line2D

        handles = []
        for s in chart.series:
            if is_baseline(s.name):
                handles.append(Line2D([0], [0], color=MUTED, lw=1.4, ls=(0, (4, 3)), label=s.name))
            else:
                sl = slots[s.name]
                handles.append(Line2D([0], [0], marker=MARKERS[sl], color=SLOTS[sl], mfc=SLOTS[sl], mec=SURFACE,
                                      ms=7, lw=2 if connect else 0, label=s.name))
        leg = fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.085, 0.885), ncol=min(len(handles), 3),
                         frameon=False, fontsize=8.5, handlelength=1.8, columnspacing=1.6)
        for t in leg.get_texts():
            t.set_color(INK2)
            t.set_fontfamily(font)

    foot = f"Target: {chart.target}" + (" · SYNTHETIC" if chart.synthetic else "") + \
        " · intervals: 95% CI where shown · full values in the JSON table view"
    fig.text(0.095, 0.085, foot, color=MUTED, fontsize=7, fontfamily=font, va="top")
    if chart.notes:
        wrapped = textwrap.wrap(chart.notes, 150)[:3]
        fig.text(0.095, 0.055, "\n".join(wrapped), color=MUTED, fontsize=6.5, fontfamily=font, va="top")

    wm = watermark_text(chart)
    if wm:
        fig.text(0.5, 0.5, wm, color=MUTED, alpha=0.16, fontsize=54, fontweight="bold", rotation=24, ha="center",
                 va="center", fontfamily=font, zorder=10)
        fig.text(0.955, 0.955, wm, color=CRITICAL, fontsize=8.5, fontweight="bold", ha="right", va="top",
                 fontfamily=font, bbox=dict(boxstyle="round,pad=0.35", fc=SURFACE, ec=CRITICAL, lw=1.2))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE, dpi=dpi)
    return buf.getvalue()


def save_png(chart: ChartDataset, path) -> None:
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(render_png(chart))
