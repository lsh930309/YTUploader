from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .paths import get_catalog_db_path


SCHEMA = """
CREATE TABLE IF NOT EXISTS game_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    title_prefix TEXT NOT NULL DEFAULT '',
    description_template TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_name TEXT NOT NULL,
    name TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(game_name, name)
);
"""


class CatalogStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or get_catalog_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def upsert_game_profile(
        self,
        *,
        name: str,
        title_prefix: str = "",
        description_template: str = "",
        tags_json: str = "[]",
    ) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO game_profiles(name, title_prefix, description_template, tags_json)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    title_prefix = excluded.title_prefix,
                    description_template = excluded.description_template,
                    tags_json = excluded.tags_json
                """,
                (name, title_prefix, description_template, tags_json),
            )
            connection.commit()

    def list_game_profiles(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT name, title_prefix, description_template, tags_json FROM game_profiles ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_preset(self, *, game_name: str, name: str, details_json: str = "{}") -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO presets(game_name, name, details_json)
                VALUES(?, ?, ?)
                ON CONFLICT(game_name, name) DO UPDATE SET details_json = excluded.details_json
                """,
                (game_name, name, details_json),
            )
            connection.commit()

    def list_presets(self, game_name: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT game_name, name, details_json FROM presets WHERE game_name = ? ORDER BY name",
                (game_name,),
            ).fetchall()
        return [dict(row) for row in rows]
