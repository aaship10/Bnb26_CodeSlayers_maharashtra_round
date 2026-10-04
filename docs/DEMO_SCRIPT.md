# Demo script: Fair Drop

**For:** whoever drives the demo, plus one co-presenter who narrates.
**Length:** about 6 min of demo inside a ~10 min slot (2 min slides before, 2 min results and close after). Trim section 4 first if time is short.
**Screens:** laptop on the projector, browser zoom 125%. Optionally, a phone mirrored for the attendee view.

> **The honesty rule.** Every number on screen is either measured (C's simulator against the real stack) or synthetic (mock), and the UI badges synthetic data in red. If a red "MOCK / SYNTHETIC DATA" stamp is visible, **say so out loud**: "these are from our model, the measured runs are on the next slide." Never present a mock number as a measurement.

## Before you start (T-10 min)

- Run the pre-flight checklist (`docs/PREFLIGHT_CHECKLIST.md`).
- Open these tabs, in this order:
  1. `/admin/sim`
  2. `/admin/sim/compare?fcfs=<FCFS run>&lottery=<Fair Drop run>` (pre-run pair)
  3. `/admin/sim/experiments`
  4. `/events/<event>/status` signed in as a test attendee (phone or second window)
  5. `/admin/events/<event>`
  6. `/events/<event>/fairness`
- Admin token entered (tabs 1, 2, 3 and 5 are unlocked).
- **Pre-run** these simulator runs and note their ids, because live runs at 50,000 users take minutes:
  - preset 1 (FCFS under attack)
  - preset 2 (Fair Drop, same attack)
  - preset 3 (100× faster bots)
  - preset 4 (10,000 sybils)

## 1. Speed wins under FCFS (0:00–1:00)

**Click:** tab 2. Point at the first row of the comparison.
**Say:** "Same 50,000 people, same 200 bots, 100 times faster than a person. Under first-come-first-served, bots take **[FCFS bot share]** of the seats." Point at the orange dot and its interval near 100%.
**Show:** the "People whose entry got through" row: under FCFS most real people never get in at all.

## 2. Identical attack against Fair Drop (1:00–2:15)

**Same tab.** Point at the blue dot near 0%.
**Say:** "Same attack, lottery mode: bots get **[Fair Drop bot share]**. That's roughly their share of identities, and nothing more. Being fast buys nothing, because entering in the first second or the last gives the same chance."
**Point at:** the "Arrival time vs winning" row (≈0 for Fair Drop), entry latency p95, and the two **Integrity held** banners: "oversold 0, duplicates 0, orphaned holds 0."
**If asked about CIs:** every number shows its 95% interval and n; the dots are means, the bars are the intervals.

## 3. 100× more requests changes nothing (2:15–3:00)

**Click:** tab 3, experiment "Speed doesn't buy seats". Hover the right end of the chart.
**Say:** "Here the bots' request rate goes from 1× to 1,000×. FCFS climbs to almost every seat. Fair Drop stays flat, because extra requests don't create extra entries." Hover to show the tooltip with intervals.
**Optional:** open the preset-3 run from tab 1 to show it's a real run, not just a chart.

## 4. Sybils: where it degrades, and how defences bend it (3:00–4:00)

**Click:** experiment "Identities are the real limiter".
**Say:** "The honest limit: an attacker with thousands of verified-looking accounts gets about that share, so identity is what matters. Defences make each identity cost more: proof-of-work, CAPTCHA for risky clients, signals and risk scoring." Point at the gap between the "No defences" and "All layers" curves.
**Then:** "Defence ablation" tab: which layer moves the number most.

## 5. Kill a replica mid-run (4:00–5:00)

**Needs B.** Ask B to run (pre-typed in a terminal): `docker compose kill <api-replica>`
**Click:** tab 4 (attendee status, live) and tab 5 (organizer).
**Say:** "We just killed one API replica during the window." Point at:
- the status page badge going **Reconnecting…** then **Live** again, with the same state shown throughout and nothing lost;
- the organizer page: stats keep moving, and the invariants badge stays **green**.

**If the reconnect is slow:** "The page retries with jittered backoff, so 50,000 phones don't all come back in the same second." That's a feature, not a stall.

## 6. Verify the draw yourself (5:00–6:00)

**Click:** tab 6 → **Verify the draw**.
**Say, while it runs (~2 s):** "This runs entirely in this browser. It downloads all 50,000 entries, checks the seed against the fingerprint we published before the window opened, mixes in the public beacon, re-ranks every entry and compares with the published winners."
**Show:** five green steps and **Verified: this draw is exactly reproducible**. Then **Try the first winner's ID** → "Picked: winner #1 of 500".
**Close:** "No one has to trust us, the server included."

*(Mock only, if time allows: switch the Mock panel's "Fairness data" to `tampered: entrants` and verify again. Step 2 fails: "The list has changed since the window closed.")*

## Results and close (6:00–8:00)

Back to the slides: measured numbers with intervals, what's honest about the limits (sybils), and the one-line pitch.

## If something breaks

| Problem | Do this |
|---|---|
| Real stack down | Switch to the mock stack (`npm run dev:all`). Every screen works and is badged synthetic. Say so. |
| Simulator slow or failed | Use the pre-run pair and the experiments tab. Never start a 50k run live. |
| Replica kill not possible | Mock: tab 4, Mock panel → **Drop SSE streams**. Shows the same reconnect behaviour. Say it's simulated. |
| Projector or browser failure | Play the backup video (pre-flight checklist, section D). |
