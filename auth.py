from __future__ import annotations

import re
from dataclasses import dataclass
from time import time
from typing import Dict, List, Optional

from flask import Flask, abort, current_app, request
from requests import HTTPError

import api

CACHE_SIZE = 100
CACHE_SECS = 10 * 60
RATE_LIMIT_TIMEOUT_SECS = 10 * 60


@dataclass
class User:
    is_admin: bool
    teams: List[str]

    def for_team(self, team: str) -> None:
        if not self.is_admin and team not in self.teams:
            abort(403)

    @staticmethod
    def plain(teams: List[str]) -> User:
        return User(False, teams)

    @staticmethod
    def admin() -> User:
        return User(True, [])


@dataclass
class CacheEntry:
    user: User
    time: float


class Auth:
    def __init__(self, app: Flask) -> None:
        self.cache: Dict[str, CacheEntry] = {}
        self.admins = set(app.config["ADMINS"])
        self.teams = set(app.config["TEAMS_WHITELIST"])
        self.rate_limited_until = 0

    def get_from_cache(self, token: str) -> Optional[User]:
        cached = self.cache.get(token)
        if cached:
            if cached.time > time() - CACHE_SECS:
                return cached.user
            del self.cache[token]
        return None

    def add_cache(self, token: str, user: User) -> None:
        if len(self.cache) >= CACHE_SIZE:
            self.cache = {
                k: v
                for k, v in self.cache.items()
                if v.time > time() - CACHE_SIZE and (v.user.is_admin or v.user.teams)
            }
        if len(self.cache) >= CACHE_SIZE:
            self.cache = {}
        self.cache[token] = CacheEntry(user, time())

    def __call__(self) -> User:
        token = request.headers.get("Authorization")
        if not token:
            abort(401)
        if not token.startswith("Bearer "):
            abort(400, description="Invalid Authorization header")
        token = token[len("Bearer ") :].strip()
        if not re.fullmatch("[a-zA-Z0-9_]+", token):
            abort(400, description="Invalid Authorization header")

        cached = self.get_from_cache(token)
        if cached:
            return cached

        if self.rate_limited_until > time():
            abort(503)

        try:
            res = api.verify_token(token)
            if (
                not res
                or "tournament:write" not in res.scopes
                or res.expires < time()
            ):
                abort(403)

            if res.userId in self.admins:
                user = User.admin()
            else:
                teams = [
                    team for team in api.leader_teams(res.userId) if team in self.teams
                ]
                user = User.plain(teams)
            self.add_cache(token, user)
            return user
        except HTTPError as e:
            current_app.logger.error(f"Error during auth requests to Lichess: {e}")
            if e.response.status_code == 429:
                self.rate_limited_until = int(time()) + RATE_LIMIT_TIMEOUT_SECS
                abort(503)
            abort(500)
