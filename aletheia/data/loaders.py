"""Data loading for the shapes datasets actually come in: a single file, a
*directory* of files (multi-file / sharded), a *very large* file (read a sample /
cap rows), or an *online* URL (download, extracting archives).

Centralized here so the profiler (sampling for the dashboard) and the domain
plugin (full / capped load for training) share one code path. ``pandas`` is a
Phase-1 dependency, imported lazily so this module stays import-safe.
"""

from __future__ import annotations

import os
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# tabular file extensions we know how to read
TABULAR_EXTS = (".csv", ".tsv", ".parquet", ".pq", ".json", ".jsonl", ".ndjson")
ARCHIVE_EXTS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")


def is_url(ref: str | None) -> bool:
    return isinstance(ref, str) and ref.lower().startswith(("http://", "https://"))


def _is_archive(name: str) -> bool:
    n = name.lower()
    return any(n.endswith(e) for e in ARCHIVE_EXTS)


def list_tabular_files(path: Path, pattern: str | None = None) -> list[Path]:
    """Resolve a path to the tabular files under it (a file -> itself; a directory
    -> a recursive, sorted glob of known tabular extensions or ``pattern``)."""
    path = Path(path)
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    if pattern:
        files = [p for p in path.rglob(pattern) if p.is_file()]
    else:
        files = [
            p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in TABULAR_EXTS
        ]
    return sorted(files)


def read_one(path: Path, nrows: int | None = None) -> Any:
    """Read a single tabular file (csv/tsv/parquet/json/jsonl)."""
    import pandas as pd

    p = str(path)
    ext = Path(path).suffix.lower()
    if ext in (".parquet", ".pq"):
        df = pd.read_parquet(p)
        return df.head(nrows) if nrows is not None else df
    if ext in (".jsonl", ".ndjson"):
        return pd.read_json(p, lines=True, nrows=nrows)
    if ext == ".json":
        df = pd.read_json(p)
        return df.head(nrows) if nrows is not None else df
    if ext == ".tsv":
        return pd.read_csv(p, sep="\t", nrows=nrows)
    return pd.read_csv(p, nrows=nrows)


def read_tabular(
    path: Path | str,
    nrows: int | None = None,
    pattern: str | None = None,
    max_files: int | None = None,
) -> Any:
    """Read a file OR a directory of files into one DataFrame.

    ``nrows`` caps the TOTAL rows (read files until reached) — the lever for very
    large / sharded datasets. Files are concatenated in sorted order.
    """
    import pandas as pd

    files = list_tabular_files(Path(path), pattern)
    if not files:
        raise FileNotFoundError(
            f"no tabular files ({', '.join(TABULAR_EXTS)}) found at {path}"
        )
    if max_files is not None:
        files = files[:max_files]

    frames: list[Any] = []
    remaining = nrows
    for f in files:
        df = read_one(f, nrows=remaining)
        frames.append(df)
        if remaining is not None:
            remaining -= len(df)
            if remaining <= 0:
                break
    out = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    if nrows is not None and len(out) > nrows:
        out = out.head(nrows)
    return out


def count_rows(path: Path | str) -> int | None:
    """Cheap total row count when possible (parquet metadata); else None (counting
    a huge CSV is as expensive as reading it, so we don't)."""
    files = list_tabular_files(Path(path))
    if not files:
        return None
    total = 0
    for f in files:
        if f.suffix.lower() in (".parquet", ".pq"):
            try:
                import pyarrow.parquet as pq

                total += pq.ParquetFile(str(f)).metadata.num_rows
            except Exception:
                return None
        else:
            return None
    return total or None


def _extract(archive: Path, dest: Path) -> None:
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            try:
                t.extractall(dest, filter="data")  # py>=3.11.4: block path traversal
            except TypeError:  # pragma: no cover - older python
                t.extractall(dest)


def download(url: str, dest_dir: Path | str, timeout: float = 300.0) -> Path:
    """Download a URL into ``dest_dir``. If it's an archive, extract it and return
    the extracted directory; otherwise return the downloaded file path."""
    import httpx

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = os.path.basename(urlparse(url).path) or "download.bin"
    target = dest_dir / name
    with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(target, "wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)
    if _is_archive(name):
        extract_dir = dest_dir / f"{name}.extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        _extract(target, extract_dir)
        return extract_dir
    return target


def materialize(data_spec: dict[str, Any], data_dir: Path | str | None = None) -> Path:
    """Return a local path (file or directory) for a data spec, downloading a URL
    if it isn't already materialized. Prefers an existing local ``uri``."""
    uri = data_spec.get("uri")
    ref = data_spec.get("ref")
    source = data_spec.get("source")

    if uri and Path(uri).exists():
        return Path(uri)
    if source == "url" or is_url(ref) or is_url(uri):
        target_dir = Path(data_dir) if data_dir else Path(tempfile.mkdtemp(prefix="aletheia-data-"))
        return download(str(ref or uri), target_dir)
    p = Path(ref or uri or "")
    if not p.exists():
        raise FileNotFoundError(f"data path does not exist: {p}")
    return p
