from __future__ import annotations

import html
import base64
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime

from opus.logutil import get_logger
from opus.net_policy import guarded_urlopen

log = get_logger()

# ---------------------------------------------------------------------------
# Google Custom Search API (primary — fast, no browser needed)
# ---------------------------------------------------------------------------

GOOGLE_API_KEY = "AIzaSyC2EXWYw0jk1buopgNyEGIPovMhkkVf6EQ"
GOOGLE_CX = "a732f878b443445af"


def _search_google_api(query: str, limit: int = 6) -> list[dict[str, str]]:
    """Use Google Custom Search JSON API."""
    if not GOOGLE_CX:
        return []
    params = urllib.parse.urlencode({
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CX,
        "q": query,
        "num": min(10, limit),
    })
    url = f"https://www.googleapis.com/customsearch/v1?{params}"
    try:
        req = urllib.request.Request(url)
        with guarded_urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        log.exception("Google API search failed")
        return []
    results: list[dict[str, str]] = []
    for item in (data.get("items") or [])[:limit]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        })
    return results


# ---------------------------------------------------------------------------
# Google search via headless Edge browser (fallback)
# ---------------------------------------------------------------------------

_EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
)


def _search_google_browser(query: str, limit: int = 6) -> list[dict[str, str]]:
    """Open headless Edge, search Google, parse results."""
    try:
        from selenium import webdriver
        from selenium.webdriver.edge.options import Options
    except ImportError:
        log.warning("selenium not installed, skipping google browser search")
        return []

    tmpdir = tempfile.mkdtemp(prefix="opus_edge_")
    try:
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument(f"user-agent={_EDGE_UA}")
        opts.add_argument(f"--user-data-dir={tmpdir}")

        driver = webdriver.Edge(options=opts)
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            })
            url = "https://www.google.com/search?q=" + urllib.parse.quote(query) + "&hl=en"
            driver.get(url)
            import time
            time.sleep(5)
            body = driver.page_source
        finally:
            driver.quit()
    except Exception:
        log.exception("google browser search failed")
        return []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return _parse_google_html(body, limit)


