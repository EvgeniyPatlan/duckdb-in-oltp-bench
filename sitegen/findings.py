"""Findings: what one report's numbers say, as plain sentences computed from the data.

Every finding is a fact about one measure (a count, a range, a list of failures), stated the same
way for every system and listed in the site's fixed system order. A range is given without naming
its ends, and every system's own value is listed below it, so no finding ranks systems or gives an
overall verdict. The author's own reading, if any, is the separate notes file.
"""
from __future__ import annotations

from collections.abc import Iterable

HEADLINE_STREAMS = "16"


def _fmt(v: float) -> str:
    a = abs(v)
    if v == 0:
        return "0"
    return f"{v:,.0f}" if a >= 100 else f"{v:.2f}" if a >= 1 else f"{v:.3f}"


def _labels(data: dict) -> dict[str, str]:
    return {s["name"]: s["label"] for s in data["systems"]}


def _of_kind(data: dict, *kinds: str) -> list[str]:
    return [s["name"] for s in data["systems"] if s["kind"] in kinds]


def _finding(key: str, title: str, text: str, items: Iterable[str] = ()) -> dict:
    return {"key": key, "title": title, "text": text, "items": list(items)}


def _pct(share: float) -> str:
    """A share (0..1) as a percentage with at most one decimal: 0%, 4.9%, 86.7%."""
    return f"{100 * share:.1f}".rstrip("0").rstrip(".") + "%"


def _range(values: list[float], unit: str, fmt=_fmt) -> str:
    lo, hi = min(values), max(values)
    return f"{fmt(lo)}{unit}" if fmt(lo) == fmt(hi) else f"{fmt(lo)}{unit} to {fmt(hi)}{unit}"


def _covered(n: int, total: int, noun: str) -> str:
    return f"all {total} {noun}" if n == total else f"{n} of {total} {noun}"


def correctness(data: dict, sf: str) -> dict | None:
    power = data["power"].get(sf)
    if not power:
        return None
    labels, queries = _labels(data), [q for q in power["queries"] if q.startswith("q")]
    systems = [s["name"] for s in data["systems"] if s["name"] in power["cells"]]
    clean, items = 0, []
    for n in systems:
        cells = power["cells"][n]
        wrong = sorted(q for q in queries if cells.get(q, {}).get("s") == "wrong_result")
        other = sorted(f"{q} {cells[q]['s']}" for q in queries
                       if q in cells and cells[q]["s"] not in ("ok", "wrong_result"))
        missing = sorted(q for q in queries if q not in cells)
        if not wrong and not other and not missing:
            clean += 1
            continue
        parts = ([f"wrong result on {', '.join(wrong)}"] if wrong else []) + other
        parts += [f"no record for {', '.join(missing)}"] if missing else []
        items.append(f"{labels[n]}: {'; '.join(parts)}")
    text = (f"{clean} of {len(systems)} systems answered all {len(queries)} queries "
            f"with the reference result.")
    return _finding("correctness", "Correct answers", text, items)


def speed(data: dict, sf: str, noun: str = "integration") -> dict | None:
    power = data["power"].get(sf)
    if not power or not power["common_queries"]:
        return None
    labels, totals = _labels(data), power["totals"]
    names = [s["name"] for s in data["systems"] if s["name"] in totals]
    engines = [n for n in _of_kind(data, "integration") if n in totals]
    ok = {n: totals[n]["common"]["v"] for n in names if totals[n]["common"]["s"] == "ok"}
    engine_vals = [ok[n] for n in engines if n in ok]
    if not engine_vals:
        return None
    text = (f"On the {len(power['common_queries'])} queries every {noun} answered correctly, the "
            f"total warm time was {_range(engine_vals, ' s')} across "
            f"{_covered(len(engine_vals), len(engines), noun + 's')}")
    plain = [ok[n] for n in _of_kind(data, "plain") if n in ok]
    text += f", and {_range(plain, ' s')} for plain DuckDB." if plain else "."
    items = [f"{labels[n]}: {_fmt(ok[n])} s" if n in ok
             else f"{labels[n]}: n/a ({totals[n]['common'].get('r') or totals[n]['common']['s']})"
             for n in names]
    return _finding("speed", "Query time", text, items)


def overhead(data: dict, sf: str) -> dict | None:
    over = data.get("overhead", {}).get(sf)
    if not over:
        return None
    labels = _labels(data)
    names = [n for n in _of_kind(data, "integration") if n in over]
    ok, items = [], []
    for n in names:
        h = over[n]["headline"]
        if h["s"] == "ok" and h["v"] is not None:
            ok.append(h["v"])
            plain = h.get("plain") or (f"DuckDB {h['duckdb_version']}" if h.get("duckdb_version")
                                       else "plain DuckDB")
            items.append(f"{labels[n]}: {_fmt(h['v'])}x against {plain}")
        else:
            items.append(f"{labels[n]}: n/a ({h.get('r') or h['s']})")
    if not ok:
        return None
    text = (f"Against plain DuckDB at the same DuckDB version, the geometric-mean time ratio was "
            f"{_range(ok, 'x')} across {_covered(len(ok), len(names), 'integrations')}. 1x means "
            f"no cost for running inside the server.")
    return _finding("overhead", "Integration overhead", text, items)


