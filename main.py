"""Forecast GTA VI's launch-day Twitch viewers, hour by hour.

Reads one case file per run (JSON, schema makefun/twitch-ccu-case/v1) from the
input directory and writes a 23-row forecast (hours 2..24, five quantiles each)
for that case to the output directory. Runs offline on the standard library
alone; deterministic, no randomness, no wall clock.

Method in one line: a conditional launch-curve mirror. Each comparable launch
in the case file contributes its own release-day curve, expressed as log ratios
against its hour-1 print; the forecast is a weighted empirical quantile of
those ratios, anchored on this launch's hour-1 print. Comparables are weighted
by how closely their unlock pattern and their hour-1 scale match the target,
so the mirror reflects launches that actually resemble this one instead of
averaging over every launch ever. Where the matching evidence is thin, the
forecast shrinks toward a plain persistence forecast (flat median, random-walk
spread in log space). If anything at all goes wrong, the program still writes
a valid persistence forecast rather than crashing.
"""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

# The five quantile levels every output row carries, lowest first.
LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
# Standard-normal quantiles at those levels (Abramowitz & Stegun 26.2.23
# values, good to far more digits than the scoring needs).
Z_SCORES = [-1.6448536269514722, -0.6744897501960817, 0.0,
            0.6744897501960817, 1.6448536269514722]
# Persistence spread: log-points of spread per square-root hour.
PERSIST_SIGMA = 0.15
# A comparable must show at least this many release-day hours (and hour 1)
# to count as evidence.
MIN_DAY_HOURS = 15
# Output bounds from the contract: every quantile strictly positive, at most
# one billion viewers.
Q_FLOOR = 1.0
Q_CEIL = 1e9
# Last-resort anchor when neither the case nor the comparables give an hour-1
# print at all.
FALLBACK_ANCHOR = 1000.0

# --- Conditional-mirror tunables (chosen on a leave-one-out backtest over
# the 37 practice comparables; see work/loo_backtest.py). Final LOO with
# these values: mean 0.424 / median 0.251, vs persistence 0.930 / 0.325,
# winning 33 of 37 launches. Rolling-midnight targets only: 0.268 / 0.164.
# ---
# Width of the Gaussian kernel on |ln(h1 comparable) - ln(anchor)|, in log
# units. Comparables near the target's scale count most; a 1-log-unit band
# keeps the closest-scale anchors dominant without collapsing to one launch.
MAG_BANDWIDTH = 1.0
# Blend strength: with effective sample size ESS at an hour, the mirror gets
# weight ESS / (ESS + BLEND_K) and persistence gets the rest. The backtest
# says the mirror's shape is genuinely informative, so the shrinkage is
# light: heavy blending (K=4) cost ~0.08 mean loss. K=0.5 still guarantees
# the band never collapses onto a single comparable's point values.
BLEND_K = 0.5
# Outer quantiles are stretched away from the median by this factor in log
# space: the target is bigger than any comparable, so the empirical spread
# understates the true uncertainty.
WIDEN = 1.1
# Penalty when a comparable's early-access status differs from the target's
# (early-access launches have a flattened release-day curve).
EA_MISMATCH = 0.5
# Penalty for comparables the kit marks as a weak analogue ("Tier: weak").
WEAK_FACTOR = 0.5
# How much a comparable's unlock pattern counts, given the target's pattern.
# Same pattern counts fully; the rolling/global contrast counts least because
# the two shapes differ most (a single global unlock starts near zero and
# explodes, a rolling midnight starts high and dips first).
CLUSTER_WEIGHTS = {
    "rolling": {"rolling": 1.0, "mixed": 0.6, "global": 0.25, "unknown": 0.4},
    "global": {"rolling": 0.25, "mixed": 0.6, "global": 1.0, "unknown": 0.4},
    "mixed": {"rolling": 0.7, "mixed": 1.0, "global": 0.7, "unknown": 0.5},
    "unknown": {"rolling": 0.6, "mixed": 0.6, "global": 0.6, "unknown": 0.6},
}
# GTA VI unlocks at rolling midnight local, so when the case notes say
# nothing classifiable about the target's unlock, rolling is the default.
DEFAULT_TARGET_CLUSTER = "rolling"


def round_half_up(value):
    """Round to two decimals, halves away from zero, returning an int for
    whole numbers so the JSON stays compact."""
    scaled = math.floor(value * 100.0 + 0.5)
    rounded = scaled / 100.0
    return int(rounded) if rounded.is_integer() else rounded


