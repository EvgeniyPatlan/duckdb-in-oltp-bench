"""A small, safe Markdown renderer for the repo's own docs (headings, lists, tables, code)."""
from __future__ import annotations

import html
import re

_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_LINK = re.compile(r"\[([^\]\x00]+)\]\(([^)\s\x00]+)\)")


_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _safe_url(url: str) -> bool:
    """http(s), relative and fragment links only."""
    if not _SCHEME.match(url):
        return True
    return url.lower().startswith(("http://", "https://"))


def inline(text: str) -> str:
    """Escaped inline HTML; code spans and links are stashed first so each part is escaped once."""
    stash: list[str] = []

    def keep(html_text: str) -> str:
        stash.append(html_text)
        return f"\x00{len(stash) - 1}\x00"

    def link(m: re.Match) -> str:
        label, url = html.escape(m.group(1), quote=False), m.group(2)
        if not _safe_url(url):
            return keep(label)
        return keep(f'<a href="{html.escape(url, quote=True)}" rel="noopener">{label}</a>')

    out = _CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    out = _LINK.sub(link, out)
    out = _BOLD.sub(r"<strong>\1</strong>", html.escape(out, quote=False))
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], out)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table(lines: list[str]) -> str:
    head, rows = _cells(lines[0]), [_cells(line) for line in lines[2:]]
    th = "".join(f"<th>{inline(c)}</th>" for c in head)
    body = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>"
                   for row in rows)
    return f'<div class="tablewrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody>' \
           "</table></div>"


def _is_table(lines: list[str], i: int) -> bool:
    return (lines[i].lstrip().startswith("|") and i + 1 < len(lines)
            and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]) is not None)


def render(source: str, base_level: int = 2) -> str:
    lines, out, i = source.splitlines(), [], 0
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(lines[i + 1:j]))}</code></pre>")
            i = j + 1
            continue
        if _is_table(lines, i):
            flush()
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            out.append(_table(lines[i:j]))
            i = j
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush()
            level = min(6, len(m.group(1)) + base_level - 1)
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
        elif re.match(r"^\s*[-*]\s+", line):
            flush()
            items = []
            while i < len(lines) and re.match(r"^\s*([-*]\s+|\s{2,}\S)", lines[i]):
                if re.match(r"^\s*[-*]\s+", lines[i]):
                    items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(t)}</li>" for t in items) + "</ul>")
            continue
        elif line.strip():
            para.append(line.strip())
        else:
            flush()
        i += 1
    flush()
    return "\n".join(out)
