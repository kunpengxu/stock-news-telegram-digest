import json
import os
import re
import sys
import textwrap
import time
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, Tag


NEWSFILTER_URL = "https://newsfilter.io/Home"
NEWSFILTER_API_ENDPOINT = "https://api.newsfilter.io/search"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_ZHIPU_MODEL = "glm-4-flash"
DEFAULT_LLM_PROVIDER = "gemini"
DEFAULT_MAX_BULLETS_PER_SECTOR = 20
TELEGRAM_CHUNK_LIMIT = 3900
DEFAULT_SEND_FALLBACK_ON_ERROR = False
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_ZHIPU_TIMEOUT_SECONDS = 120
DEFAULT_LLM_MAX_BULLET_CHARS = 320
DEFAULT_ENFORCE_EXACT_COUNTS = True
GEMINI_FALLBACK_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]
ZHIPU_FALLBACK_MODELS = [
    "glm-4-flash",
    "glm-4-air",
]

HighlightMap = Dict[str, List[str]]


CATEGORY_LABELS = {
    "tech": "科技 Tech",
    "technology": "科技 Tech",
    "healthcare": "医疗 Healthcare",
    "consumer": "消费 Consumer",
    "finance": "金融 Finance",
    "financial": "金融 Finance",
    "industrials & materials": "工业与材料 Industrials & Materials",
    "industrials and materials": "工业与材料 Industrials & Materials",
    "energy & utilities": "能源与公用事业 Energy & Utilities",
    "energy and utilities": "能源与公用事业 Energy & Utilities",
    "healthcare/consumer crossover": "医疗/消费交叉 Healthcare/Consumer crossover",
    "financial services & market infrastructure": "金融服务与市场基础设施 Financial Services & Market Infrastructure",
    "financial markets & macro": "金融市场与宏观 Financial Markets & Macro",
    "financial markets and macro": "金融市场与宏观 Financial Markets & Macro",
    "media, telecom & internet": "媒体、电信与互联网 Media, Telecom & Internet",
    "media telecom & internet": "媒体、电信与互联网 Media, Telecom & Internet",
    "media, telecom and internet": "媒体、电信与互联网 Media, Telecom & Internet",
    "consumer (retail, travel, leisure & food)": "消费 Consumer",
    "energy-related industrials & utilities": "能源相关工业与公用事业 Energy-Related Industrials & Utilities",
    "energy-related industrials and utilities": "能源相关工业与公用事业 Energy-Related Industrials & Utilities",
    "other": "其他 Other",
}

KNOWN_CATEGORIES = [
    "Tech",
    "Healthcare",
    "Consumer",
    "Finance",
    "Industrials & Materials",
    "Energy & Utilities",
    "Healthcare/Consumer crossover",
    "Financial Services & Market Infrastructure",
    "Consumer (Retail, Travel, Leisure & Food)",
    "Financial Markets & Macro",
    "Media, Telecom & Internet",
    "Energy-Related Industrials & Utilities",
    "Real Estate",
    "Media & Entertainment",
    "Autos & Transportation",
    "Retail & E-Commerce",
    "Retail & E‑Commerce",
    "Banks & Payments",
    "Macro & Markets",
    "Other",
]


def log(message: str) -> None:
    print(f"[newsfilter] {message}", flush=True)


def load_local_env(env_file: str = ".env.local") -> None:
    if not os.path.exists(env_file):
        return
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as exc:
        log(f"Warning: failed to load {env_file}: {exc}")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def is_access_denied_text(value: str) -> bool:
    lowered = normalize_space(value).lower()
    markers = [
        "access denied",
        "forbidden",
        "blocked",
        "request blocked",
        "bot detected",
        "please contact support@newsfilter.io",
    ]
    return any(marker in lowered for marker in markers)


def clean_bullet(value: str) -> str:
    value = normalize_space(value)
    return re.sub(r"^[•\-\*\u2022\s]+", "", value).strip()


def normalize_category_key(value: str) -> str:
    value = normalize_space(value).lower()
    value = value.replace("‑", "-")
    value = re.sub(r"\s+", " ", value)
    return value


def canonical_category(value: str) -> str:
    value = normalize_space(value)
    lowered = normalize_category_key(value)
    return CATEGORY_LABELS.get(lowered, value or "Other")


def dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        normalized = normalize_space(item)
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def merge_highlights(highlights: HighlightMap) -> HighlightMap:
    merged: HighlightMap = {}
    for category, bullets in highlights.items():
        clean_category = canonical_category(category)
        clean_items = dedupe(clean_bullet(item) for item in bullets if clean_bullet(item))
        if clean_items:
            merged.setdefault(clean_category, [])
            merged[clean_category] = dedupe([*merged[clean_category], *clean_items])
    return merged


