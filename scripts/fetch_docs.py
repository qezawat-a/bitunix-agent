#!/usr/bin/env python3
"""Extract Bitunix VitePress API docs to clean markdown.

The docs site is a VitePress SPA. The real page URLs live in
`window.__VP_SITE_DATA__.themeConfig.sidebar`; each page HTML references a
`*.lean.js` asset that contains the rendered HTML as a JS string literal.
"""
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://www.bitunix.com/api-docs"
OUT = Path(__file__).resolve().parent.parent / "jrrock" / "docs"
RAW = Path(__file__).resolve().parent.parent / "jrrock" / "docs_raw"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

try:
    from markdownify import markdownify as md
except ImportError:  # pragma: no cover
    md = None


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def clean(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def fname_for(link: str) -> str:
    """`/futures/trade/place_order` -> `futures_trade_place_order.md`"""
    return link.strip("/").replace("/", "_").replace(" ", "_") + ".md"


def sidebar_links() -> list:
    index = fetch(f"{BASE}/index.html")
    m = re.search(r"window\.__VP_SITE_DATA__=JSON\.parse\(\"(.*?)\"\);</script>", index, re.S)
    if not m:
        raise SystemExit("could not find site data")
    raw = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
    data = json.loads(raw)
    links = []

    def walk(node):
        if isinstance(node, dict):
            link = node.get("link")
            if isinstance(link, str):
                links.append(link)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data.get("themeConfig", {}))
    out = []
    for l in links:
        if l not in out:
            out.append(l)
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)

    links = sidebar_links()
    print(f"found {len(links)} sidebar links")

    ok = fail = 0
    for link in links:
        name = fname_for(link)
        page_url = f"{BASE}{urllib.parse.quote(link, safe='/')}.html"
        try:
            page_html = fetch(page_url)
        except Exception as e:
            print(f"  ! {name}: page fetch failed: {e}")
            fail += 1
            continue

        am = re.search(r"assets/([^\"']+\.lean\.js)", page_html) or re.search(
            r"assets/([^\"']+\.js)", page_html
        )
        if not am:
            print(f"  ! {name}: no payload asset")
            fail += 1
            continue

        asset = f"{BASE}/assets/{urllib.parse.quote(am.group(1), safe='._-')}"
        try:
            js = fetch(asset)
        except Exception as e:
            print(f"  ! {name}: asset fetch failed: {e}")
            fail += 1
            continue

        stem = Path(name).stem
        (RAW / f"{stem}.lean.js").write_text(js, encoding="utf-8")

        # The HTML helper is aliased per-bundle (`a1 as a`, `a1 as r`, ...) and
        # the payload may be a single-quoted string or a backtick template.
        alias = next(iter(re.findall(r"\ba1 as (\w+)", js)), None)
        if not alias:
            print(f"  ! {name}: no html helper alias")
            fail += 1
            continue

        m = re.search(rf"{re.escape(alias)}\s*\(\s*(['\"`])", js)
        if not m:
            print(f"  ! {name}: no payload call for alias {alias}")
            fail += 1
            continue

        start = m.end()
        # Render wrapper closes as: `<payload>`,12)]))}const ... }
        end = js.rfind(")]))}")
        if end == -1 or end <= start:
            print(f"  ! {name}: malformed payload tail")
            fail += 1
            continue

        chunk = js[start:end]
        # strip the trailing `,14)` and the closing quote character
        chunk = re.sub(r",\s*\d+\)\s*$", "", chunk)
        if chunk and chunk[-1] in "'\"`":
            chunk = chunk[:-1]

        # `unicode_escape` mangles non-ASCII (UTF-8 bytes become mojibake), so
        # reverse the JS escaping only, preserving the original UTF-8 text.
        body = chunk.replace("\\/", "/").replace('\\"', '"').replace("\\`", "`")
        body = body.replace("\\n", "\n").replace("\\t", "\t")
        body = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), body)
        (RAW / f"{stem}.html").write_text(body, encoding="utf-8")

        text = md(body, heading_style="ATX", bullets="-") if md else re.sub(r"(?s)<[^>]+>", " ", body)
        text = clean(text)

        title = ""
        tm = re.search(r"JSON\.parse\('(\{\"title\".*?\})'\)", js)
        if tm:
            try:
                title = json.loads(tm.group(1).replace("\\'", "'")).get("title", "")
            except Exception:
                title = ""

        header = (
            f"<!-- Bitunix doc: {link} | source: {page_url} -->\n"
            f"# {title or stem}\n\n"
        )
        (OUT / name).write_text(header + text + "\n", encoding="utf-8")
        ok += 1

    print(f"extracted {ok} pages, {fail} failures -> {OUT}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
