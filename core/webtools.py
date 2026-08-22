import html as htmllib
import re
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JARVIS/1.0"}


def _get(url: str, timeout: int = 9) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, "ignore")


def html_to_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>|</pre>", "\n", page)
    text = re.sub(r"(?s)<[^>]+>", " ", page)
    text = htmllib.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def fetch_page(url: str, max_chars: int = 3500) -> str:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    raw = _get(url)
    title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    title = htmllib.unescape(title_m.group(1)).strip() if title_m else url
    body = html_to_text(raw)[:max_chars]
    return f"{title}\n\n{body}"


def extract_links(page_html: str) -> list[tuple[str, str]]:
    results = []
    for m in re.finditer(
        r'(?is)<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', page_html
    ):
        url = m.group(1)
        label = re.sub(r"(?s)<[^>]+>", "", m.group(2))
        label = htmllib.unescape(label).strip()
        if not label or len(label) < 4:
            continue
        results.append((label, url))
        if len(results) >= 12:
            break
    return results


def _search_bing_rss(query: str, max_results: int) -> list[dict]:
    raw = _get(f"https://www.bing.com/search?q={urllib.parse.quote_plus(query)}&format=rss")
    items = re.findall(r"(?is)<item>(.*?)</item>", raw)
    out = []
    for it in items:
        t = re.search(r"(?is)<title>(.*?)</title>", it)
        l = re.search(r"(?is)<link>(.*?)</link>", it)
        d = re.search(r"(?is)<description>(.*?)</description>", it)
        if not (t and l):
            continue
        url = htmllib.unescape(l.group(1)).strip()
        title = re.sub(r"(?s)<[^>]+>", "", htmllib.unescape(t.group(1))).strip()
        snippet = ""
        if d:
            snippet = re.sub(r"(?s)<[^>]+>", "", htmllib.unescape(d.group(1))).strip()
        out.append({"title": title[:110], "url": url, "snippet": snippet[:220]})
        if len(out) >= max_results:
            break
    return out


def _search_ddg_lite(query: str, max_results: int) -> list[dict]:
    raw = _get(f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote_plus(query)}")
    out = []
    for m in re.finditer(
        r'(?is)<a[^>]+href="[^"]*uddg=([^&"\']+)[^"]*"[^>]*>(.*?)</a>', raw
    ):
        url = urllib.parse.unquote(htmllib.unescape(m.group(1)))
        if "duckduckgo.com" in url:
            continue
        title = re.sub(r"(?s)<[^>]+>", "", htmllib.unescape(m.group(2))).strip()
        if len(title) < 4:
            continue
        out.append({"title": title[:110], "url": url, "snippet": ""})
        if len(out) >= max_results:
            break
    return out


def search_web(query: str, max_results: int = 5) -> list[dict]:
    last_error: Exception | None = None
    for engine in (_search_bing_rss, _search_ddg_lite):
        try:
            results = engine(query, max_results)
            if results:
                return results
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"All search engines failed ({last_error})")