def shorten_text(value: str, max_chars: int) -> str:
    value = normalize_space(value)
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def compact_highlights_for_llm(highlights: HighlightMap) -> HighlightMap:
    max_chars = int(os.getenv("LLM_MAX_BULLET_CHARS", DEFAULT_LLM_MAX_BULLET_CHARS))
    compacted: HighlightMap = {}
    for category, bullets in highlights.items():
        compacted[category] = [shorten_text(bullet, max_chars) for bullet in bullets]
    return compacted


def strip_button_label(value: str) -> str:
    return re.sub(r"^\[?Button:?\]?\s*", "", normalize_space(value), flags=re.I).strip()


def is_category_label(value: str) -> bool:
    cleaned = strip_button_label(value)
    return any(normalize_category_key(cleaned) == normalize_category_key(category) for category in KNOWN_CATEGORIES)


def first_category_in_text(value: str) -> Optional[str]:
    normalized = normalize_category_key(strip_button_label(value))
    for category in sorted(KNOWN_CATEGORIES, key=len, reverse=True):
        if normalize_category_key(category) in normalized:
            return category
    return None


def is_noise_line(value: str) -> bool:
    value = normalize_space(value)
    lowered = value.lower()
    if not value:
        return True
    if lowered in {"highlights", "latest news", "analyst ratings", "spacs & ipos", "fda approvals"}:
        return True
    if is_category_label(value):
        return True
    if re.fullmatch(r"(?:\[?button:?\]?\s*)?(?:" + "|".join(re.escape(c) for c in KNOWN_CATEGORIES) + r")", value, re.I):
        return True
    return False


def likely_news_line(value: str) -> bool:
    value = clean_bullet(value)
    if is_noise_line(value):
        return False
    if len(value) < 40:
        return False
    signals = [
        "$",
        "%",
        "IPO",
        "FDA",
        "AI",
        "stock",
        "shares",
        "revenue",
        "market",
        "deal",
        "merger",
        "acquisition",
        "earnings",
        "sales",
        "guidance",
        "announced",
        "reported",
        "launched",
        "filed",
        "won",
        "surged",
        "fell",
        "rose",
    ]
    return any(signal.lower() in value.lower() for signal in signals) or len(value) >= 100


def find_highlights_container(soup: BeautifulSoup) -> Tag:
    highlights_text = soup.find(string=re.compile(r"\bHighlights\b", re.I))
    if not highlights_text:
        return soup.body or soup

    element = highlights_text.parent if isinstance(highlights_text.parent, Tag) else soup
    best = element
    best_score = -1
    current = element

    for _ in range(8):
        if not isinstance(current, Tag):
            break
        score = len(current.find_all("li")) * 3
        score += len(current.find_all(["button", "a"], string=True))
        score += len(current.get_text(" ", strip=True))
        if score > best_score:
            best = current
            best_score = score
        if current.name in {"section", "main", "article"} and len(current.find_all("li")):
            return current
        current = current.parent

    return best


def extract_from_tab_panels(container: Tag) -> HighlightMap:
    highlights: HighlightMap = {}

    for tab in container.find_all(attrs={"role": re.compile("^tab$", re.I)}):
        category = normalize_space(tab.get_text(" ", strip=True))
        panel_id = tab.get("aria-controls")
        if not category or not panel_id:
            continue
        panel = container.find(id=panel_id)
        if not panel:
            continue
        bullets = [clean_bullet(li.get_text(" ", strip=True)) for li in panel.find_all("li")]
        if bullets:
            highlights[category] = bullets

    return merge_highlights(highlights)


def extract_from_headed_lists(container: Tag) -> HighlightMap:
    highlights: HighlightMap = {}
    heading_tags = ["h2", "h3", "h4", "h5", "button"]

    for heading in container.find_all(heading_tags):
        category = normalize_space(heading.get_text(" ", strip=True))
        if not category or category.lower() == "highlights":
            continue
        bullets: List[str] = []
        sibling = heading.find_next_sibling()
        while sibling and isinstance(sibling, Tag):
            if sibling.name in heading_tags and normalize_space(sibling.get_text(" ", strip=True)):
                break
            bullets.extend(clean_bullet(li.get_text(" ", strip=True)) for li in sibling.find_all("li"))
            if sibling.name == "ul":
                bullets.extend(clean_bullet(li.get_text(" ", strip=True)) for li in sibling.find_all("li", recursive=False))
            sibling = sibling.find_next_sibling()
        if bullets:
            highlights[category] = bullets

    return merge_highlights(highlights)


