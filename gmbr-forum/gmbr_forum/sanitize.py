"""Limpa o HTML raspado: só tags/atributos seguros, links absolutos e
links internos do gmbr apontando para o próprio fórum local."""
from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin

BASE = "https://gmbr.forumeiros.com/"

ALLOWED = {
    "a", "abbr", "b", "blockquote", "br", "caption", "center", "cite", "code",
    "dd", "del", "details", "div", "dl", "dt", "em", "font", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "i", "img", "ins", "li", "ol", "p", "pre", "q", "s",
    "small", "span", "strike", "strong", "sub", "summary", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "tt", "u", "ul",
}
VOID = {"br", "hr", "img"}
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "noscript",
                "form", "textarea", "select", "button", "template", "svg", "math"}
ATTRS = {"align", "alt", "border", "cellpadding", "cellspacing", "color",
         "colspan", "rowspan", "face", "height", "width", "size", "title",
         "valign", "start", "type"}
SAFE_STYLE = re.compile(
    r"^\s*(color|background-color|font-size|font-weight|font-style|font-family|"
    r"text-align|text-decoration|width|height|max-width|margin(-\w+)?|"
    r"padding(-\w+)?|border(-\w+)?|float|display|line-height)\s*:\s*"
    r"[#\w\s.,%'\"()-]{1,80}$", re.I)
TOPIC_LINK = re.compile(r"^https?://gmbr\.forumeiros\.com/t(\d+)(?:p(\d+))?[-\w]*", re.I)


def _url(value: str) -> str | None:
    value = (value or "").strip()
    if not value or value.startswith("#"):
        return value or None
    url = urljoin(BASE, value)
    if not re.match(r"^https?://", url, re.I):
        return None  # javascript:, data:, etc.
    m = TOPIC_LINK.match(url)
    if m:
        return f"/t/{m.group(1)}" + (f"?start={m.group(2)}" if m.group(2) else "")
    return url


def _style(value: str) -> str:
    return "; ".join(p.strip() for p in value.split(";")
                     if SAFE_STYLE.match(p) and not re.search(r"url\(|expression|\\", p, re.I))


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT:
            if tag not in VOID:
                self.skip += 1
            return
        if self.skip or tag not in ALLOWED:
            return
        kept = []
        for k, v in attrs:
            v = v or ""
            if k in ATTRS:
                kept.append((k, v))
            elif k == "style":
                s = _style(v)
                if s:
                    kept.append((k, s))
            elif k == "href" and tag == "a":
                u = _url(v)
                if u:
                    kept.append((k, u))
            elif k == "src" and tag == "img":
                u = _url(v)
                if u:
                    kept.append((k, u))
        if tag == "a":
            href = dict(kept).get("href", "")
            if href.startswith("http"):
                kept += [("rel", "nofollow noopener noreferrer"), ("target", "_blank")]
        if tag == "img":
            if "src" not in dict(kept):
                return
            kept += [("loading", "lazy"), ("referrerpolicy", "no-referrer")]
        self.out.append("<" + tag + "".join(f' {k}="{escape(v)}"' for k, v in kept) + ">")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.stack[-1] == tag:
            self.stack.pop()
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data, quote=False))


def clean_html(html: str | None) -> str:
    if not html:
        return ""
    c = _Cleaner()
    c.feed(html)
    c.close()
    c.out.extend(f"</{t}>" for t in reversed(c.stack))
    return "".join(c.out)
