"""Shared constants for trainee-sim."""

from __future__ import annotations

# A deliberately uncommon port, to avoid colliding with the many things that
# squat on 8000 (other dev servers, proxies, etc.) on the target machine.
DEFAULT_PORT = 22133
DEFAULT_SERVER_URL = f"http://localhost:{DEFAULT_PORT}"
