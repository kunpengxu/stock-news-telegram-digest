import json
import os
import re
import sys
import textwrap
import time
from datetime import date
from typing import Dict, Iterable, List, Tuple

import requests
from bs4 import BeautifulSoup, Tag


NEWSFILTER_URL = "https://newsfilter.io/"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
DEFAULT_GEMINI_MODEL = "gemini-1.5-flash"
DEFAULT_MAX_BULLETS_PER_SECTOR = 6
TELEGRAM_CHUNK_LIMIT = 3900

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
    "other": "其他 Other",
}


def log(message: str) -> None:
    print(f"[newsfilter] {message}", flush=True)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_bullet(value: str) -> str:
    value = normalize_space(value)
    return re.sub(r"^[•\-\*\u2022\s]+", "", value).strip()


def canonical_category(value: str) -> str:
    value = normalize_space(value)
    lowered = value.lower()
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


def parse_highlights(html: str) -> HighlightMap:
    soup = BeautifulSoup(html, "html.parser")
    for noisy in soup(["script", "style", "noscript", "svg"]):
        noisy.decompose()

    container = find_highlights_container(soup)

    for extractor in (extract_from_tab_panels, extract_from_headed_lists, extract_json_like_highlights):
        highlights = extractor(container if extractor != extract_json_like_highlights else soup)
        if highlights:
            return highlights

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
    response.raise_for_status()
    return response.text


def fetch_highlights_with_playwright(max_bullets_per_sector: int) -> HighlightMap:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run: python -m playwright install chromium") from exc

    log("Static extraction was empty; trying Playwright-rendered page.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        try:
            page.goto(NEWSFILTER_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                log("Network idle timed out; continuing with the loaded DOM.")

            js = """
            () => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span')]
                .filter((el) => /\\bHighlights\\b/i.test(norm(el.textContent || '')));
              let root = headings[0] || document.body;
              for (let i = 0; i < 8 && root.parentElement; i++) {
                const lis = root.querySelectorAll('li').length;
                const buttons = root.querySelectorAll('button,[role="tab"]').length;
                if (lis || buttons >= 2) break;
                root = root.parentElement;
              }
              const selectors = 'button,[role="tab"],[aria-controls]';
              return [...root.querySelectorAll(selectors)]
                .filter((el) => {
                  const r = el.getBoundingClientRect();
                  return r.width > 0 && r.height > 0 && norm(el.textContent);
                })
                .map((el, idx) => ({ idx, text: norm(el.textContent) }));
            }
            """
            tabs = page.evaluate(js)
            highlights: HighlightMap = {}

            if tabs:
                for tab in tabs:
                    label = tab["text"]
                    if label.lower() == "highlights":
                        continue
                    locator = page.locator("button, [role='tab'], [aria-controls]").filter(has_text=label).first
                    try:
                        locator.click(timeout=5000)
                        page.wait_for_timeout(600)
                    except Exception as exc:
                        log(f"Could not click category tab '{label}': {exc}")
                        continue
                    bullets = page.evaluate(
                        """
                        () => {
                          const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                          const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                          };
                          const heading = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span')]
                            .find((el) => /\\bHighlights\\b/i.test(norm(el.textContent || '')));
                          let root = heading || document.body;
                          for (let i = 0; i < 8 && root.parentElement; i++) {
                            if (root.querySelectorAll('li').length) break;
                            root = root.parentElement;
                          }
                          return [...root.querySelectorAll('li')]
                            .filter(visible)
                            .map((li) => norm(li.textContent))
                            .filter(Boolean);
                        }
                        """
                    )
                    if bullets:
                        highlights[label] = bullets[:max_bullets_per_sector]
            else:
                html = page.content()
                highlights = parse_highlights(html)

            return merge_highlights(highlights)
        finally:
            browser.close()


def fetch_highlights() -> HighlightMap:
    max_bullets = int(os.getenv("MAX_BULLETS_PER_SECTOR", DEFAULT_MAX_BULLETS_PER_SECTOR))
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
    return textwrap.dedent(
        f"""
        你是一个美股市场新闻摘要助手。请只基于下面 Newsfilter 首页 Highlights 抓取到的英文内容，生成 Telegram 友好的中文纯文本摘要。

        要求：
        - 不要编造事实，不要加入 Highlights 中没有的信息。
        - 翻译并压缩英文要点，语言简洁，适合盘后市场监控。
        - 保留公司名、ticker、金额、百分比、日期和重要数字的原文形式。
        - 不提供买入、卖出、持有等投资建议。
        - 按原始 sector/category 分组；没有内容的分类不要输出。
        - 顶部输出 3-5 条跨领域「今日重点」。
        - 末尾输出「值得关注」，列出提到的公司/股票和主题；如果无法识别则写“未明确提及”。
        - 只输出最终消息，不要解释过程。

        输出格式：
        📌 Newsfilter 美股新闻精选 | {today}

        【今日重点】
        1. ...
        2. ...
        3. ...

        【科技 Tech】
        - ...

        【值得关注】
        - 公司/股票：...
        - 主题：...

        原始 Highlights JSON：
        {payload}
        """
    ).strip()


def summarize_with_gemini(highlights: HighlightMap) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing required environment variable: GEMINI_API_KEY")

    model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    prompt = build_gemini_prompt(highlights)
    log(f"Calling Gemini model: {model}")

    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        text = (response.text or "").strip()
    except Exception as exc:
        raise RuntimeError(f"Gemini API request failed: {exc}") from exc

    if not text:
        raise RuntimeError("Gemini returned an empty response.")
    return text


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
    dry_run = env_bool("DRY_RUN", False)
    try:
        highlights = fetch_highlights()
        if not highlights:
            raise RuntimeError("No Highlights were extracted from Newsfilter.")

        log(f"Highlights categories: {', '.join(highlights.keys())}")
        message = summarize_with_gemini(highlights)
    except Exception as exc:
        log(f"Failed to build Gemini summary: {exc}")
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
