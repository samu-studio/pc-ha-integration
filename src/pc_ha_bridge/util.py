import re
import unicodedata


def slugify(text: str) -> str:
    """Lowercase ASCII identifier usable in MQTT topics and unique_ids."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")
    return slug or "unnamed"


def unique_slug(base: str, taken: set[str]) -> str:
    """Return `base`, or `base_2`, `base_3`, ... — whichever is not in `taken`."""
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"
