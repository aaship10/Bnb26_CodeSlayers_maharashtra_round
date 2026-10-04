"""Rate limiting. bucket.py (Lua limiter) and errors.py (429 contract) exist since stage 2
because registration needs them; stage 3 adds the per-endpoint gate for /enter, /claim,
/status and the nginx edge layer."""