def parse_stamp(text):
    """Parse an ISO-8601 UTC timestamp; returns None on anything unparsable."""
    if not isinstance(text, str):
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def day_curve(comparable):
    """Pull one comparable's release-day curve out of its hourly series.

    Returns a dict {hour: viewers} for hours 1..24 of the comparable's own
    release day, or None when hour 1 is missing or too few hours are present
    (sparse SullyGnome sampling drops small categories' quiet hours).
    """
    start = parse_stamp(comparable.get("day_start"))
    points = comparable.get("hourly")
    if start is None or not isinstance(points, list):
        return None
    curve = {}
    for point in points:
        if not isinstance(point, dict):
            continue
        at = parse_stamp(point.get("from"))
        viewers = point.get("viewers")
        if at is None or not isinstance(viewers, (int, float)):
            continue
        if isinstance(viewers, bool) or not math.isfinite(viewers):
            continue
        offset = (at - start).total_seconds() / 3600.0
        hour = int(offset) + 1
        if 1 <= hour <= 24 and abs(offset - (hour - 1)) < 1e-6:
            if viewers > 0:
                curve[hour] = float(viewers)
    if 1 not in curve or len(curve) < MIN_DAY_HOURS:
        return None
    return curve


def weighted_quantile(pairs, tau):
    """Interpolated tau-quantile of values with positive weights.

    `pairs` is a list of (value, weight); weights need not sum to one. With a
    single pair the answer is that pair's value. Linear interpolation on the
    cumulative-weight axis keeps the quantile smooth as weights shift.
    """
    ordered = sorted(pairs, key=lambda pair: pair[0])
    total = sum(weight for _, weight in ordered)
    if total <= 0:
        return ordered[len(ordered) // 2][0]
    target = tau * total
    running = 0.0
    for index, (value, weight) in enumerate(ordered):
        running += weight
        if running >= target:
            if index == 0 or running - weight >= target:
                return value
            prev_value, _ = ordered[index - 1]
            if weight <= 0:
                return value
            frac = (target - (running - weight)) / weight
            return prev_value + frac * (value - prev_value)
    return ordered[-1][0]


def persistence_row(anchor, hour):
    """The persistence forecast for one hour: flat median at the anchor, with
    a random-walk spread in log space that widens with the square root of the
    hours since hour 1."""
    spread = PERSIST_SIGMA * math.sqrt(hour - 1)
    base = math.log(anchor)
    return [round_half_up(math.exp(base + z * spread)) for z in Z_SCORES]


def anchor_from_case(case):
    """Hour-1 print for the target launch, or None when the case carries no
    usable hour-1 row."""
    known = case.get("known")
    if not isinstance(known, list):
        return None
    for row in known:
        if not isinstance(row, dict) or row.get("hour") != 1:
            continue
        viewers = row.get("viewers")
        if isinstance(viewers, (int, float)) and not isinstance(viewers, bool):
            if math.isfinite(viewers) and viewers > 0:
                # Clamp to the contract's own bounds: a corrupt print must
                # never push exp() past float range downstream.
                return min(Q_CEIL, max(Q_FLOOR, float(viewers)))
    return None


def tidy_row(values):
    """Force one output row into contract shape: clamp to [1, 1e9],
    non-decreasing, two decimals."""
    clean = []
    for value in values:
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            value = FALLBACK_ANCHOR
        clean.append(min(Q_CEIL, max(Q_FLOOR, float(value))))
    clean.sort()
    return [round_half_up(value) for value in clean]


def classify_unlock(text):
    """Classify a free-text unlock description.

    Returns (cluster, early_access, weak_tier). Cluster is one of "rolling"
    (midnight-local launches, staggered by timezone), "global" (one
    simultaneous worldwide unlock), "mixed" (rolling on console plus a global
    PC unlock, or the reverse), or "unknown" (the kit says "unverified").
    """
    lowered = text.lower() if isinstance(text, str) else ""
    # Strip negations before keyword matching: "no simultaneous launch"
    # describes a rolling unlock, but contains the word "simultaneous".
    for phrase in ("no simultaneous", "not simultaneous",
                   "no global unlock", "not a global unlock",
                   "no single global", "not a single global"):
        lowered = lowered.replace(phrase, "")
    rolling = ("midnight local" in lowered or "rolling" in lowered)
    globalish = ("single global" in lowered or "simultaneous" in lowered
                 or "global unlock" in lowered)
    if rolling and globalish:
        cluster = "mixed"
    elif rolling:
        cluster = "rolling"
    elif globalish:
        cluster = "global"
    else:
        cluster = "unknown"
    if "no early access" in lowered:
        early_access = False
    else:
        early_access = ("early access" in lowered or "early-access" in lowered)
    weak_tier = "tier: weak" in lowered
    return cluster, early_access, weak_tier


def target_profile(case):
    """The target launch's unlock profile, read from the case notes.

    Falls back to the rolling-midnight default when the notes carry no
    classifiable unlock text: this program forecasts GTA VI, a rolling
    midnight-local launch.
    """
    notes = case.get("notes") if isinstance(case, dict) else None
    cluster, early_access, _ = classify_unlock(notes)
    if cluster == "unknown":
        cluster = DEFAULT_TARGET_CLUSTER
    return cluster, early_access


def evidence_weight(unlock_text, log_h1, log_anchor, target_cluster, target_ea):
    """How much one comparable counts: unlock-pattern match times a smooth
    magnitude kernel, with penalties for early-access mismatch and weak tier.
    """
    cluster, early_access, weak_tier = classify_unlock(unlock_text)
    weight = CLUSTER_WEIGHTS.get(target_cluster, {}).get(cluster, 0.5)
    gap = (log_h1 - log_anchor) / MAG_BANDWIDTH
    weight *= math.exp(-0.5 * gap * gap)
    if early_access != target_ea:
        weight *= EA_MISMATCH
    if weak_tier:
        weight *= WEAK_FACTOR
    return weight


def forecast_rows(anchor, evidence, target_cluster, target_ea):
    """The conditional mirror: one forecast row per hour, 2..24.

    `evidence` is a list of (curve, unlock_text) pairs, where curve maps
    release-day hours to viewers. Each hour's quantiles are weighted empirical
    quantiles of the comparables' log ratios against their own hour 1,
    anchored on this launch's hour-1 print, then shrunk toward persistence in
    proportion to how thin the evidence is at that hour.
    """
    log_anchor = math.log(anchor)
    weighted = []
    for curve, unlock_text in evidence:
        weight = evidence_weight(unlock_text, math.log(curve[1]), log_anchor,
                                 target_cluster, target_ea)
        if weight > 0:
            weighted.append((curve, weight))
    rows = []
    for hour in range(2, 25):
        pairs = []
        for curve, weight in weighted:
            viewers = curve.get(hour)
            if viewers is not None and viewers > 0:
                pairs.append((math.log(viewers / curve[1]), weight))
        if not pairs:
            rows.append({"hour": hour, "q": tidy_row(persistence_row(anchor, hour))})
            continue
        total = sum(weight for _, weight in pairs)
        sum_sq = sum(weight * weight for _, weight in pairs)
        ess = (total * total / sum_sq) if sum_sq > 0 else 0.0
        mirror_share = ess / (ess + BLEND_K) if ess > 0 else 0.0
        mirror = [weighted_quantile(pairs, tau) for tau in LEVELS]
        median = mirror[2]
        widened = [median + WIDEN * (value - median) for value in mirror]
        spread = PERSIST_SIGMA * math.sqrt(hour - 1)
        quantiles = []
        for index, z in enumerate(Z_SCORES):
            log_q = (mirror_share * widened[index]
                     + (1.0 - mirror_share) * z * spread)
            quantiles.append(anchor * math.exp(log_q))
        rows.append({"hour": hour, "q": tidy_row(quantiles)})
    return rows


def persistence_forecast(anchor):
    """A full 23-row persistence forecast at the given anchor."""
    return {"hours": [{"hour": hour, "q": tidy_row(persistence_row(anchor, hour))}
                      for hour in range(2, 25)]}


def forecast_case(case):
    """Build the full 23-row forecast for one case dict.

    Never raises: each step is guarded separately, so a failure in the mirror
    still leaves a persistence forecast anchored on the best available
    hour-1 print, and a failure anywhere still leaves a valid file.
    """
    anchor = None
    try:
        anchor = anchor_from_case(case)
    except Exception:
        anchor = None
    evidence = []
    try:
        comparables = case.get("comparables") if isinstance(case, dict) else None
        if isinstance(comparables, list):
            for comparable in comparables:
                if not isinstance(comparable, dict):
                    continue
                try:
                    curve = day_curve(comparable)
                except Exception:
                    curve = None
                if curve is not None:
                    evidence.append((curve, comparable.get("unlock")))
    except Exception:
        evidence = []
    if anchor is None:
        try:
            if evidence:
                firsts = sorted(curve[1] for curve, _ in evidence)
                anchor = firsts[len(firsts) // 2]
        except Exception:
            anchor = None
    if anchor is None:
        anchor = FALLBACK_ANCHOR
    try:
        target_cluster, target_ea = target_profile(case)
        return {"hours": forecast_rows(anchor, evidence,
                                       target_cluster, target_ea)}
    except Exception:
        return persistence_forecast(anchor)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
            forecast = forecast_case(case)
        except Exception:
            # Unreadable or unparsable case file: still write a valid file.
            forecast = persistence_forecast(FALLBACK_ANCHOR)
        text = json.dumps(forecast, separators=(",", ":")) + "\n"
        (OUTPUT_DIR / (path.stem + ".out")).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
