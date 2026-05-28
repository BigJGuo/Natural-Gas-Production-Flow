"""Generate the Part-5 analysis prompt with computed deltas."""
from __future__ import annotations

from pathlib import Path

from ..storage.export import TERMINAL_ORDER
from .stats import DailyStats

PROMPT_TEMPLATE = """\
You are a natural gas market analyst. I am providing you with today's U.S. LNG feedgas
data pulled from pipeline EBBs. Please analyze this data accurately.

Gas Day: {gas_day}
Nomination Cycle: {cycle}

Scope: U.S. LNG feedgas only. The total below is the sum of the LNG terminals
listed (cross-border Mexico exports and Canada flows are tracked separately and
are NOT included here).

FEEDGAS DATA (MMcf/d):
{terminal_lines}
TOTAL U.S. LNG:    {us_total} MMcf/d

Prior gas day total: {prior_day} MMcf/d
7-day average:       {seven_day} MMcf/d
30-day average:      {thirty_day} MMcf/d

Known outages / maintenance today: [FILL IN MANUALLY OR "NONE KNOWN"]

Please provide:
1. Day-over-day change and which terminals drove it
2. Any terminal running notably below its nameplate capacity (flag if >20% below)
3. Week-over-week trend summary
4. Any anomalies or data points worth flagging
5. A one-line total feedgas summary suitable for a morning report
"""


def _fmt(v: float | None) -> str:
    if v is None:
        return "[N/A — insufficient history]"
    return f"{v:,.0f}"


def render_prompt(stats: DailyStats) -> str:
    lines = []
    label_width = 18
    for terminal in TERMINAL_ORDER:
        total = stats.terminal_totals.get(terminal, 0.0)
        lines.append(f"{(terminal + ':').ljust(label_width)} {_fmt(total)} MMcf/d")
    return PROMPT_TEMPLATE.format(
        gas_day=stats.gas_day.isoformat(),
        cycle=stats.cycle.upper(),
        terminal_lines="\n".join(lines),
        us_total=_fmt(stats.us_total),
        prior_day=_fmt(stats.prior_day_us_total),
        seven_day=_fmt(stats.seven_day_avg),
        thirty_day=_fmt(stats.thirty_day_avg),
    )


def write_prompt(stats: DailyStats, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_prompt(stats), encoding="utf-8")
    return out_path
