# Slide outline: Fair Drop (8 slides plus backups)

**For:** whoever builds the deck. Each slide lists its one message, what goes on it, and the speaker note. Keep text on slides minimal; numbers come from C's measured runs and are shown **with their 95% intervals and n**. Placeholders are in [brackets].

**Look:** match the app (warm paper background, ink outlines, tomato accent, ticket and ball motifs). The fonts are Bricolage Grotesque for headings and DM Sans for text. Screenshots come straight from the app at 125% zoom.

---

**1. Title**
*Message:* 500 seats, 50,000 people, and bots don't win.
*On slide:* "Fair Drop", team name, one ticket illustration.
*Note:* Open with the pain: everyone has lost a sale to bots in the first second.

**2. The problem**
*Message:* First-come-first-served turns a sale into a speed contest, and bots are faster.
*On slide:* A single chart: bot share vs request rate under FCFS (climbs to ~100%).
*Note:* Rate limiting slows bots down but still rewards whoever is fastest within the limit.

**3. Our idea in one line**
*Message:* Remove speed from the game. Everyone gets one entry in a window; a provably fair draw picks the seats.
*On slide:* Three steps with the numbered balls: commit → mix in public randomness → anyone can replay.
*Note:* "No need to rush" is the user promise; it's also why the server isn't stampeded.

**4. Architecture (one picture)**
*Message:* Simple, stateful where it matters, resilient everywhere else.
*On slide:* Browser → nginx → API replicas → database; simulator beside it; a public verifier in the browser.
*Note:* One entry per identity is enforced in the database; claims are idempotent; status is pushed over SSE with polite polling fallback (never per-second polling); time comes from the server.

**5. Evidence: same attack, two allocations** (the money slide)
*Message:* Under the identical attack, bots take [FCFS share] of seats with FCFS and [Fair Drop share] with Fair Drop.
*On slide:* The compare-page screenshot (dot = mean, bar = 95% CI, n=[n]) plus the integrity line "oversold 0 · duplicates 0 · orphaned holds 0".
*Note:* Point at intervals, not just means.

**6. Evidence: what attackers can and can't buy**
*Message:* Speed buys nothing; identities buy a proportional share; defences raise the price.
*On slide:* Two small charts: bot share vs request rate (flat for Fair Drop), and bot share vs identities with/without defences. Cost per seat won: [requests], [accounts], [PoW hashes].
*Note:* Be upfront: sybils are the real limit, and identity verification is the lever.

**7. Trust: verify it yourself**
*Message:* The draw is reproducible by anyone in a browser.
*On slide:* Screenshot of five green steps and "Verified". The commitment was published before the window, plus beacon round [N].
*Note:* 50,000 entries re-ranked in ~2 s on a laptop, in a Web Worker. Tampering with the list, results or seed is caught at the exact step.

**8. Close**
*Message:* Fair, fast enough, and checkable.
*On slide:* 3 bullets, each with its proof (CI chart, invariants held through a replica kill, in-browser verification) and the team.

---

**Backup slides** (for Q&A, not presented)
- **B1:** 50,000 users: how the simulator generates them (async logical clients) and their arrival curve.
- **B2:** Defence ablation chart, and detection precision/recall by layer.
- **B3:** Latency percentiles: normal vs attack, Fair Drop vs FCFS.
- **B4:** Accessibility and performance:
  - Lighthouse mobile: Performance 90–96, Accessibility 100, Best Practices 100.
  - axe: zero findings on every main screen.
  - First-visit JS: ~115 kB gzipped.
- **B5:** Failure handling: SSE resume with Last-Event-ID, jittered backoff, idempotent claims across refreshes.
