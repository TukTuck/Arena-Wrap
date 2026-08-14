"""Secret-safe provider credential access.

Only presence is exposed to the registry and diagnostics. Secret values are
never returned by public methods and are intentionally not persisted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class CredentialRef:
    env_var: str | None
    configured: bool


class CredentialStore:
    """Resolve provider credentials from the process environment only."""

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = os.environ if environ is None else environ

    def reference(self, env_var: str | None) -> CredentialRef:
        if not env_var:
            return CredentialRef(env_var=None, configured=True)
        return CredentialRef(
            env_var=env_var,
            configured=bool(str(self._environ.get(env_var, "")).strip()),
        )

    def get_secret(self, env_var: str | None) -> str | None:
        """Internal transport hook; callers must not log or persist the result."""
        if not env_var:
            return None
        value = str(self._environ.get(env_var, "")).strip()
        return value or None

    def public_status(self, env_var: str | None) -> dict[str, str | bool | None]:
        ref = self.reference(env_var)
        return {"env_var": ref.env_var, "configured": ref.configured}