def extract_json_like_highlights(soup: BeautifulSoup) -> HighlightMap:
    highlights: HighlightMap = {}
    text = soup.get_text("\n", strip=True)
    known_categories = [
        "Tech",
        "Healthcare",
        "Consumer",
        "Finance",
        "Industrials & Materials",
        "Energy & Utilities",
        "Healthcare/Consumer crossover",
        "Financial Services & Market Infrastructure",
        "Other",
    ]

    for category in known_categories:
        pattern = re.compile(
            rf"{re.escape(category)}\s*(?:\n|:)\s*((?:[-•*]\s*.+(?:\n|$))+)",
            re.I,
        )
        match = pattern.search(text)
        if not match:
            continue
        bullets = [clean_bullet(line) for line in match.group(1).splitlines()]
        if bullets:
            highlights[category] = bullets

    return merge_highlights(highlights)


def split_compact_button_lines(line: str) -> List[str]:
    matches = re.findall(r"\[Button:\s*([^\]]+)\]", line, flags=re.I)
    if matches:
        return [normalize_space(match) for match in matches]
    return [line]


def extract_from_plain_text(text: str, active_category: Optional[str] = None) -> HighlightMap:
    raw_lines: List[str] = []
    for line in text.splitlines():
        for split_line in split_compact_button_lines(line):
            cleaned = normalize_space(split_line)
            if cleaned:
                raw_lines.append(cleaned)

    highlights_index = next((idx for idx, line in enumerate(raw_lines) if line.lower() == "highlights"), -1)
    if highlights_index < 0:
        return {}

    highlights: HighlightMap = {}
    current_category = active_category
    first_seen_category = active_category
    bullets_seen = False
    started = False

    for line in raw_lines[highlights_index + 1 :]:
        line = normalize_space(line)
        if not line:
            continue

        category = first_category_in_text(line)
        if category and (
            is_category_label(line)
            or line.lower().startswith("[button:")
            or len(strip_button_label(line)) <= 140
        ):
            if not first_seen_category:
                first_seen_category = category
            if not active_category:
                current_category = category
            started = True
            if current_category:
                highlights.setdefault(current_category, [])
            continue

        if not started and not active_category:
            continue

        if re.match(r"^(latest news|market news|most recent|watchlist|sign in|log in)\b", line, re.I):
            break

        bullet = clean_bullet(line)
        if current_category and likely_news_line(bullet):
            if not bullets_seen and not active_category and first_seen_category:
                current_category = first_seen_category
            highlights.setdefault(current_category, []).append(bullet)
            bullets_seen = True

    return merge_highlights(highlights)


def parse_highlights(html: str) -> HighlightMap:
    soup = BeautifulSoup(html, "html.parser")
    for noisy in soup(["script", "style", "noscript", "svg"]):
        noisy.decompose()

    container = find_highlights_container(soup)

    for extractor in (extract_from_tab_panels, extract_from_headed_lists, extract_json_like_highlights):
        highlights = extractor(container if extractor != extract_json_like_highlights else soup)
        if highlights:
            return highlights

    plain_text_highlights = extract_from_plain_text(soup.get_text("\n", strip=True))
    if plain_text_highlights:
        return plain_text_highlights

    bullets = [clean_bullet(li.get_text(" ", strip=True)) for li in container.find_all("li")]
    return merge_highlights({"Other": bullets}) if bullets else {}


def fetch_static_html() -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        )
    }
    response = requests.get(NEWSFILTER_URL, headers=headers, timeout=30)
    body_sample = normalize_space(response.text)[:600]
    if response.status_code == 403 or is_access_denied_text(body_sample):
        raise RuntimeError(
            f"Newsfilter blocked static access (status={response.status_code}). "
            "This runner IP is likely denied."
        )
    response.raise_for_status()
    return response.text


def map_api_sector_to_category(sectors: List[str], industries: List[str]) -> str:
    joined = " | ".join(sectors + industries)
    lower = joined.lower()
    if "health care" in lower or "healthcare" in lower:
        if "consumer" in lower:
            return "Healthcare/Consumer crossover"
        return "Healthcare"
    if "technology" in lower or "information technology" in lower or "communication services" in lower:
        return "Tech"
    if "consumer" in lower:
        return "Consumer"
    if "financial" in lower or "bank" in lower or "insurance" in lower or "capital markets" in lower:
        return "Finance"
    if "industrial" in lower or "material" in lower or "basic materials" in lower:
        return "Industrials & Materials"
    if "energy" in lower or "utilities" in lower:
        return "Energy & Utilities"
    return "Other"


