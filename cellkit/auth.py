"""Verify platform-issued bearer tokens (RS256) for a cell's ``<cell>-api`` audience.

The same token is forwarded to the platform for every decision, so the
platform can refuse it too; local verification only rejects bad tokens early
and binds the request to one workspace for row-level security.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt

MAX_LIFETIME_SECONDS = 3600  # owner tokens (no delegation) are always short-lived
HARD_MAX_LIFETIME_SECONDS = 30 * 86400
CHANNELS = {"api", "mcp", "workflow", "cli", "event"}


class Unauthenticated(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    actor: uuid.UUID
    workspace: uuid.UUID
    delegation: uuid.UUID | None
    channel: str
    token: str  # forwarded to the platform; never logged

    def __repr__(self) -> str:  # keep the token out of logs and tracebacks
        return f"Principal(actor={self.actor}, workspace={self.workspace}, channel={self.channel})"


@dataclass(frozen=True)
class TokenVerifier:
    public_key_pem: str
    issuer: str
    audience: str
    max_lifetime: int = HARD_MAX_LIFETIME_SECONDS

    def verify(self, authorization: str | None, channel: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthenticated("missing bearer token")
        token = authorization.removeprefix("Bearer ")
        try:
            claims = jwt.decode(token, self.public_key_pem, algorithms=["RS256"], audience=self.audience,
                                issuer=self.issuer, options={"require": ["exp", "iat", "sub", "jti"]})
            lifetime = claims["exp"] - claims["iat"]
            limit = self.max_lifetime if claims.get("del") else min(self.max_lifetime, MAX_LIFETIME_SECONDS)
            if lifetime > limit:
                raise Unauthenticated("token lifetime too long")
            actor = uuid.UUID(claims["sub"])
            workspace = uuid.UUID(claims["ws"])
            delegation = uuid.UUID(claims["del"]) if claims.get("del") else None
        except Unauthenticated:
            raise
        except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
            raise Unauthenticated("invalid token") from exc
        channel = channel or "api"
        if channel not in CHANNELS:
            raise Unauthenticated("unknown channel")
        return Principal(actor=actor, workspace=workspace, delegation=delegation, channel=channel, token=token)
