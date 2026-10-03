# Pre-flight checklist and fallback plan

**For:** the whole team on demo day. Tick every box, and do it twice: the full demo must run end to end **twice without intervention** before we present.

## A. The day before

- [ ] `main` builds clean: `cd frontend && npm ci && npm run build`.
- [ ] Unit/contract tests: `npm test` (all green).
- [ ] End-to-end tests: `npm run e2e` (29 tests, desktop and phone).
- [ ] Full stack up via B's compose. Contract smoke test passes:
      `npm run smoke -- --base http://localhost:8080 --event <id> --dev-user <uuid> --admin-token $ADMIN_TOKEN --writes`
- [ ] C has **pre-run** the four demo runs against the real stack (presets 1–4). Note the run ids in the demo script.
- [ ] Event created and walked to the right phase for sections 5 and 6 (claiming, draw done, fairness page verifies).
- [ ] Backup video recorded (section D) and copied to two places (laptop and a USB stick or cloud drive).
- [ ] Mock stack works offline on the presenting laptop: `npm run dev:all` with Wi-Fi off. Fonts are self-hosted, so nothing should load from the internet.
- [ ] Slides final, with real numbers and intervals filled in.

## B. One hour before

- [ ] Laptop on power. Notifications off (Do Not Disturb). Screen sleep off.
- [ ] Browser: a clean profile or a guest window, zoom 125%, bookmarks bar hidden, extensions off.
- [ ] Projector: mirrored display, check the resolution. Is text readable from the back row?
- [ ] Network: venue Wi-Fi tested. Phone hotspot ready as backup. The stack runs locally, so only the phone view needs the network.
- [ ] Stack up: `docker compose up -d`, then the smoke test passes.
- [ ] Tabs open in demo order (see `DEMO_SCRIPT.md`), admin token entered.
- [ ] B's replica-kill command typed in a terminal, not yet run. The restart command is ready too.
- [ ] Test attendee signed in on the phone (or second window) on the status page, with the badge showing **Live**.
- [ ] The Mock panel is **not visible** on the real stack (it only exists in dev builds). On the mock stack it's at the bottom right: decide whether to hide it.

## C. Ten minutes before

- [ ] Run section 6 (verify the draw) once: it should be green in ~2 s.
- [ ] Reload the compare tab: both runs load, intervals visible.
- [ ] Close every tab not in the script. Clear the browser console.
- [ ] Water. Breathe.

## D. Backup video (record the day before)

- [ ] Record the whole demo script once, cleanly, at 1080p, with the cursor visible. Use the system screen recorder or OBS.
- [ ] Narration optional. If silent, prepare to talk over it.
- [ ] Include: the FCFS vs Fair Drop compare, the request-rate and sybil charts, the replica kill with the status page reconnecting, and the in-browser verification turning green.
- [ ] Watch it back in full on another device. Check the sound and that the text is readable.
- [ ] Have it opened and paused on the first frame before presenting.

## E. Fallback plan

| If… | then… | say… |
|---|---|---|
| The real stack won't start | Run the mock stack: `npm run dev:all` | "Here's the same UI on our deterministic test server; numbers marked synthetic are from our model, and the measured results are on the slides." |
| A simulator run is slow or fails | Use the pre-run ids, then the experiments tab | (nothing; it's the plan) |
| The replica kill misbehaves | Restart the replica; on the mock, Mock panel → **Drop SSE streams** | "Simulating the same failure: watch the page reconnect and resume." |
| The browser or projector fails | Play the backup video | "Let me show you the recorded run while we fix that." |
| A number looks wrong live | Don't improvise; go to the slide with measured results | "The measured runs with intervals are here." |
