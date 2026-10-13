"""Machine accessibility checks for an HTML page, without a browser.

Used two ways: to check the pages people build with the site, and to test the site's own pages.
These checks are honest about their limits: they read the HTML, so they cannot hear a screen reader
or see the layout. Every report lists what was NOT checked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Build maps the older wording of these lines, which is stored with older versions (features/build.py).
NOT_CHECKED = [
    "How a real screen reader reads the page, in speech and in braille (ask for a peer check).",
    "Contrast, layout and spacing: not measured here.",
    "Whether the page works on a phone with zoom at 400 percent.",
    "Whether buttons and links do what their names say.",
]

RULES = {
    "html-lang": "The page says which language it is in.",
    "document-title": "The page has a title.",
    "one-h1": "The page has exactly one main heading (h1).",
    "heading-order": "Headings do not skip levels.",
    "empty-heading": "No heading is empty.",
    "img-alt": "Every image has a text alternative (alt).",
    "form-label": "Every form field has a label.",
    "button-name": "Every button has a name.",
    "link-name": "Every link has a name.",
    "duplicate-id": "No two elements share an id.",
    "viewport-zoom": "The page lets people zoom.",
    "landmark-main": "The page has a main region.",
    "iframe-title": "Every frame has a title.",
    "tabindex-positive": "No element forces its own tab order.",
    "meta-refresh": "The page does not refresh or redirect by itself.",
    "aria-hidden-focus": "Nothing hidden from screen readers can still be focused.",
    "table-headers": "Data tables have header cells.",
    "autoplay": "Nothing plays sound by itself.",
    "page-weight": "The page is light enough for slow phones (under 60 KB of HTML).",
    "needs-javascript": "The page shows its content without JavaScript.",
}

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_FOCUSABLE = {"a", "button", "input", "select", "textarea", "iframe"}
_NO_LABEL_INPUTS = {"hidden", "submit", "button", "reset", "image"}


@dataclass
class Finding:
    rule: str
    severity: str  # "error" or "warning"
    # The effect on visitors in plain words first, for the member; then the technical fix, for the AI.
    message: str
    where: str = ""


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=lambda: list(NOT_CHECKED))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def to_dict(self) -> dict:
        return {
            "findings": [f.__dict__ for f in self.findings],
            "passed": self.passed,
            "not_checked": self.not_checked,
        }


@dataclass
class _Node:
    tag: str
    attrs: dict
    parent: "_Node | None"
    children: list = field(default_factory=list)  # _Node or str
    line: int = 0

    def text(self) -> str:
        parts: list[str] = []
        for c in self.children:
            if isinstance(c, str):
                parts.append(c)
            elif c.tag in ("script", "style", "template"):
                continue
            elif c.tag == "img":
                parts.append(c.attrs.get("alt") or "")
            else:
                parts.append(c.text())
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    def walk(self):
        for c in self.children:
            if isinstance(c, _Node):
                yield c
                yield from c.walk()

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur, line=self.getpos()[0])
        self.cur.children.append(node)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur, line=self.getpos()[0])
        self.cur.children.append(node)

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def _where(node: _Node) -> str:
    bits = [f"<{node.tag}"]
    for key in ("id", "name", "type", "href", "src"):
        if node.attrs.get(key):
            bits.append(f' {key}="{node.attrs[key][:40]}"')
    return "".join(bits) + f"> on line {node.line}"


def _accessible_name(node: _Node, by_id: dict) -> str:
    if node.attrs.get("aria-label", "").strip():
        return node.attrs["aria-label"].strip()
    if node.attrs.get("aria-labelledby", "").strip():
        names = [by_id[i].text() for i in node.attrs["aria-labelledby"].split() if i in by_id]
        if any(names):
            return " ".join(names)
    return node.text() or node.attrs.get("title", "").strip()


def check_html(html: str, *, name: str = "page") -> Report:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    root = builder.root
    nodes = list(root.walk())
    by_tag: dict[str, list[_Node]] = {}
    for n in nodes:
        by_tag.setdefault(n.tag, []).append(n)
    by_id: dict[str, _Node] = {}
    report = Report()
    failed: set[str] = set()

    def fail(rule: str, message: str, node: _Node | None = None, severity: str = "error"):
        report.findings.append(Finding(rule, severity, message, _where(node) if node else ""))
        failed.add(rule)

    # html-lang
    html_nodes = by_tag.get("html", [])
    if not html_nodes or not html_nodes[0].attrs.get("lang", "").strip():
        fail("html-lang", "Screen readers may read this page in the wrong language, in speech and in braille. "
                         "Add a lang attribute to <html>, for example lang=\"en\".")
    # document-title
    titles = by_tag.get("title", [])
    if not titles or not titles[0].text():
        fail("document-title", "Visitors cannot tell which page or tab this is: the title is the first thing a screen "
                               "reader gives them. Add a <title> that says what the page is.")
    # duplicate ids
    seen: set[str] = set()
    for n in nodes:
        i = n.attrs.get("id")
        if i:
            if i in seen:
                fail("duplicate-id", f"Labels and links that point to \"{i}\" may go to the wrong place. The id "
                                     f"\"{i}\" is used more than once. Give each element its own id.", n)
            seen.add(i)
            by_id.setdefault(i, n)
    # headings
    headings = [n for n in nodes if re.fullmatch(r"h[1-6]", n.tag)]
    h1s = [h for h in headings if h.tag == "h1"]
    if len(h1s) != 1:
        has = "no main heading (h1)" if not h1s else f"{len(h1s)} main headings (h1)"
        fail("one-h1", "Visitors who move by headings cannot find one main heading that says what the page is. "
                       f"The page has {has}. Use exactly one, describing the page.",
             severity="warning" if len(h1s) > 1 else "error")
    last = 0
    for h in headings:
        level = int(h.tag[1])
        if last and level > last + 1:
            fail("heading-order", "Visitors who move by headings may think a section is missing. The heading level "
                                  f"jumps from h{last} to h{level}. Use h{last + 1} instead.", h, "warning")
        last = level
        if not h.text():
            fail("empty-heading", "Visitors who move by headings land on a heading with nothing in it. This heading is "
                                  "empty. Give it text, or remove it.", h)
    # images
    for img in by_tag.get("img", []):
        if "alt" not in img.attrs:
            fail("img-alt", "Screen readers may give visitors this image's file name instead of what it shows. Add alt "
                           "text that says what it shows, or alt=\"\" if it is only decoration.", img)
    # form labels
    labels_for = {n.attrs.get("for") for n in by_tag.get("label", []) if n.attrs.get("for")}
    for n in by_tag.get("input", []) + by_tag.get("select", []) + by_tag.get("textarea", []):
        if n.tag == "input" and n.attrs.get("type", "text").lower() in _NO_LABEL_INPUTS:
            continue
        labelled = (
            n.attrs.get("id") in labels_for
            or any(a.tag == "label" for a in n.ancestors())
            or n.attrs.get("aria-label", "").strip()
            or n.attrs.get("aria-labelledby", "").strip()
            or n.attrs.get("title", "").strip()
        )
        if not labelled:
            fail("form-label", "Visitors cannot tell what to enter in this form field, because it has no label. "
                              "Add a <label for=...> that says what to enter.", n)
    # buttons
    for b in by_tag.get("button", []):
        if not _accessible_name(b, by_id):
            fail("button-name", "Visitors cannot tell what this button does, because it has no name. "
                               "Add text inside it or an aria-label.", b)
    for n in by_tag.get("input", []):
        if n.attrs.get("type", "").lower() in ("submit", "button", "reset") and not (
            n.attrs.get("value", "").strip() or n.attrs.get("aria-label", "").strip()
        ):
            fail("button-name", "Visitors cannot tell what this button does, because it has no name. "
                               "Add a value attribute.", n)
        if n.attrs.get("type", "").lower() == "image" and not n.attrs.get("alt", "").strip():
            fail("button-name", "Visitors cannot tell what this image button does, because it has no name. "
                               "Add alt text to it.", n)
    # links
    for a in by_tag.get("a", []):
        if "href" in a.attrs and not _accessible_name(a, by_id):
            fail("link-name", "Visitors cannot tell where this link goes, because it has no name. "
                             "Add text inside it, or alt text to its image.", a)
    # viewport
    for m in by_tag.get("meta", []):
        if m.attrs.get("name", "").lower() == "viewport":
            content = m.attrs.get("content", "").replace(" ", "").lower()
            mx = re.search(r"maximum-scale=([\d.]+)", content)
            if "user-scalable=no" in content or "user-scalable=0" in content or (mx and float(mx.group(1)) < 2):
                fail("viewport-zoom", "Visitors who zoom to read cannot make this page larger. The viewport stops "
                                      "zooming. Remove user-scalable=no and maximum-scale.", m)
        if m.attrs.get("http-equiv", "").lower() == "refresh":
            fail("meta-refresh", "Visitors lose their place, in speech, braille or magnification, when the page "
                                     "changes without warning. It refreshes or redirects by itself (meta refresh). "
                                     "Remove it, or use a link.", m)
    # main landmark
    if not by_tag.get("main") and not any(n.attrs.get("role") == "main" for n in nodes):
        fail("landmark-main", "Visitors cannot jump straight to the main content. Wrap it in <main>.")
    # iframes
    for f in by_tag.get("iframe", []):
        if not f.attrs.get("title", "").strip():
            fail("iframe-title", "Visitors cannot tell what this frame holds before they go into it. Give the "
                                "<iframe> a title that says what it contains.", f)
    # tabindex
    for n in nodes:
        t = n.attrs.get("tabindex", "").strip()
        if t.lstrip("-").isdigit() and int(t) > 0:
            fail("tabindex-positive", "Keyboard visitors move through the page in an unexpected order. Avoid "
                                         "tabindex above 0.", n, "warning")
    # aria-hidden on focusable
    for n in nodes:
        if n.attrs.get("aria-hidden") == "true":
            for inner in [n] + list(n.walk()):
                if inner.tag in _FOCUSABLE and inner.attrs.get("tabindex") != "-1" and (inner.tag != "a" or "href" in inner.attrs):
                    fail("aria-hidden-focus", "Keyboard visitors can reach something their screen reader does not give "
                                                   "them. It is inside aria-hidden=\"true\" but can still be focused. "
                                                   "Remove aria-hidden, or add tabindex=\"-1\".", inner)
                    break
    # tables
    for t in by_tag.get("table", []):
        if t.attrs.get("role") in ("presentation", "none"):
            continue
        inner = list(t.walk())
        if any(x.tag == "td" for x in inner) and not any(x.tag == "th" for x in inner):
            fail("table-headers", "Screen readers cannot say which column or row each cell belongs to. This table "
                                  "has no header cells. Add <th> cells.", t, "warning")
    # autoplay
    for n in by_tag.get("audio", []) + by_tag.get("video", []):
        if "autoplay" in n.attrs:
            fail("autoplay", "Sound that starts by itself plays over visitors' screen reader speech. "
                            "Remove autoplay.", n)
    # weight
    size = len(html.encode("utf-8"))
    if size > 60_000:
        fail("page-weight", f"The page may load slowly on low-cost phones and small data plans. The HTML is "
                            f"{size // 1024} KB. Keep it under 60 KB.", severity="warning")
    # needs JavaScript
    body = (by_tag.get("body") or [root])[0]
    if by_tag.get("script") and len(body.text()) < 40:
        fail("needs-javascript", "Visitors may find an empty page if JavaScript does not run. Almost no text is in "
                                 "the HTML itself. Put the main content in the HTML.", severity="warning")

    report.passed = [RULES[r] for r in RULES if r not in failed]
    return report
