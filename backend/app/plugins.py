"""Plugin loading: how teammates extend the API without editing core code.

Set PLUGINS=defence.plugin (comma-separated). Each module must expose
`setup(app: FastAPI) -> None`, which may, for example:

    from app.auth import get_current_user
    from app.hooks import register_entry_gate

    def setup(app):
        app.add_middleware(ClientIpMiddleware)                       # sets request.state.client_ip
        app.dependency_overrides[get_current_user] = jwt_current_user  # AUTH_MODE=jwt
        register_entry_gate(defence_gate)

Plugins must not keep correctness-critical state in process memory.
"""
from __future__ import annotations

import importlib

from fastapi import FastAPI


def load_plugins(app: FastAPI, spec: str) -> list[str]:
    loaded = []
    for name in (p.strip() for p in spec.split(",")):
        if not name:
            continue
        module = importlib.import_module(name)
        module.setup(app)
        loaded.append(name)
    return loaded
