# GTA VI launch-day Twitch forecast

[![make.fun bounty](https://make.fun/badge/gta6-twitch-ccu-forecast-01.svg)](https://make.fun/bounty/gta6-twitch-ccu-forecast-01?utm_source=github&utm_medium=readme&utm_campaign=gta6-twitch-ccu-forecast-01)
[![make.fun rank](https://make.fun/badge/gta6-twitch-ccu-forecast-01/fadliferdian28/gta6-twitch-ccu-forecast-01/rank.svg)](https://make.fun/bounty/gta6-twitch-ccu-forecast-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=gta6-twitch-ccu-forecast-01)
[![make.fun reward](https://make.fun/badge/gta6-twitch-ccu-forecast-01/fadliferdian28/gta6-twitch-ccu-forecast-01/reward.svg)](https://make.fun/bounty/gta6-twitch-ccu-forecast-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=gta6-twitch-ccu-forecast-01)

An entry for the [gta6-twitch-ccu-forecast-01](https://make.fun/bounty/gta6-twitch-ccu-forecast-01?utm_source=github&utm_medium=readme&utm_campaign=gta6-twitch-ccu-forecast-01)
bounty: forecast the concurrent viewers in the Grand Theft Auto VI Twitch
category for hours 2–24 of release day, 2026-11-19 UTC, as five quantiles
(5%, 25%, 50%, 75%, 95%) per hour.

## How it works

A conditional launch-curve mirror, computed at runtime from the comparables
shipped inside each case file — no baked-in curve, no network, no model calls.

1. **Read the evidence.** Every comparable launch contributes its own
   release-day curve, expressed as log ratios against its hour-1 print.
2. **Match the unlock pattern.** Launches are grouped by how they unlocked —
   rolling midnight local, one simultaneous global unlock, a mix of the two,
   or unknown — from the free-text unlock notes. GTA VI unlocks at rolling
   midnight local, so rolling-midnight launches count most; a single global
   unlock starts near zero and explodes, a shape that says little about a
   rolling launch.
3. **Match the scale.** A smooth kernel on log hour-1 viewers weights the
   closest-scale launches heaviest — the biggest launches (ELDEN RING,
   Cyberpunk 2077, Diablo IV) are the nearest anchors for a launch of GTA VI's
   size. Early-access-contaminated and weak-tier comparables are downweighted.
4. **Mirror, then shrink.** Each hour's quantiles are weighted empirical
   quantiles of the matched log ratios, anchored on the target's actual
   hour-1 print, blended lightly toward a persistence forecast where the
   matching evidence is thin, with the outer quantiles widened slightly —
   GTA VI is bigger than any comparable, so the empirical spread understates
   the true uncertainty.
5. **Never fail the case.** Any bad input — a missing hour-1 print, an empty
   comparables list, degenerate viewer counts — falls back to a valid
   persistence forecast. The program always writes a contract-valid file.

Backtest (leave-one-out over the 37 usable comparables in the practice
cases, scored with the contest's exact log-scale pinball loss): mean **0.424**
/ median **0.251**, against the persistence baseline's 0.930 / 0.325, winning
33 of 37 launches. On rolling-midnight targets only — the final's regime —
mean 0.268 / median 0.164, against persistence 0.357 / 0.255.

## Run it

Python 3.13, standard library only. The grader runs `python3 -P -s
/bot/main.py` with the case in `/input` and the forecast written to `/output`.
Outside the grader, point the two environment variables at local directories:

```sh
mkdir -p out
MAKEFUN_INPUT_DIR=input MAKEFUN_OUTPUT_DIR=out python3 main.py
```

Each `<id>.in` in the input directory produces an `<id>.out` with exactly 23
rows (hours 2–24, five non-decreasing quantiles each). Runs in well under
half a second per case and is fully deterministic: two runs on the same case
produce byte-identical output.

## Layout

- `main.py` — the whole forecaster, one self-contained file.
- `LICENSE` — MIT.
- `.makefun/owner.json` — the entry owner's wallet.

## Data

Viewer counts in the case files come from SullyGnome's public Twitch category
statistics, as shipped with the bounty kit.
