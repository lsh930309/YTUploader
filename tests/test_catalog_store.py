from __future__ import annotations

from pathlib import Path

from core.catalog_store import CatalogStore


def test_catalog_store_upserts_and_lists_profiles(tmp_path: Path) -> None:
    store = CatalogStore(db_path=tmp_path / "catalog.db")
    store.upsert_game_profile(name="Zenless Zone Zero", title_prefix="[젠존제]", description_template="desc")

    profiles = store.list_game_profiles()

    assert profiles == [
        {
            "name": "Zenless Zone Zero",
            "title_prefix": "[젠존제]",
            "description_template": "desc",
            "tags_json": "[]",
        }
    ]


def test_catalog_store_upserts_and_lists_presets(tmp_path: Path) -> None:
    store = CatalogStore(db_path=tmp_path / "catalog.db")
    store.upsert_preset(game_name="Zenless Zone Zero", name="Boss", details_json='{"kind":"boss"}')

    presets = store.list_presets("Zenless Zone Zero")

    assert presets == [
        {
            "game_name": "Zenless Zone Zero",
            "name": "Boss",
            "details_json": '{"kind":"boss"}',
        }
    ]