def _parse_google_html(body: str, limit: int = 6) -> list[dict[str, str]]:
    """Extract search results from Google HTML."""
    results: list[dict[str, str]] = []

    # Method 1: find <a> tags with data-ved (organic result links) followed by <h3>
    # Google wraps each result in a div; find cite elements for URLs paired with h3 titles
    h3_blocks = re.finditer(r'<h3[^>]*>(.*?)</h3>', body, re.S)
    for m in h3_blocks:
        title = re.sub(r"<[^>]+>", "", html.unescape(m.group(1))).strip()
        if not title:
            continue
        # Look backwards from h3 for nearest href
        before = body[max(0, m.start() - 2000):m.start()]
        href_m = re.findall(r'href="(https?://[^"]+)"', before)
        if not href_m:
            continue
        href = href_m[-1]
        if "google.com" in href or "accounts.google" in href:
            continue
        # Also look for <cite> URL which is the displayed URL
        cite_region = body[m.end():m.end() + 1000]
        cite_m = re.search(r'<cite[^>]*>(.*?)</cite>', cite_region, re.S)
        if cite_m:
            cite_url = re.sub(r"<[^>]+>", "", cite_m.group(1)).strip()
            if cite_url.startswith("http") and " " not in cite_url and "\u203a" not in cite_url:
                href = cite_url
        results.append({"url": href, "title": title, "snippet": ""})
        if len(results) >= limit:
            break

    # Method 2: look for snippet text near version numbers or key content
    # Extract any featured snippet / AI overview text
    # Look for data that mentions versions
    version_matches = re.findall(
        r'(?:Minecraft|Bedrock|Java)[^<]{0,80}(\d+\.\d+(?:\.\d+)?)',
        body, re.I
    )
    if version_matches and results:
        results[0]["snippet"] = f"Version mentioned: {version_matches[0]}"

    # Try to get snippet text from span elements near results
    snippets = re.findall(
        r'<span[^>]*>([^<]{50,300})</span>',
        body, re.S
    )
    snippet_idx = 0
    for r in results:
        if r.get("snippet"):
            continue
        while snippet_idx < len(snippets):
            s = snippets[snippet_idx].strip()
            snippet_idx += 1
            if any(kw in s.lower() for kw in _keyword_query(r["title"].lower()).split()):
                r["snippet"] = s[:200]
                break

    return results

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
RESULT_LINK_RE = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
RESULT_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div|span)>',
    re.IGNORECASE | re.DOTALL,
)
BING_BLOCK_RE = re.compile(r'<li class="b_algo".*?</li>', re.IGNORECASE | re.DOTALL)
BING_HREF_RE = re.compile(r'<a[^>]+href="([^"]+)"', re.IGNORECASE)
BING_TITLE_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
BING_SNIPPET_RE = re.compile(r'<p[^>]*class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL)
BING_SNIPPET_FALLBACK_RE = re.compile(r"<p>(.*?)</p>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"(?is)<script.*?>.*?</script>|<style.*?>.*?</style>")
WS_RE = re.compile(r"\s+")
FACT_RE = re.compile(
    r"\b(newest|latest|current|today|now|version|release|patch|price|winner|president|prime minister)\b",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "what",
    "whats",
    "is",
    "the",
    "in",
    "on",
    "for",
    "to",
    "of",
    "a",
    "an",
    "im",
    "i",
    "can",
    "get",
    "you",
    "me",
    "your",
    "my",
}


def _clean_html(text: str) -> str:
    cleaned = html.unescape(TAG_RE.sub(" ", text or ""))
    return WS_RE.sub(" ", cleaned).strip()


def _normalize_search_query(query: str) -> str:
    text = (query or "").strip().lower()
    text = text.replace("what's", "what is")
    text = re.sub(r"\bwhats\b", "what is", text)
    text = re.sub(r"\bim\b", "in", text)
    text = re.sub(r"[^a-z0-9\s.-]", " ", text)
    text = WS_RE.sub(" ", text).strip()
    return text


def _search_query_for_engine(query: str) -> str:
    text = _normalize_search_query(query)
    words = text.split()
    if len(words) >= 4:
        return '"' + text + '"'
    return text


def _fetch_text(url: str, timeout: float = 12.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with guarded_urlopen(request, timeout=timeout, web_read=True) as response:
        raw = response.read(1_500_000)
    charset = "utf-8"
    match = re.search(r'charset=["\']?([\w-]+)', raw[:2000].decode("latin-1", errors="ignore"), re.I)
    if match:
        charset = match.group(1)
    return raw.decode(charset, errors="replace")


def _resolve_ddg_url(url: str) -> str:
    if "uddg=" not in url:
        return url
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    target = params.get("uddg", [""])[0]
    return urllib.parse.unquote(target) or url


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _resolve_bing_url(url: str) -> str:
    url = html.unescape(url or "")
    if url.startswith("/"):
        url = urllib.parse.urljoin("https://www.bing.com", url)
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() != "www.bing.com":
        return url
    query = urllib.parse.parse_qs(parsed.query)
    payload = query.get("u", [""])[0]
    if payload.startswith("a1"):
        payload = payload[2:]
    if payload:
        try:
            padded = payload + "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="ignore")
            if decoded.startswith("http"):
                return decoded
        except Exception:
            pass
    return url


def _search_results(query: str, limit: int = 5) -> list[dict[str, str]]:
    needle = _normalize_search_query(query)
    if not needle:
        return []
    data = urllib.parse.urlencode({"q": needle}).encode("utf-8")
    request = urllib.request.Request(
        "https://html.duckduckgo.com/html/",
        data=data,
        headers={"User-Agent": USER_AGENT},
    )
    with guarded_urlopen(request, timeout=15) as response:
        body = response.read().decode("utf-8", errors="replace")
    links = RESULT_LINK_RE.findall(body)
    snippets = RESULT_SNIPPET_RE.findall(body)
    results: list[dict[str, str]] = []
    for index, (url, title_html) in enumerate(links[:limit]):
        snippet = _clean_html(snippets[index]) if index < len(snippets) else ""
        results.append(
            {
                "title": _clean_html(title_html),
                "url": _resolve_ddg_url(url),
                "snippet": snippet,
            }
        )
    return results


def _search_results_bing(query: str, limit: int = 5) -> list[dict[str, str]]:
    needle = _normalize_search_query(query)
    if not needle:
        return []
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(needle)
    body = _fetch_text(url, timeout=15.0)
    blocks = BING_BLOCK_RE.findall(body)
    results: list[dict[str, str]] = []
    for block in blocks:
        link_match = BING_HREF_RE.search(block)
        title_match = BING_TITLE_RE.search(block)
        if not link_match or not title_match:
            continue
        hit_url = _resolve_bing_url(link_match.group(1))
        title_html = title_match.group(1)
        snippet_match = BING_SNIPPET_RE.search(block) or BING_SNIPPET_FALLBACK_RE.search(block)
        snippet = _clean_html(snippet_match.group(1)) if snippet_match else ""
        results.append(
            {
                "title": _clean_html(title_html),
                "url": hit_url,
                "snippet": snippet,
            }
        )
        if len(results) >= limit:
            break
    return results


def _search_results_wikipedia(query: str, limit: int = 5) -> list[dict[str, str]]:
    needle = _normalize_search_query(query)
    if not needle:
        return []
    url = (
        "https://en.wikipedia.org/w/api.php?action=opensearch&format=json&limit="
        + str(max(1, min(8, limit)))
        + "&search="
        + urllib.parse.quote(needle)
    )
    body = _fetch_text(url, timeout=10.0)
    try:
        data = __import__("json").loads(body)
    except Exception:
        return []
    if not isinstance(data, list) or len(data) < 4:
        return []
    titles = data[1] if isinstance(data[1], list) else []
    snippets = data[2] if isinstance(data[2], list) else []
    urls = data[3] if isinstance(data[3], list) else []
    results: list[dict[str, str]] = []
    for i, title in enumerate(titles[:limit]):
        hit_url = urls[i] if i < len(urls) else ""
        snippet = snippets[i] if i < len(snippets) else ""
        if not isinstance(title, str) or not isinstance(hit_url, str):
            continue
        results.append({"title": title.strip(), "url": hit_url.strip(), "snippet": str(snippet).strip()})
    return results


def _search_mediawiki(api_url: str, query: str, limit: int = 5) -> list[dict[str, str]]:
    needle = _normalize_search_query(query)
    if not needle:
        return []
    url = (
        api_url
        + "?action=query&list=search&format=json&utf8=1&srlimit="
        + str(max(1, min(8, limit)))
        + "&srsearch="
        + urllib.parse.quote(needle)
    )
    body = _fetch_text(url, timeout=10.0)
    try:
        data = json.loads(body)
    except Exception:
        return []
    search = (data.get("query") or {}).get("search") or []
    results: list[dict[str, str]] = []
    for row in search[:limit]:
        title = str(row.get("title") or "").strip()
        snippet = _clean_html(str(row.get("snippet") or ""))
        if not title:
            continue
        base = api_url.replace("/api.php", "")
        link = base + "/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
        results.append({"title": title, "url": link, "snippet": snippet})
    return results


def _keyword_query(query: str) -> str:
    words = [w for w in WORD_RE.findall((query or "").lower()) if w not in STOPWORDS]
    if not words:
        return (query or "").strip()
    return " ".join(words[:6])


def _page_excerpt(url: str) -> str:
    try:
        text = _fetch_text(url, timeout=10.0)
    except Exception:
        log.exception("page fetch failed url=%s", url)
        return ""
    text = SCRIPT_RE.sub(" ", text)
    text = _clean_html(text)
    return text[:1800]


def _score_hit(query: str, hit: dict[str, str]) -> int:
    title = (hit.get("title") or "").lower()
    url = (hit.get("url") or "").lower()
    score = 0
    if any(token in title for token in ("latest", "newest", "version", "release", "update", "changelog", "patch")):
        score += 4
    if any(token in url for token in ("version", "release", "update", "changelog", "patch", "article")):
        score += 3
    if url.count("/") <= 3:
        score -= 1
    if "wikipedia.org" in url or "minecraft.wiki" in url or "minecraft.net" in url:
        score += 2
    q = (query or "").lower()
    domain = _domain(url)
    if "minecraft" in q and "minecraft" in title:
        score += 2
    if "minecraft" in q and "minecraft.net" in url:
        score += 6
    if "minecraft" in q and any(d in domain for d in ("minecraft.net", "minecraft.wiki", "fandom.com", "planetminecraft.com")):
        score += 8
    if "minecraft" in q and any(
        d in domain
        for d in (
            "dictionary.com",
            "merriam-webster.com",
            "dictionary.cambridge.org",
            "thefreedictionary.com",
            "wiktionary.org",
            "whatsapp.com",
        )
    ):
        score -= 10
    if ("ban" in q or "bannable" in q) and any(k in title for k in ("ban", "banned", "rules", "server", "policy")):
        score += 5
    return score


def _fresh_queries(query: str) -> list[str]:
    needle = _normalize_search_query(query)
    if not needle:
        return []
    options: list[str] = []
    short_q = _keyword_query(needle)
    if short_q and short_q != needle:
        options.append(short_q)
    options.append(needle)
    if FACT_RE.search(needle):
        options.append(f"{needle} official source")
        options.append(f"{needle} wikipedia")
    lowered = needle.lower()
    if "minecraft" in lowered and "version" in lowered:
        options.append("minecraft latest version minecraft.net")
        options.append("minecraft java edition latest release")
    if "minecraft" in lowered and ("ban" in lowered or "bannable" in lowered):
        options.insert(0, "minecraft banned reasons site:minecraft.net OR site:minecraft.wiki")
        options.insert(0, "minecraft ban rules")
        options.append("minecraft server rules ban reasons")
    # preserve order and uniqueness
    seen: set[str] = set()
    unique: list[str] = []
    for item in options:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _collect_results(query: str, limit: int) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    # Try Google via headless Edge first (best quality)
    try:
        google_hits = _search_google_browser(query, limit=limit)
        for item in google_hits:
            url = item.get("url") or ""
            if url.startswith("http") and url not in seen_urls:
                seen_urls.add(url)
                merged.append(item)
    except Exception:
        log.exception("google browser search failed")

    if len(merged) >= limit:
        merged.sort(key=lambda item: _score_hit(query, item), reverse=True)
        return merged[:limit]

    for q in _fresh_queries(query):
        batch: list[dict[str, str]] = []
        try:
            batch = _search_results_bing(q, limit=limit)
        except Exception:
            log.exception("bing batch failed query=%s", q)
        if not batch:
            try:
                batch = _search_results(q, limit=limit)
            except Exception:
                log.exception("duckduckgo batch failed query=%s", q)
        if not batch:
            try:
                batch = _search_results_wikipedia(q, limit=limit)
            except Exception:
                log.exception("wikipedia batch failed query=%s", q)
                continue
        for item in batch:
            url = item.get("url") or ""
            if not url.startswith("http") or url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(item)
            if len(merged) >= max(limit * 2, limit + 4):
                break
        if len(merged) >= max(limit * 2, limit + 4):
            break
    merged.sort(key=lambda item: _score_hit(query, item), reverse=True)
    return merged[:limit]


def search_web(query: str, limit: int = 5) -> str:
    needle = _normalize_search_query(query)
    if not needle:
        return "No search query given."
    try:
        hits = _collect_results(needle, limit=max(3, min(10, int(limit or 6))))
    except Exception as exc:
        log.exception("web search failed")
        return f"Web search failed: {exc}"
    if not hits:
        try:
            hits = _search_results_bing(needle, limit=max(3, min(8, int(limit or 6))))
        except Exception:
            try:
                hits = _search_results(needle, limit=max(3, min(8, int(limit or 6))))
            except Exception:
                hits = []
    if not hits:
        short_q = _keyword_query(needle)
        sources: list[dict[str, str]] = []
        try:
            sources.extend(_search_results_wikipedia(short_q, limit=4))
        except Exception:
            pass
        if "minecraft" in needle.lower():
            try:
                sources.extend(_search_mediawiki("https://minecraft.wiki/api.php", short_q, limit=5))
            except Exception:
                pass
        if sources:
            dedup: list[dict[str, str]] = []
            seen: set[str] = set()
            for row in sources:
                u = row.get("url") or ""
                if not u or u in seen:
                    continue
                seen.add(u)
                dedup.append(row)
            hits = dedup[: max(3, min(8, int(limit or 6)))]
    if hits and "minecraft" in needle:
        has_minecraft_source = any("minecraft" in _domain((row.get("url") or "")) for row in hits)
        if not has_minecraft_source:
            extra: list[dict[str, str]] = []
            short_q = _keyword_query(needle)
            try:
                extra.extend(_search_mediawiki("https://minecraft.wiki/api.php", short_q, limit=5))
            except Exception:
                pass
            if extra:
                merged = extra + hits
                dedup: list[dict[str, str]] = []
                seen_urls: set[str] = set()
                for row in merged:
                    url = row.get("url") or ""
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    dedup.append(row)
                hits = dedup[: max(3, min(10, int(limit or 6)))]
    if not hits:
        q = urllib.parse.quote(needle)
        return "\n".join(
            [
                f"Web lookup for: {needle}",
                "I could not parse results this attempt. Try one of these direct search links:",
                f"- Google: https://www.google.com/search?q={q}",
                f"- Bing: https://www.bing.com/search?q={q}",
                f"- DuckDuckGo: https://duckduckgo.com/?q={q}",
            ]
        )

    lines = [f"Web search for: {needle}", f"As of: {datetime.now().strftime('%Y-%m-%d %H:%M %Z')}"]
    for index, hit in enumerate(hits, start=1):
        lines.append(f"{index}. {hit['title']}")
        if hit["snippet"]:
            lines.append(f"   {hit['snippet']}")
        lines.append(f"   {hit['url']}")

    # Pull text from several distinct domains so answers are less likely to rely on one stale page.
    lines.append("")
    lines.append("Source excerpts:")
    used_domains: set[str] = set()
    excerpt_count = 0
    for hit in hits:
        url = hit.get("url", "")
        dom = _domain(url)
        if not url.startswith("http") or not dom or dom in used_domains:
            continue
        excerpt = _page_excerpt(url)
        if not excerpt:
            continue
        used_domains.add(dom)
        excerpt_count += 1
        lines.append(f"- {hit['title']} ({dom})")
        lines.append(excerpt)
        if excerpt_count >= 3:
            break
    return "\n".join(lines)


def lookup_context(query: str, limit: int = 4) -> str:
    needle = (query or "").strip()
    if len(needle) < 3:
        return ""
    try:
        return search_web(needle, limit=limit)
    except Exception:
        log.exception("lookup context failed")
        return ""