def build_api_bullet(article: dict) -> str:
    title = normalize_space(article.get("title", ""))
    desc = normalize_space(article.get("description", ""))
    source = normalize_space((article.get("source") or {}).get("name", ""))
    published_at = normalize_space(article.get("publishedAt", ""))

    if not title:
        return ""

    if desc and desc.lower() not in title.lower():
        core = f"{title} — {desc}"
    else:
        core = title

    if source and published_at:
        return f"{core} ({source}, {published_at})"
    if source:
        return f"{core} ({source})"
    return core


def fetch_highlights_via_api(max_bullets_per_sector: int) -> HighlightMap:
    api_key = os.getenv("NEWSFILTER_API_KEY")
    if not api_key:
        return {}

    lookback_hours = int(os.getenv("NEWSFILTER_LOOKBACK_HOURS", DEFAULT_LOOKBACK_HOURS))
    query = f"publishedAt:[now-{lookback_hours}h TO *]"
    payload = {
        "queryString": query,
        "size": 50,
        "from": 0,
        "sort": [{"publishedAt": {"order": "desc"}}],
    }
    headers = {"Authorization": api_key}

    log(f"Fetching Newsfilter Query API ({lookback_hours}h lookback).")
    try:
        response = requests.post(
            NEWSFILTER_API_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"Newsfilter Query API request failed: {exc}") from exc

    articles = data.get("articles") or []
    if not articles:
        raise RuntimeError("Newsfilter Query API returned no articles.")

    grouped: HighlightMap = {}
    for article in articles:
        sector = map_api_sector_to_category(article.get("sectors") or [], article.get("industries") or [])
        bullet = build_api_bullet(article)
        if not bullet:
            continue
        grouped.setdefault(sector, []).append(bullet)

    merged = merge_highlights(grouped)
    return {k: v[:max_bullets_per_sector] for k, v in merged.items()}


