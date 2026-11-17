#!/usr/bin/env python3
"""Rebuild docs/ from the architecture page.

  python3 scripts/build-docs.py <architecture page .html>

Writes:
  docs/architecture.html     the page as a standalone HTML file (open it in a browser)
  docs/diagrams/*.svg        each diagram as a standalone SVG (light theme, white background)
  docs/ARCHITECTURE.md       the same content as Markdown, rendered by GitHub
"""
import html
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
VOID = {"meta", "link", "br", "img", "input", "hr"}


class Node:
    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs or {}), parent, []

    def cls(self):
        return self.attrs.get("class", "").split()

    def find_all(self, tag):
        for c in self.children:
            if isinstance(c, Node):
                if c.tag == tag:
                    yield c
                yield from c.find_all(tag)

    def text(self):
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)


class Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.cur = Node("root")

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def inline(node) -> str:
    out = []
    for c in node.children:
        if isinstance(c, str):
            out.append(re.sub(r"\s+", " ", c))
        elif c.tag in ("b", "strong"):
            out.append(f"**{inline(c).strip()}**")
        elif c.tag == "code":
            out.append(f"`{c.text()}`")
        elif c.tag == "i" and "sw" in c.cls():
            continue
        else:
            out.append(inline(c))
    return "".join(out)


def table_md(t) -> str:
    rows = [[inline(cell).strip().replace("|", "\\|") for cell in tr.children if isinstance(cell, Node)]
            for tr in t.find_all("tr")]
    head, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def main(src: Path):
    raw = src.read_text()
    # accept either the artifact source or a page this script already wrapped
    raw = re.sub(r"(?is)<!doctype html>\s*<html[^>]*>\s*<head>\s*<meta charset=\"utf-8\">\s*<meta name=\"viewport\"[^>]*>\s*", "", raw)
    raw = re.sub(r"(?is)</head>\s*<body>\s*", "\n", raw)
    raw = re.sub(r"(?is)\s*</body>\s*</html>\s*$", "\n", raw)
    head, _, body = raw.partition("</style>")
    head += "</style>"
    title = re.search(r"<title>(.*?)</title>", raw).group(1)
    style = re.search(r"<style>(.*?)</style>", raw, re.S).group(1)

    DOCS.mkdir(exist_ok=True)
    (DOCS / "diagrams").mkdir(exist_ok=True)
    for old in (DOCS / "diagrams").glob("*.svg"):  # numbering shifts when sections move
        old.unlink()

    # 1. standalone page
    (DOCS / "architecture.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f"{head}\n</head>\n<body>\n{body.strip()}\n</body>\n</html>\n")

    # 2. standalone SVGs: light tokens + the svg rules, inlined
    tokens = re.search(r":root\{(.*?)\}", style, re.S).group(1)
    svg_rules = "\n".join(line for line in style.splitlines()
                          if re.match(r"^\.(box|zone|t|s|lb|ln|life|head-[a-z]+|lane|lane-line)(?=[.{])", line))
    svg_css = f"svg{{{tokens};color:var(--fg);font-family:Inter,system-ui,sans-serif}}\n" + svg_rules

    tree = Builder()
    tree.feed(body)
    names = []
    for sec in tree.root.find_all("section"):
        h2 = next(sec.find_all("h2"), None)
        for i, _ in enumerate(sec.find_all("svg")):
            base = re.sub(r"[^a-z0-9]+", "-", h2.text().lower()).strip("-")
            names.append(base if i == 0 else f"{base}-{i + 1}")
    svgs = re.findall(r"<svg\b.*?</svg>", body, re.S)
    assert len(svgs) == len(names), (len(svgs), len(names))
    files = {}
    for n, (name, svg) in enumerate(zip(names, svgs), 1):
        vb = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        w, h = vb.group(1), vb.group(2)
        svg = svg.replace("<svg ", f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" ', 1)
        svg = re.sub(r"(<svg[^>]*>)", rf'\1<style>{svg_css}</style><rect width="100%" height="100%" fill="#ffffff"/>',
                     svg, count=1)
        fname = f"{n:02d}-{name}.svg"
        (DOCS / "diagrams" / fname).write_text(svg + "\n")
        files[id(svg)] = fname
    svg_files = [f"{n:02d}-{name}.svg" for n, name in enumerate(names, 1)]

    # 3. Markdown
    md, svg_i = [], 0
    wrap = next(n for n in tree.root.find_all("div") if "wrap" in n.cls())

    def walk(node):
        nonlocal svg_i
        for c in node.children:
            if not isinstance(c, Node):
                continue
            k = c.cls()
            if c.tag == "header":
                walk(c)
            elif c.tag == "span" and "eyebrow" in k:
                md.append(f"_{inline(c).strip()}_")
            elif c.tag == "h1":
                md.insert(0, f"# {inline(c).strip()}")
            elif c.tag == "h2":
                md.append(f"## {inline(c).strip()}")
            elif c.tag == "h3":
                md.append(f"### {inline(c).strip()}")
            elif c.tag == "p":
                md.append(inline(c).strip())
            elif c.tag == "ol":
                md.append("\n".join(f"{i}. {inline(li).strip()}" for i, li in
                                    enumerate((x for x in c.children if isinstance(x, Node)), 1)))
            elif c.tag == "ul":
                md.append("\n".join(f"- {inline(li).strip()}" for li in c.children if isinstance(li, Node)))
            elif c.tag == "pre":
                md.append(f"```\n{c.text().strip()}\n```")
            elif c.tag == "figure":
                for part in c.children:
                    if not isinstance(part, Node):
                        continue
                    if "frame" in part.cls():
                        svg = next(part.find_all("svg"))
                        alt = svg.attrs.get("aria-label", "diagram").replace("]", ")")
                        md.append(f"![{alt[:120]}](diagrams/{svg_files[svg_i]})")
                        svg_i += 1
                    elif "legend" in part.cls():
                        md.append("Legend: " + " · ".join(inline(s).strip() for s in part.children if isinstance(s, Node)))
                    elif part.tag == "figcaption":
                        md.append(f"_{inline(part).strip()}_")
            elif c.tag == "div" and "tbl" in k:
                md.append(table_md(next(c.find_all("table"))))
            elif c.tag in ("section", "div"):
                walk(c)

    walk(wrap)
    note = ("> Generated from `docs/architecture.html` by `scripts/build-docs.py`. "
            "Open the HTML file for the interactive version with dark mode.")
    md.insert(1, note)
    (DOCS / "ARCHITECTURE.md").write_text(html.unescape("\n\n".join(md)) + "\n")
    print(f"docs/architecture.html, {len(svg_files)} diagrams, docs/ARCHITECTURE.md ({title})")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
