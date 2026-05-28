"""Part-6 reasonableness checks."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True)
class ValidationIssue:
    severity: str  # "warn" or "error"
    message: str


def validate(
    cfg: Config,
    terminal_totals: dict[str, float],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    for terminal, total in terminal_totals.items():
        nameplate = cfg.terminal_nameplate.get(terminal)
        if not nameplate:
            continue
        if total > nameplate * 1.10:
            issues.append(ValidationIssue(
                "warn",
                f"{terminal}: total {total:.0f} MMcf/d is >10% above nameplate "
                f"{nameplate:.0f} — possible double-counted meter or wrong direction.",
            ))
        if total > 0 and total < nameplate * 0.20:
            issues.append(ValidationIssue(
                "warn",
                f"{terminal}: total {total:.0f} MMcf/d is <20% of nameplate "
                f"{nameplate:.0f} — possible outage, maintenance, or scraper miss.",
            ))

    us_total = sum(terminal_totals.values())
    if us_total < cfg.us_total_min_mmcfd or us_total > cfg.us_total_max_mmcfd:
        issues.append(ValidationIssue(
            "warn",
            f"U.S. total {us_total:.0f} MMcf/d is outside the expected operating range "
            f"[{cfg.us_total_min_mmcfd:.0f}, {cfg.us_total_max_mmcfd:.0f}] — "
            "verify scrapers and direction filter.",
        ))
    return issues
