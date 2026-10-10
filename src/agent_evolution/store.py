from __future__ import annotations

import json
import sqlite3
import time
import uuid
import re
from pathlib import Path

from .util import canonical, EvolutionError


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "state.sqlite", timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, run TEXT, kind TEXT, at REAL, data TEXT);
        CREATE TABLE IF NOT EXISTS usage(run TEXT, kind TEXT, count INTEGER, PRIMARY KEY(run,kind));
        CREATE TABLE IF NOT EXISTS evals(run TEXT, key TEXT, data TEXT, PRIMARY KEY(run,key));
        CREATE TABLE IF NOT EXISTS holdouts(key TEXT PRIMARY KEY, run TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS promotions(id TEXT PRIMARY KEY, data TEXT NOT NULL);
        """)

    def create(self, data: dict) -> str:
        ident = uuid.uuid4().hex
        data.update(id=ident, created=time.time(), status="draft")
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?,?)", (ident, canonical(data)))
        return ident

    def get(self, ident: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise EvolutionError("Invalid run ID")
        row = self.db.execute("SELECT data FROM runs WHERE id=?", (ident,)).fetchone()
        if not row:
            raise EvolutionError("Unknown run ID")
        return json.loads(row[0])

    def put(self, data):
        with self.db:
            self.db.execute("UPDATE runs SET data=? WHERE id=?", (canonical(data), data["id"]))

    def event(self, run, kind, data):
        with self.db:
            self.db.execute("INSERT INTO events(run,kind,at,data) VALUES(?,?,?,?)", (run, kind, time.time(), canonical(data)))

    def reserve(self, run: str, kind: str, limit: int, deadline: float):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if time.time() >= deadline:
                raise EvolutionError("Wall time budget exhausted")
            row = self.db.execute("SELECT count FROM usage WHERE run=? AND kind=?", (run, kind)).fetchone()
            used = row[0] if row else 0
            if used >= limit:
                raise EvolutionError(f"{kind} budget exhausted")
            self.db.execute("INSERT INTO usage VALUES(?,?,?) ON CONFLICT(run,kind) DO UPDATE SET count=excluded.count", (run, kind, used + 1))

    def used(self, run):
        return dict(self.db.execute("SELECT kind,count FROM usage WHERE run=?", (run,)))

    def cached(self, run, key):
        row = self.db.execute("SELECT data FROM evals WHERE run=? AND key=?", (run, key)).fetchone()
        return json.loads(row[0]) if row else None

    def save_eval(self, run, key, result):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO evals VALUES(?,?,?)", (run, key, canonical(result)))

    def evaluations(self, run):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM evals WHERE run=? ORDER BY key", (run,))]

    def claim_holdout(self, key, run):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT run FROM holdouts WHERE key=?", (key,)).fetchone()
            if row and row[0] != run:
                raise EvolutionError("Holdout already exposed in another run; supply a fresh suite")
            self.db.execute("INSERT OR IGNORE INTO holdouts VALUES(?,?)", (key, run))
