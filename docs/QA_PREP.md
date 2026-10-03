# Q&A prep

**For:** all four presenters. Short, honest answers first, then a detail to add if pressed. Never claim a measurement we didn't make; say "we modelled" vs "we measured" precisely. Numbers in [brackets] come from C's measured runs.

## The four we must nail

**"What about someone with 1,000 real accounts?"**
They get roughly 1,000 entries out of [N] total, so about that share of seats, and nothing more. That's the honest limit of any one-entry-per-identity system. What we add is cost: each identity has to pass verification, defences (proof-of-work, CAPTCHA for risky clients, device and timing signals) reject a share of fake ones, and we measured the attacker's cost per seat won: [requests / accounts / PoW hashes]. The sybil chart shows degradation is gradual and that defences bend the curve. The real limiter is identity verification (phone, ID, payment instrument), and that's a product decision, not something to hide.

**"Isn't a lottery unfair to the people who wanted it most?"**
It's equal opportunity by design: everyone who shows up during the window has the same chance, whatever their bandwidth, device or reflexes. "Who wanted it most" under FCFS really means "who had the fastest connection and the best bot". If an organizer wants to favour loyal fans, the draw already supports weights (1, 0.5, 0.25), for example loss compensation: people who lost last time get a higher weight next time. Weights are public in the entrant list, so even that stays verifiable.

**"Did you really test 50,000 users?"**
Yes, 50,000 *logical* users, simulated as asynchronous clients with realistic arrival times, retries and think times. Not 50,000 simultaneous sockets from one machine; a single laptop can't open that, and claiming so would be dishonest. What matters for the server is request rate and concurrency, which the simulator reproduces: [peak rps], [concurrent connections]. Every result has a 95% confidence interval from [n] repeats.

**"Why not just rate limit?"**
Rate limiting still rewards speed. Within the limit, the fastest client wins, and a bot farm spreads its requests across many IPs and accounts to stay under it. It also punishes real people on shared networks (campus Wi-Fi, mobile carriers). We do rate limit, but as one defence layer. The lottery removes the reward for speed entirely, so the incentive to hammer the server goes away.

## Likely follow-ups

**"Can you, the organizers, rig the draw?"**
Not without being caught. Before the window opens we publish a commitment (SHA-256 of a secret seed). After it closes we lock the entrant list (its hash is published) and mix in a public randomness beacon round fixed in advance, which nobody knows ahead of time. Anyone can re-run the whole draw in their browser and compare with the published winners; changing the seed, the list or the results fails a specific step. The audit log is a hash chain checked in the browser too. *(Detail: until A's final spec, the byte encodings follow our provisional spec in `docs/DRAW_SPEC_PROVISIONAL.md`, cross-checked by three independent implementations.)*

**"What if the beacon is unavailable?"**
[A's answer: fallback beacon source / delay rule.] The rule has to be fixed before the window opens, otherwise it would be a back door.

**"Doesn't proof-of-work hurt people on cheap phones?"**
The difficulty is a setting (B's defence config). We measured about 1.2 million attempts per second in a laptop browser, so the default 18 bits takes a fraction of a second there; [phone result from `/__dev/pow-bench` on a mid-range phone], and we size it to stay around a second or less. The page explains what's happening ("Verifying you're human…") with a progress bar. It runs in a Web Worker so the page stays responsive, and it works on plain-http LAN demos because it doesn't depend on WebCrypto. For a bot farm, the same cost multiplied by thousands of requests is what hurts.

**"What happens at the moment the window opens? Isn't that a spike?"**
Much less than FCFS, because nobody gains by being first and the UI says so. The client also never refreshes exactly at deadlines: each browser waits a random 0.3–4 s, live updates come over SSE (no polling while it works), and the fallback polls every 5–10 s with jitter and backs off on errors. No page polls once a second.

**"What if my connection drops while claiming?"**
The claim carries an Idempotency-Key created once and kept for that tab session. Retries and refreshes reuse it, so the server can grant at most one seat however many times the request arrives. We test this by killing the network mid-claim and refreshing.

**"How do you know you never oversell?"**
Invariants are checked continuously: oversold, duplicate users, duplicate seats, orphaned holds. They're shown on the organizer dashboard and in every simulator result. They stayed at zero [through all runs, including the replica kill].

**"Is the UI accessible?"**
Keyboard and screen-reader navigable. State changes are announced through a live region, and countdown digits are hidden from screen readers in favour of a minute-level sentence. Reduced motion is respected. axe finds zero WCAG A/AA issues on every main screen, and Lighthouse accessibility is 100 on mobile.

**"What would you do next?"**
Stronger identity verification options, weighted and loss-compensation draws in the organizer UI, a published draw spec with test vectors for third-party verifiers, and load tests from multiple regions.

## If you don't know

Say so: "We haven't measured that; here's how we'd test it." Then name the simulator scenario you'd run.
