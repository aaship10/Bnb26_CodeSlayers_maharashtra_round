import argparse

import uvicorn

from .app import create_app


def main() -> None:
    p = argparse.ArgumentParser(description="Fair Drop mock target (dev double)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8200)
    a = p.parse_args()
    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="warning", access_log=False,
                timeout_keep_alive=75)


if __name__ == "__main__":
    main()
