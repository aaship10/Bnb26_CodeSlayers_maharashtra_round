"""Run the defence-schema migrations:  python -m app.defence.migrate [upgrade|downgrade-base]"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config


def _sqlalchemy_url(dsn: str) -> str:
    # We use psycopg 3 everywhere; SQLAlchemy needs the dialect spelled out.
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg://" + dsn[len(prefix) :]
    return dsn


def _config(dsn: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.attributes["url"] = _sqlalchemy_url(dsn)
    return cfg


def upgrade(dsn: str) -> None:
    command.upgrade(_config(dsn), "head")


def downgrade_base(dsn: str) -> None:
    command.downgrade(_config(dsn), "base")


if __name__ == "__main__":
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is required")
    action = sys.argv[1] if len(sys.argv) > 1 else "upgrade"
    {"upgrade": upgrade, "downgrade-base": downgrade_base}[action](url)
    print("defence migrations:", action, "ok")
