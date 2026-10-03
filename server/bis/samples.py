"""Sample icon library: the user's corpus (``samle icons/``, sic) + the SVG pipeline test files."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from urllib.parse import quote

from .config import Settings
from .pipeline import SvgPipeline
from .schemas import SampleIcon
from .util import atomic_write_bytes

log = logging.getLogger("bis.samples")


class SampleNotFound(KeyError):
    def __str__(self) -> str:
        return f"Sample not found: {self.args[0] if self.args else ''}"


class SampleLibrary:
    def __init__(self, settings: Settings, svg: SvgPipeline) -> None:
        self.settings = settings
        self.svg = svg
        self.thumb_dir = settings.cache_dir / "samples"

    def _scan(self) -> dict[str, tuple[Path, str]]:
        """name -> (path, collection). Corpus names win; later collisions get a collection prefix."""
        out: dict[str, tuple[Path, str]] = {}
        for i, d in enumerate(self.settings.sample_dirs):
            if not d.is_dir():
                continue
            collection = "corpus" if i == 0 else d.name
            for f in sorted(d.glob("*.svg"), key=lambda p: p.name.lower()):
                name = f.stem
                if name in out:
                    name = f"{collection}-{f.stem}"
                out[name] = (f, collection)
        return out

    def list(self) -> list[SampleIcon]:
        items = []
        for name, (path, collection) in self._scan().items():
            q = quote(name, safe="")
            items.append(
                SampleIcon(
                    name=name,
                    file=path.name,
                    url=f"/api/samples/{q}/source.svg",
                    thumbnail=f"/api/samples/{q}/thumbnail.png",
                    collection=collection,
                )
            )
        return items

    def resolve(self, name: str) -> Path:
        scan = self._scan()
        hit = scan.get(name) or scan.get(name[:-4] if name.lower().endswith(".svg") else "")
        if hit is None:
            raise SampleNotFound(name)
        return hit[0]

    def thumbnail(self, name: str, size: int = 256) -> bytes:
        path = self.resolve(name)
        size = int(min(max(size, 32), 1024))
        st = path.stat()
        key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}|{size}".encode()).hexdigest()[:16]
        cached = self.thumb_dir / f"{key}.png"
        if cached.is_file():
            return cached.read_bytes()
        png = self.svg.thumbnail_png(path.read_bytes(), size)
        try:
            atomic_write_bytes(cached, png)
        except OSError as e:  # pragma: no cover - cache is best-effort
            log.debug("thumbnail cache write failed: %s", e)
        return png
