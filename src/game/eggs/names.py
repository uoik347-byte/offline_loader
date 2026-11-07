"""Egg labels. Instance ei is not a species id and is never used as a catalog key."""
from __future__ import annotations

# 1-8 and 1=1 items in this repository are ei, pmid and st. They do not carry
# a species id or a name, so the live panel cannot join a name table.
CATALOG_VERSION = "none"
NAME_NOTE = "没有可用的种类编号，只显示 eggId。不能把 eggId 当成蛋名。"


def egg_display_name(
    egg_id: int,
    species_id: int | None = None,
    catalog: dict[int, str] | None = None,
) -> str:
    """Join a name only through an explicit species id.

    Callers must not pass the warehouse instance ei as ``species_id``.
    Looking up ``catalog[egg_id]`` is intentionally not done here.
    """
    name = ""
    if species_id is not None and catalog is not None:
        value = catalog.get(species_id)
        if isinstance(value, str) and value.strip():
            name = value.strip()
    if name:
        return f"{name}（eggId={egg_id}）"
    return f"eggId={egg_id}"
