"""Visit every page reachable by links (signed in), and check each with the site's own accessibility checker.
New sections are covered automatically as long as they are linked from somewhere."""
import re
from urllib.parse import urljoin, urlparse

from studio.a11y_check import check_html

from .conftest import register

SKIP_PREFIXES = ("/static/", "/logout")
SKIP_SUFFIXES = (".json", ".zip", ".docx", ".md", ".txt", ".tex", ".html", ".csv")
HREF_RE = re.compile(r'href="([^"#]+)')


def crawl(client, start="/", limit=400):
    seen, queue, pages = set(), [start], {}
    while queue and len(seen) < limit:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        r = client.get(path)
        if "text/html" not in r.headers.get("content-type", ""):
            continue
        pages[path] = r
        for href in HREF_RE.findall(r.text):
            url = urljoin("http://testserver" + path, href)
            parsed = urlparse(url)
            if parsed.netloc != "testserver":
                continue
            p = parsed.path + (("?" + parsed.query) if parsed.query else "")
            if p.startswith(SKIP_PREFIXES) or parsed.path.endswith(SKIP_SUFFIXES) or "/preview" in parsed.path:
                continue
            if p not in seen:
                queue.append(p)
    return pages


def test_every_page_is_reachable_and_passes_checks(client):
    register(client)
    pages = crawl(client)
    assert len(pages) > 8
    problems = []
    for path, r in pages.items():
        if r.status_code != 200:
            problems.append(f"{path}: HTTP {r.status_code}")
            continue
        report = check_html(r.text, name=path)
        for f in report.errors:
            problems.append(f"{path}: {f.rule}: {f.message} {f.where}")
        h1s = re.findall(r"<h1[ >]", r.text)
        if len(h1s) != 1:
            problems.append(f"{path}: has {len(h1s)} h1 elements")
    assert not problems, "\n".join(problems)


def test_signed_out_pages_pass_checks(client):
    pages = crawl(client)
    problems = []
    for path, r in pages.items():
        if r.status_code != 200:
            continue
        for f in check_html(r.text, name=path).errors:
            problems.append(f"{path}: {f.rule}: {f.message} {f.where}")
    assert not problems, "\n".join(problems)