def fetch_highlights_with_playwright(max_bullets_per_sector: int) -> HighlightMap:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run: python -m playwright install chromium") from exc

    log("Static extraction was empty; trying Playwright-rendered page.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        page = browser.new_page(
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """
        )
        try:
            response = page.goto(NEWSFILTER_URL, wait_until="load", timeout=60000)
            if response:
                log(f"Playwright response status: {response.status}")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                log("Network idle timed out; continuing with the loaded DOM.")
            try:
                page.wait_for_selector("text=Highlights", timeout=15000)
            except PlaywrightTimeoutError:
                log("Timed out waiting for Highlights text.")

            js = """
            () => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const selectors = 'button,[role="tab"],[aria-controls],a,span,div';
              const known = [
                'Tech',
                'Healthcare',
                'Consumer',
                'Finance',
                'Industrials & Materials',
                'Energy & Utilities',
                'Healthcare/Consumer crossover (Healthcare already above; no duplication.)',
                'Financial Services & Market Infrastructure (subset of Finance already captured; no duplication.)',
                'Healthcare/Consumer crossover',
                'Financial Services & Market Infrastructure',
                'Other'
              ];
              return [...document.querySelectorAll(selectors)]
                .filter((el) => {
                  const r = el.getBoundingClientRect();
                  const text = norm(el.textContent);
                  return r.width > 0 && r.height > 0 && known.includes(text);
                })
                .map((el) => norm(el.textContent));
            }
            """
            tab_texts = dedupe(page.evaluate(js))
            tabs = [text for text in tab_texts if first_category_in_text(text)]
            if not tabs:
                body_text = page.locator("body").inner_text(timeout=10000)
                tabs = [
                    category
                    for category in KNOWN_CATEGORIES
                    if re.search(rf"\b{re.escape(category)}\b", body_text, re.I)
                ]
            log(f"Playwright visible category tabs: {', '.join(tabs) if tabs else 'none'}")
            highlights: HighlightMap = {}

            if tabs:
                for index, label in enumerate(tabs):
                    category_name = first_category_in_text(label) or label
                    clicked = False
                    try:
                        page.get_by_text(label, exact=True).first.click(timeout=5000)
                        page.wait_for_timeout(600)
                        clicked = True
                    except Exception as exc:
                        log(f"Could not click category tab '{label}': {exc}")
                    if not clicked and index > 0:
                        continue
                    bullets = page.evaluate(
                        """
                        () => {
                          const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                          const isVisible = (el) => {
                            const r = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                          };

                          const heading = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span')]
                            .find((el) => /\\bHighlights\\b/i.test(norm(el.textContent || '')));
                          let root = heading || document.body;
                          for (let i = 0; i < 8 && root.parentElement; i++) {
                            if (root.querySelectorAll('li').length >= 3) break;
                            root = root.parentElement;
                          }

                          const lines = [...root.querySelectorAll('li, p')]
                            .filter(isVisible)
                            .map((el) => norm(el.textContent))
                            .filter((t) => t && t.length > 30);

                          return [...new Set(lines)];
                        }
                        """
                    )

                    clean_bullets = dedupe(clean_bullet(item) for item in bullets if clean_bullet(item))
                    if clean_bullets:
                        highlights[category_name] = clean_bullets[:max_bullets_per_sector]
                        log(
                            f"Category '{label}' yielded {len(clean_bullets)} bullet(s); sample: "
                            f"{clean_bullets[0][:120]}"
                        )
                    else:
                        log(f"Category '{label}' yielded 0 bullet(s).")
            else:
                title = page.title()
                current_url = page.url
                body_text = page.locator("body").inner_text(timeout=10000)
                sample = normalize_space(body_text)[:600]
                log(f"Playwright page title: {title}")
                log(f"Playwright final URL: {current_url}")
                log(f"Playwright body sample: {sample}")
                if response and response.status == 403:
                    raise RuntimeError(
                        "Newsfilter blocked Playwright access (HTTP 403). "
                        "GitHub-hosted runner IP appears denied."
                    )
                if is_access_denied_text(sample):
                    raise RuntimeError(
                        "Newsfilter returned an access-denied page to Playwright. "
                        "GitHub-hosted runner IP appears denied."
                    )
                html = page.content()
                highlights = parse_highlights(html)

            merged = merge_highlights(highlights)
            signatures = {}
            for category, bullets in merged.items():
                signatures[category] = " | ".join(bullets[:2]).lower()
            unique_signatures = {v for v in signatures.values() if v}
            if merged and len(unique_signatures) <= 2 and len(merged) >= 4:
                log("Warning: many categories share near-identical bullets; tab switching may not be effective.")
            return merged
        finally:
            browser.close()


def fetch_highlights() -> HighlightMap:
    max_bullets = int(os.getenv("MAX_BULLETS_PER_SECTOR", DEFAULT_MAX_BULLETS_PER_SECTOR))
    api_key = os.getenv("NEWSFILTER_API_KEY")

    if api_key:
        highlights = fetch_highlights_via_api(max_bullets)
        if highlights:
            log(f"Extracted {sum(len(v) for v in highlights.values())} bullets from Newsfilter Query API.")
            return highlights

    try:
        log(f"Fetching {NEWSFILTER_URL}")
        html = fetch_static_html()
        highlights = parse_highlights(html)
        if highlights:
            log(f"Extracted {sum(len(v) for v in highlights.values())} bullets from static HTML.")
            return {k: v[:max_bullets] for k, v in highlights.items()}
        log("No highlights found in static HTML.")
    except Exception as exc:
        log(f"Static page fetch or parse failed: {exc}")

    highlights = fetch_highlights_with_playwright(max_bullets)
    if highlights:
        log(f"Extracted {sum(len(v) for v in highlights.values())} bullets with Playwright.")
    return {k: v[:max_bullets] for k, v in highlights.items()}


def build_gemini_prompt(highlights: HighlightMap) -> str:
    today = date.today().isoformat()
    payload = json.dumps(highlights, ensure_ascii=False, indent=2)
    required_sections = [canonical_category(category) for category in highlights.keys() if highlights.get(category)]
    required_section_text = "\n".join(f"- 【{section}】" for section in required_sections) if required_sections else "- 【其他 Other】"
    required_counts_text = "\n".join(
        f"- 【{canonical_category(category)}】: {len(bullets)} 条"
        for category, bullets in highlights.items()
        if bullets
    )
    return textwrap.dedent(
        f"""
        你是一个美股市场新闻摘要助手。请只基于下面 Newsfilter 首页 Highlights 抓取到的英文内容，生成 Telegram 友好的中文纯文本摘要。

        要求：
        - 不要编造事实，不要加入 Highlights 中没有的信息。
        - 翻译并压缩英文要点，语言简洁，适合盘后市场监控。
        - 保留公司名、ticker、金额、百分比、日期和重要数字的原文形式。
        - 不提供买入、卖出、持有等投资建议。
        - 按原始 sector/category 分组；没有内容的分类不要输出。
        - 必须覆盖“输入 JSON 中所有有内容的分类”，不得只输出单一分类。
        - 下方“必须输出的分类标题”里每个标题都要出现。
        - 每个分类输出条数必须严格等于“必须输出的分类与条数”中的数量，逐条对应，不得合并、删减或新增。
        - 顶部输出 3-5 条跨领域「今日重点」。
        - 末尾输出「值得关注」，列出提到的公司/股票和主题；如果无法识别则写“未明确提及”。
        - 只输出最终消息，不要解释过程。

        必须输出的分类标题（逐一输出，不可遗漏）：
        {required_section_text}

        必须输出的分类与条数（严格一致）：
        {required_counts_text}

        输出格式：
        📌 Newsfilter 美股新闻精选 | {today}

        【今日重点】
        1. ...
        2. ...
        3. ...

        【分领域摘要】

        【科技 Tech】
        - ...

        【值得关注】
        - 公司/股票：...
        - 主题：...

        原始 Highlights JSON：
        {payload}
        """
    ).strip()


def build_exact_count_prompt(highlights: HighlightMap) -> str:
    today = date.today().isoformat()
    payload = json.dumps(highlights, ensure_ascii=False, indent=2)
    required_counts_text = "\n".join(
        f"- 【{canonical_category(category)}】: {len(bullets)} 条"
        for category, bullets in highlights.items()
        if bullets
    )
    return textwrap.dedent(
        f"""
        你是美股新闻翻译助手。请把输入 JSON 中每一条英文 bullet 逐条翻译成中文。

        强制规则（必须遵守）：
        - 每个分类的输出条数必须与输入完全一致。
        - 一条输入 bullet 只能对应一条输出 bullet，禁止合并、拆分、删减、补充。
        - 保留公司名、ticker、金额、百分比、日期和关键数字原样。
        - 不要给投资建议。
        - 输出 Telegram 纯文本。

        输出格式：
        📌 Newsfilter 美股新闻精选 | {today}

        【今日重点】
        1. ...
        2. ...
        3. ...

        【分领域摘要】
        （按下列分类顺序输出）

        必须输出的分类与条数：
        {required_counts_text}

        原始 Highlights JSON：
        {payload}
        """
    ).strip()


def parse_section_bullet_counts(message: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    current: Optional[str] = None
    for raw in message.splitlines():
        line = raw.strip()
        header_match = re.match(r"^【([^】]+)】$", line)
        if header_match:
            current = header_match.group(1).strip()
            counts.setdefault(current, 0)
            continue
        if current and line.startswith("-"):
            counts[current] = counts.get(current, 0) + 1
    return counts


def has_expected_section_counts(message: str, highlights: HighlightMap) -> bool:
    parsed = parse_section_bullet_counts(message)
    for category, bullets in highlights.items():
        if not bullets:
            continue
        section = canonical_category(category)
        if parsed.get(section, 0) < len(bullets):
            return False
    return True


def build_sector_translation_prompt(category: str, bullets: List[str]) -> str:
    payload = json.dumps(bullets, ensure_ascii=False, indent=2)
    return textwrap.dedent(
        f"""
        你是财经新闻翻译助手。请将下面这个分类的英文 bullet 列表逐条翻译成中文。

        强制规则：
        - 输出条数必须与输入完全一致（{len(bullets)} 条）。
        - 一条输入对应一条输出，禁止合并、拆分、删减、新增。
        - 保留公司名、ticker、金额、百分比、日期和关键数字原样。
        - 只输出 JSON 数组，不要输出 Markdown、标题、解释文字或代码块。
        - JSON 数组长度必须是 {len(bullets)}。
        - JSON 数组每个元素是对应的中文字符串。

        分类：{category}
        输入 bullets JSON：
        {payload}
        """
    ).strip()


def parse_bullet_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("-"):
            line = clean_bullet(line)
            if line:
                lines.append(line)
    return lines


def extract_json_array(text: str) -> Optional[List[str]]:
    text = (text or "").strip()
    if not text:
        return None
    candidates = [text]
    block_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if block_match:
        candidates.insert(0, block_match.group(1).strip())
    bracket_match = re.search(r"\[.*\]", text, re.S)
    if bracket_match:
        candidates.append(bracket_match.group(0).strip())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                parsed = [normalize_space(str(item)) for item in data if normalize_space(str(item))]
                return parsed
        except Exception:
            continue
    return None


def filter_instruction_like_lines(lines: List[str]) -> List[str]:
    blocked = [
        "输出条数必须",
        "一条输入对应",
        "禁止合并",
        "保留公司名",
        "每行以",
        "不要输出任何解释",
        "只输出",
        "json 数组",
        "必须遵守",
        "强制规则",
    ]
    clean: List[str] = []
    for line in lines:
        lowered = normalize_space(line).lower()
        if any(token in lowered for token in blocked):
            continue
        clean.append(line)
    return clean


def translate_sector_with_zhipu(category: str, bullets: List[str]) -> List[str]:
    api_key = os.getenv("ZHIPU_API_KEY")
    if not api_key:
        raise RuntimeError("Missing required environment variable: ZHIPU_API_KEY")

    model = os.getenv("ZHIPU_MODEL", DEFAULT_ZHIPU_MODEL)
    timeout_seconds = int(os.getenv("ZHIPU_TIMEOUT_SECONDS", DEFAULT_ZHIPU_TIMEOUT_SECONDS))
    endpoint = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    prompt = build_sector_translation_prompt(category, bullets)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个严谨的财经翻译助手。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }

    for attempt in range(1, 4):
        try:
            response = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            text = ((((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
            translated = extract_json_array(text)
            if translated is None:
                translated = parse_bullet_lines(text)
            translated = filter_instruction_like_lines(translated)
            if len(translated) >= len(bullets):
                return translated[: len(bullets)]
            if translated:
                padded = translated + bullets[len(translated) :]
                return padded[: len(bullets)]
            raise RuntimeError("Empty translation output.")
        except requests.Timeout:
            if attempt < 3:
                wait_s = attempt * 2
                log(f"Zhipu timeout translating {category} attempt {attempt}; retrying in {wait_s}s.")
                time.sleep(wait_s)
                continue
            raise

    raise RuntimeError(f"Failed to translate category: {category}")


def build_message_from_translated_sections(translated: HighlightMap) -> str:
    today = date.today().isoformat()
    sections = [f"📌 Newsfilter 美股新闻精选 | {today}", "", "【今日重点】"]
    top_lines: List[str] = []
    for bullets in translated.values():
        for bullet in bullets:
            if bullet:
                top_lines.append(bullet)
            if len(top_lines) >= 5:
                break
        if len(top_lines) >= 5:
            break
    if not top_lines:
        top_lines = ["今日暂无可用重点。"]
    for idx, line in enumerate(top_lines[:5], start=1):
        sections.append(f"{idx}. {line}")

    sections.extend(["", "【分领域摘要】", ""])
    for category, bullets in translated.items():
        if not bullets:
            continue
        sections.append(f"【{category}】")
        for bullet in bullets:
            sections.append(f"- {bullet}")
        sections.append("")

    sections.append("【值得关注】")
    sections.append("- 公司/股票：见分领域摘要")
    sections.append("- 主题：见分领域摘要")
    return "\n".join(sections).strip()


def regenerate_exact_by_sector(highlights: HighlightMap) -> str:
    provider = os.getenv("LLM_PROVIDER", DEFAULT_LLM_PROVIDER).strip().lower()
    if provider != "zhipu":
        return summarize_with_llm(highlights, exact_counts=True)

    translated: HighlightMap = {}
    for category, bullets in highlights.items():
        if not bullets:
            continue
        log(f"Strict mode: translating category '{canonical_category(category)}' ({len(bullets)} bullets).")
        translated[canonical_category(category)] = translate_sector_with_zhipu(canonical_category(category), bullets)

    return build_message_from_translated_sections(translated)


def summarize_with_gemini(highlights: HighlightMap, exact_counts: bool = False) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing required environment variable: GEMINI_API_KEY")

    model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    compacted = compact_highlights_for_llm(highlights)
    prompt = build_exact_count_prompt(compacted) if exact_counts else build_gemini_prompt(compacted)
    log(f"Calling Gemini model: {model}")

    from google import genai

    client = genai.Client(api_key=api_key)
    tried = set()
    candidates = [model, *GEMINI_FALLBACK_MODELS]
    last_error = None

    for candidate in candidates:
        if candidate in tried:
            continue
        tried.add(candidate)
        try:
            if candidate != model:
                log(f"Retrying with fallback Gemini model: {candidate}")
            response = client.models.generate_content(model=candidate, contents=prompt)
            text = (response.text or "").strip()
            if text:
                return text
            last_error = RuntimeError(f"Gemini model {candidate} returned empty response.")
        except Exception as exc:
            last_error = exc
            err = str(exc).lower()
            # Retry next model when this model is unavailable or quota-exhausted.
            if (
                "404" in err
                or "not_found" in err
                or "not found" in err
                or "429" in err
                or "resource_exhausted" in err
                or "quota exceeded" in err
            ):
                log(f"Gemini model {candidate} unavailable/quota-limited; trying next model.")
                continue
            raise RuntimeError(f"Gemini API request failed: {exc}") from exc

    raise RuntimeError(
        "Gemini API request failed after trying fallback models. "
        f"Last error: {last_error}"
    )


def summarize_with_zhipu(highlights: HighlightMap, exact_counts: bool = False) -> str:
    api_key = os.getenv("ZHIPU_API_KEY")
    if not api_key:
        raise RuntimeError("Missing required environment variable: ZHIPU_API_KEY")

    model = os.getenv("ZHIPU_MODEL", DEFAULT_ZHIPU_MODEL)
    compacted = compact_highlights_for_llm(highlights)
    prompt = build_exact_count_prompt(compacted) if exact_counts else build_gemini_prompt(compacted)
    log(f"Calling Zhipu model: {model}")

    endpoint = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    tried = set()
    candidates = [model, *ZHIPU_FALLBACK_MODELS]
    last_error = None
    timeout_seconds = int(os.getenv("ZHIPU_TIMEOUT_SECONDS", DEFAULT_ZHIPU_TIMEOUT_SECONDS))

    for candidate in candidates:
        if candidate in tried:
            continue
        tried.add(candidate)
        try:
            if candidate != model:
                log(f"Retrying with fallback Zhipu model: {candidate}")
            payload = {
                "model": candidate,
                "messages": [
                    {"role": "system", "content": "你是一个严谨的财经新闻摘要助手。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
            }
            for attempt in range(1, 4):
                try:
                    response = requests.post(
                        endpoint,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                        timeout=timeout_seconds,
                    )
                    response.raise_for_status()
                    data = response.json()
                    text = ((((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
                    if text:
                        return text
                    last_error = RuntimeError(f"Zhipu model {candidate} returned empty response.")
                    break
                except requests.Timeout as exc:
                    last_error = exc
                    if attempt < 3:
                        wait_s = attempt * 2
                        log(f"Zhipu timeout on {candidate} attempt {attempt}; retrying in {wait_s}s.")
                        time.sleep(wait_s)
                        continue
                    raise
        except Exception as exc:
            last_error = exc
            err = str(exc).lower()
            if "404" in err or "not found" in err or "429" in err or "resource_exhausted" in err:
                log(f"Zhipu model {candidate} unavailable/quota-limited; trying next model.")
                continue
            raise RuntimeError(f"Zhipu API request failed: {exc}") from exc

    raise RuntimeError(
        "Zhipu API request failed after trying fallback models. "
        f"Last error: {last_error}"
    )


def summarize_with_llm(highlights: HighlightMap, exact_counts: bool = False) -> str:
    provider = os.getenv("LLM_PROVIDER", DEFAULT_LLM_PROVIDER).strip().lower()
    if provider == "gemini":
        return summarize_with_gemini(highlights, exact_counts=exact_counts)
    if provider == "zhipu":
        return summarize_with_zhipu(highlights, exact_counts=exact_counts)
    raise RuntimeError("Unsupported LLM_PROVIDER. Use 'gemini' or 'zhipu'.")


def chunk_message(message: str, limit: int = TELEGRAM_CHUNK_LIMIT) -> List[str]:
    if len(message) <= limit:
        return [message]

    chunks: List[str] = []
    current = ""
    for paragraph in message.split("\n"):
        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > limit:
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token:
        raise RuntimeError("Missing required environment variable: TELEGRAM_BOT_TOKEN")
    if not chat_id:
        raise RuntimeError("Missing required environment variable: TELEGRAM_CHAT_ID")

    chunks = chunk_message(message)
    log(f"Sending {len(chunks)} Telegram message chunk(s).")
    for index, chunk in enumerate(chunks, start=1):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        try:
            response = requests.post(
                TELEGRAM_API_URL.format(token=token),
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"Telegram API request failed on chunk {index}: {exc}") from exc
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API returned ok=false on chunk {index}: {data}")
        time.sleep(0.5)


def fallback_message(reason: str) -> str:
    return textwrap.dedent(
        f"""
        📌 Newsfilter 美股新闻精选 | {date.today().isoformat()}

        【今日重点】
        1. 今日未能提取到 Newsfilter 首页 Highlights。

        【分领域摘要】
        - 暂无可用内容。

        【值得关注】
        - 公司/股票：未明确提及
        - 主题：数据抓取异常

        备注：{reason}
        """
    ).strip()


def main() -> None:
    load_local_env()
    dry_run = env_bool("DRY_RUN", False)
    send_fallback_on_error = env_bool("SEND_FALLBACK_ON_ERROR", DEFAULT_SEND_FALLBACK_ON_ERROR)
    try:
        highlights = fetch_highlights()
        if not highlights:
            raise RuntimeError("No Highlights were extracted from Newsfilter.")

        log(f"Highlights categories: {', '.join(highlights.keys())}")
        message = summarize_with_llm(highlights, exact_counts=False)
        if env_bool("ENFORCE_EXACT_COUNTS", DEFAULT_ENFORCE_EXACT_COUNTS) and not has_expected_section_counts(message, highlights):
            log("Summary bullets are fewer than extracted counts; regenerating with strict one-to-one mode by sector.")
            message = regenerate_exact_by_sector(highlights)
    except Exception as exc:
        log(f"Failed to build LLM summary: {exc}")
        if not send_fallback_on_error:
            raise
        message = fallback_message(str(exc))

    if dry_run:
        log("DRY_RUN=true; printing final message instead of sending Telegram.")
        print("\n" + message)
        return

    send_telegram(message)
    log("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"Fatal error: {exc}")
        sys.exit(1)
