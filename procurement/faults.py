"""Process-kill hook for the crash-recovery test; inert unless the variable is set."""

import os


def crash_point(name: str) -> None:
    """Terminate immediately, without cleanup, when PROCUREMENT_CRASH_AT names this point."""
    if os.environ.get("PROCUREMENT_CRASH_AT") == name:
        os._exit(70)