def throughput(data: dict, sf: str) -> dict | None:
    thr = data.get("throughput", {}).get(sf)
    if not thr:
        return None
    labels = _labels(data)
    names = [s["name"] for s in data["systems"] if s["name"] in thr]
    at = {n: thr[n][HEADLINE_STREAMS] for n in names if HEADLINE_STREAMS in thr[n]}
    vals = [c["v"] for c in at.values() if c["s"] == "ok" and c["v"] is not None]
    if not vals:
        return None
    items = [f"{labels[n]}: {_fmt(c['v'])} qph" if c["s"] == "ok" else f"{labels[n]}: {c['s']}"
             for n, c in at.items()]
    for n in names:
        for streams, c in sorted(thr[n].items(), key=lambda kv: int(kv[0])):
            counts = c.get("counts") or {}
            bad = sum(v for k, v in counts.items() if k != "ok")
            if c["s"] != "ok":
                items.append(f"{labels[n]}, {streams} streams: {c['s']} ({c.get('r') or ''})")
            elif bad:
                items.append(f"{labels[n]}, {streams} streams: {bad} of {sum(counts.values())} "
                             f"queries did not complete")
    text = (f"At {HEADLINE_STREAMS} concurrent streams, throughput was {_range(vals, ' qph')} "
            f"across {_covered(len(vals), len(names), 'systems measured')}.")
    return _finding("throughput", "Concurrency", text, items)


def compat(data: dict, sf: str) -> dict | None:
    sc = data.get("sqlcompat", {}).get(sf)
    if not sc:
        return None
    labels, items, wrong = _labels(data), [], []
    titles = {p["id"]: p["title"] for p in sc["probes"]}
    for n in [s["name"] for s in data["systems"] if s["name"] in sc["counts"]]:
        c = sc["counts"][n]
        items.append(f"{labels[n]}: {c.get('error', 0)} errors, {c.get('wrong_result', 0)} wrong "
                     f"results, {c.get('n/a', 0)} n/a")
        wrong += [f"Wrong result, {labels[n]}: {titles.get(p, p)} ({p})"
                  for p, cell in sorted(sc["cells"][n].items()) if cell.get("c") == "wrong_result"]
    text = (f"{len(sc['probes'])} SQL statements an OLTP application would send, checked against "
            f"the server's own row-store tables. A wrong result is silent: the statement succeeds "
            f"and the answer differs.")
    return _finding("compat", "SQL compatibility", text, items + wrong)


def _share(c: dict) -> str:
    if c["s"] != "ok" or c["v"] is None:
        return c["s"]
    return _pct(c["v"]) + (f" ({_pct(c['unobserved'])} unobserved)" if c.get("unobserved") else "")


def fallback(data: dict, sf: str) -> dict | None:
    fb = data.get("fallback", {}).get(sf)
    if not fb:
        return None
    labels = _labels(data)
    names = [n for n in _of_kind(data, "integration") if n in fb]
    tpch = {n: fb[n]["fallback_share_tpch"] for n in names if "fallback_share_tpch" in fb[n]}
    probes = {n: fb[n]["fallback_share_probes"] for n in names if "fallback_share_probes" in fb[n]}
    if not tpch and not probes:
        return None
    seen = [c for c in tpch.values() if c["s"] == "ok" and c["v"] is not None]
    none_off = [c for c in seen if c["v"] == 0 and not c.get("unobserved")]
    text = (f"TPC-H queries seen on the row path: none on {len(none_off)} of the {len(seen)} "
            f"integrations that report it.")
    probe_vals = [c["v"] for c in probes.values() if c["s"] == "ok" and c["v"] is not None]
    if probe_vals:
        text += (" Of the read probes, the share answered on the row path instead of in DuckDB "
                 f"was {_range(probe_vals, '', _pct)}.")
    items = [f"{labels[n]}: TPC-H {_share(tpch[n]) if n in tpch else 'not reported'}, "
             f"probes {_share(probes[n]) if n in probes else 'not reported'}" for n in names]
    return _finding("fallback", "Silent slow paths", text, items)


def load(data: dict, sf: str) -> dict | None:
    ld = data.get("load", {}).get(sf)
    if not ld:
        return None
    labels = _labels(data)
    names = [s["name"] for s in data["systems"] if "load_bulk" in ld.get(s["name"], {})]
    cells = {n: ld[n]["load_bulk"] for n in names}
    vals = [c["v"] for c in cells.values() if c["s"] == "ok" and c["v"] is not None]
    if not vals:
        return None
    items = [f"{labels[n]}: {_fmt(c['v'])} s" if c["s"] == "ok" else f"{labels[n]}: {c['s']}"
             for n, c in cells.items()]
    return _finding("load", "Loading", "Bulk load of the TPC-H data took "
                    f"{_range(vals, ' s')} across {_covered(len(vals), len(names), 'systems')}.",
                    items)


def trackb(data: dict, sf: str) -> dict | None:
    found = correctness(data, sf)
    if not found:
        return None
    sp = speed(data, sf, noun="entry point")
    text = found["text"] + (" " + sp["text"] if sp else "")
    return _finding("trackb", "Track B (Parquet)", text, found["items"] + (sp["items"] if sp else []))


SECTIONS = (correctness, speed, overhead, throughput, compat, fallback, load)


def for_report(track_a: dict, track_b: dict, sf: str | None) -> list[dict]:
    """The findings for one scale factor of a report, in reading order."""
    if sf is None:
        return []
    out = [f for f in (s(track_a, sf) for s in SECTIONS) if f]
    tb = trackb(track_b, sf) if track_b.get("power") else None
    return out + ([tb] if tb else [])
