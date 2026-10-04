# Fixtures

`a_stage2_openapi.json`: the OpenAPI document served by Member A's real backend at `origin/main`
commit `10277c8` (A's stage 2: create/schedule/open/close/config/enter/status, **no** draw, claim,
reset, stats, invariants, stream, readyz). Captured from a running instance, not hand-written.
`tests/test_capabilities.py` uses it to prove capability detection against the real thing. When A
ships more routes, regenerate it (`curl http://127.0.0.1:8000/openapi.json`) and update the
expectations in that test; a failing test there means A's API moved.
