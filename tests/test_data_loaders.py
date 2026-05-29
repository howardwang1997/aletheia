"""Phase 1: data loading for the shapes data actually comes in —
directory (multi-file), large file (sampling/caps), and online URL (download +
archive extraction)."""

from __future__ import annotations

import functools
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

from aletheia.data.loaders import download, list_tabular_files, materialize, read_tabular
from aletheia.data.profiler import profile_path
from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin


def _csv(path: Path, rows: list[tuple]) -> None:
    pd.DataFrame(rows, columns=["composition", "band_gap"]).to_csv(path, index=False)


def test_directory_concat_and_row_cap(tmp_path):
    d = tmp_path / "ds"
    d.mkdir()
    _csv(d / "a.csv", [("Si", 1.1), ("Ge", 0.7)])
    _csv(d / "b.csv", [("GaAs", 1.4), ("ZnO", 3.3)])
    assert len(list_tabular_files(d)) == 2

    df = read_tabular(d)
    assert len(df) == 4 and set(df.columns) == {"composition", "band_gap"}
    # total-row cap across files (lever for very large / sharded data)
    assert len(read_tabular(d, nrows=3)) == 3


def test_profile_directory(tmp_path):
    d = tmp_path / "ds"
    d.mkdir()
    _csv(d / "a.csv", [("Si", 1.1), ("Ge", 0.7)])
    _csv(d / "b.csv", [("GaAs", 1.4)])
    prof = profile_path(str(d), target_hint="band_gap")
    assert prof["is_directory"] is True
    assert prof["n_files"] == 2
    assert prof["profiled_rows"] == 3
    assert "band_gap" in prof["target_candidates"]


def test_large_file_sampling(tmp_path):
    big = tmp_path / "big.csv"
    _csv(big, [("Si", i * 0.001) for i in range(5000)])
    prof = profile_path(str(big), sample_rows=100)
    assert prof["profiled_rows"] == 100
    assert prof["sampled"] is True


def test_online_download_file_and_zip(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    _csv(served / "data.csv", [("Si", 1.1), ("Ge", 0.7)])
    with zipfile.ZipFile(served / "data.zip", "w") as z:
        z.write(served / "data.csv", "inner/data.csv")

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(served))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        # plain online file
        f = download(f"{base}/data.csv", tmp_path / "dl1")
        assert Path(f).is_file()
        assert len(read_tabular(f)) == 2
        # online archive -> extracted directory
        d = download(f"{base}/data.zip", tmp_path / "dl2")
        assert Path(d).is_dir()
        df = read_tabular(d)
        assert len(df) == 2 and "composition" in df.columns
    finally:
        httpd.shutdown()


def test_materialize_prefers_local_path(tmp_path):
    d = tmp_path / "ds"
    d.mkdir()
    _csv(d / "a.csv", [("Si", 1.1)])
    assert materialize({"source": "directory", "ref": str(d)}) == d


def test_plugin_loads_directory(tmp_path):
    d = tmp_path / "ds"
    d.mkdir()
    _csv(d / "a.csv", [("Si", 1.1), ("Ge", 0.7)])
    _csv(d / "b.csv", [("GaAs", 1.4), ("ZnO", 3.3)])
    df = MaterialsBandGapPlugin().load_data(
        {"source": "directory", "ref": str(d), "target_column": "band_gap"}
    )
    assert len(df) == 4
    assert df.attrs["data_spec"]["source"] == "directory"
