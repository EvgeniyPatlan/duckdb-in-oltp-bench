"""The user docs must agree with the lock file, the site labels and the files they link to.

Print the per-system table for ``systems/README.md`` with::

    scripts/dev.sh env PYTHONPATH=. python tests/test_docs.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sitegen.model import LABELS, config_dir, label_of

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "systems" / "versions.lock.json"
BEGIN, END = "<!-- table:begin -->", "<!-- table:end -->"
HEADER = ("| System | Track | Label on the site | Server | Integration | DuckDB | Image | Pin |\n"
          "|---|---|---|---|---|---|---|---|")
DOCS = ("README.md", "docs/reproduce.md", "systems/README.md", "campaigns/README.md")
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")


def _lock() -> dict:
    return json.loads(LOCK.read_text(encoding="utf-8"))


def _integration(server_version: str, duckdb_version: str | None, system: str) -> tuple[str, str]:
    server, sep, integration = server_version.partition(" + ")
    if sep:
        return server, integration
    if system.startswith("duckdb-"):
        return server, "none (plain DuckDB)"
    if duckdb_version is None:
        return server, "none (row-store baseline)"
    return server, "built into the server"


def render_table(lock: dict | None = None) -> str:
    lock = _lock() if lock is None else lock
    rows = [HEADER]
    for system in sorted(lock):
        e = lock[system]
        server, integration = _integration(e["server_version"], e["duckdb_version"], system)
        track = "B" if system == "villagesql" else "A"
        pin = "registry digest" if e["digest_kind"] == "registry" else "local build"
        image = e["image"].split("@", 1)[0]
        link = f"[`{system}`]({config_dir(system)}/README.md)"
        rows.append(f"| {link} | {track} | {label_of(system)} | {server} | {integration} | "
                    f"{e['duckdb_version'] or 'none'} | `{image}` | {pin} |")
    return "\n".join(rows)


def _documented_table() -> str:
    text = (ROOT / "systems" / "README.md").read_text(encoding="utf-8")
    assert BEGIN in text and END in text, "systems/README.md lost its table markers"
    return text.split(BEGIN, 1)[1].split(END, 1)[0].strip()


def test_systems_table_matches_lock_and_labels():
    assert _documented_table() == render_table(), (
        "systems/README.md is out of date; regenerate it with: "
        "scripts/dev.sh env PYTHONPATH=. python tests/test_docs.py")


def test_every_lock_entry_has_a_row_and_a_readme():
    for system in _lock():
        assert f"[`{system}`]({config_dir(system)}/README.md)" in _documented_table()
        assert (ROOT / "systems" / config_dir(system) / "README.md").is_file()


def test_labels_are_in_the_table():
    table = _documented_table()
    for system in _lock():
        assert f"| {label_of(system)} |" in table
    for name, label in LABELS.items():
        if name in _lock():
            assert f"| {label} |" in table


@pytest.mark.parametrize("doc", DOCS)
def test_relative_links_resolve(doc):
    path = ROOT / doc
    for target in LINK.findall(path.read_text(encoding="utf-8")):
        if re.match(r"[a-z]+:|#", target):
            continue
        assert (path.parent / target.split("#", 1)[0]).exists(), f"{doc}: broken link {target}"


if __name__ == "__main__":
    print(render_table())
