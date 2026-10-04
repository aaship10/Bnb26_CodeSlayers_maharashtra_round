"""Redis stand-in for machines without Redis: fakeredis served over TCP (Lua supported).

Honest limits: single process, one Python thread pool, in-memory only, not a performance
or durability model. Good for functional local runs and tests; never quote throughput
numbers from it. With a real Redis (REDIS_URL) this file is not used.

    python infra/local/fake_redis.py 6380
"""
import sys

from fakeredis import TcpFakeServer

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 6380
    server = TcpFakeServer(("127.0.0.1", port), server_type="redis")
    print(f"fakeredis listening on 127.0.0.1:{port}", flush=True)
    server.serve_forever()
