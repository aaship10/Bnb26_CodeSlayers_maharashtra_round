"""Fair Drop MOCK target. Development double only: results against it are target="mock"."""

from .app import Clock, ManualClock, Settings, create_app

__all__ = ["Clock", "ManualClock", "Settings", "create_app"]
