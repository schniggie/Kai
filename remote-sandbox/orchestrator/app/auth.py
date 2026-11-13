from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Token:
    token_id: str
    secret: str
    label: str = ""


class TokenStore:
    """Loads `token_id secret [label…]` lines from a tokens file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._by_secret: dict[str, Token] = {}
        self.reload()

    def reload(self) -> None:
        by_secret: dict[str, Token] = {}
        if self.path.exists():
            for raw in self.path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                token_id, secret, *rest = parts
                by_secret[secret] = Token(token_id=token_id, secret=secret, label=" ".join(rest))
        self._by_secret = by_secret

    def authenticate(self, authorization: str | None) -> Token:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise PermissionError("missing bearer token")
        secret = authorization.split(" ", 1)[1].strip()
        token = self._by_secret.get(secret)
        if token is None:
            # Hot-reload so adding a token does not require a restart.
            self.reload()
            token = self._by_secret.get(secret)
        if token is None:
            raise PermissionError("invalid token")
        return token
