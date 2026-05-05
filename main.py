from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urldefrag, urljoin, urlparse

from apify import Actor
from playwright.async_api import Page, async_playwright


DEFAULT_PRICE_SELECTORS = [
    "[itemprop='price']",
    "[data-price]",
    ".price",
    ".product-price",
    "[class*='price']",
]
DEFAULT_TITLE_SELECTORS = [
    "h1",
    "[itemprop='name']",
    ".product-title",
]

MAX_BACKOFF_SECS = 30


def normalize_url(url: str) -> str:
    clean, _fragment = urldefrag(url.strip())
    parsed = urlparse(clean)
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return parsed._replace(netloc=host, path=path, fragment="").geturl()


def same_domain(url: str, allowed_hosts: Iterable[str]) -> bool:
    host = urlparse(url).netloc.lower()
    for allowed in _scope_hosts(allowed_hosts):
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def _scope_hosts(allowed_hosts: Iterable[str]) -> set[str]:
    scoped: set[str] = set()
    for host in allowed_hosts:
        host = host.lower().strip(".")
        if not host:
            continue
        scoped.add(host)
        if host.startswith("www."):
            scoped.add(host[4:])
        else:
            scoped.add("www." + host)
    return scoped


def content_hash(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()


def stable_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()


def parse_price(value: str) -> dict[str, Any]:
    text = " ".join(str(value or "").split())
    currency = ""
    if "$" in text:
        currency = "USD"
    elif "£" in text:
        currency = "GBP"
    elif "€" in text:
        currency = "EUR"
    # Strip non-numeric characters except digits, commas, and dots
    clean = re.sub(r"[^\d.,]", "", text)
    # Handle European format: 1.234,56 → 1234.56
    if re.search(r"\d{1,3}(?:\.\d{3})+,\d{2}$", clean):
        clean = clean.replace(".", "").replace(",", ".")
    else:
        clean = clean.replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", clean)
    amount = float(match.group()) if match else None
    return {"raw": text, "amount": amount, "currency": currency}


def normalize_availability(text: str) -> str:
    """Normalize availability text to 'InStock', 'OutOfStock', or the original value."""
    if not text:
        return ""
    compact = text.lower().replace(" ", "").replace("-", "")
    out_signals = {"outofstock", "soldout", "unavailable", "notavailable", "nostock"}
    in_signals = {"instock", "available", "addtocart", "addtobag", "buynow", "orderonline"}
    if any(s in compact for s in out_signals):
        return "OutOfStock"
    if any(s in compact for s in in_signals):
        return "InStock"
    return text.strip()


def compute_price_change(
    current: float | None,
    previous: float | None,
) -> tuple[float | None, float | None]:
    """Return (changeAmount, changePercent). Both None when not computable."""
    if current is None or previous is None or previous == 0:
        return None, None
    change_amount = round(current - previous, 4)
    change_percent = round((change_amount / previous) * 100, 2)
    return change_amount, change_percent


def regex_any(patterns: list[str], url: str) -> bool:
    for pattern in patterns:
        if not pattern:
            continue
        try:
            if re.search(pattern, url, flags=re.I):
                return True
        except re.error:
            if pattern.lower() in url.lower():
                return True
    return False


class PageDataParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.description = ""
        self.canonical = ""
        self.links: set[str] = set()
        self.images: list[str] = []
        self.json_ld_raw: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._in_json_ld = False
        self._json_ld_parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {k.lower(): v or "" for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "script" and attrs_dict.get("type", "").lower() == "application/ld+json":
            self._in_json_ld = True
            self._json_ld_parts = []
        elif tag in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1
        elif tag == "a" and attrs_dict.get("href"):
            url = normalize_url(urljoin(self.base_url, attrs_dict["href"]))
            if url:
                self.links.add(url)
        elif tag == "img":
            src = attrs_dict.get("src") or attrs_dict.get("data-src")
            if src:
                url = normalize_url(urljoin(self.base_url, src))
                if url:
                    self.images.append(url)
        elif tag == "meta":
            name = attrs_dict.get("name", "").lower()
            prop = attrs_dict.get("property", "").lower()
            if name == "description" or prop == "og:description":
                self.description = attrs_dict.get("content", "").strip()
        elif tag == "link" and attrs_dict.get("rel") and attrs_dict.get("href"):
            rels = {part.strip().lower() for part in attrs_dict["rel"].split()}
            if "canonical" in rels:
                self.canonical = normalize_url(urljoin(self.base_url, attrs_dict["href"]))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            raw = "\n".join(self._json_ld_parts).strip()
            if raw:
                self.json_ld_raw.append(raw)
        elif tag in {"script", "style", "noscript", "template"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_ld_parts.append(data)
            return
        text = " ".join(data.split())
        if not text or self._skip_depth:
            return
        if self._in_title:
            self.title = (self.title + " " + text).strip()
            return
        self.text_parts.append(text)

    @property
    def text(self) -> str:
        return " ".join(self.text_parts)


def iter_json_ld_objects(raw_values: list[str]) -> Iterable[dict[str, Any]]:
    for raw in raw_values:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        yield from _walk_json_ld(parsed)


def _walk_json_ld(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _walk_json_ld(item)
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json_ld(item)


def product_from_json_ld(objects: Iterable[dict[str, Any]]) -> dict[str, Any]:
    for item in objects:
        item_type = item.get("@type", "")
        types = item_type if isinstance(item_type, list) else [item_type]
        if not any(str(t).lower() == "product" for t in types):
            continue
        offers = item.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = offers.get("price") or offers.get("lowPrice") or item.get("price") or ""
        availability = offers.get("availability") or item.get("availability") or ""
        image = item.get("image") or ""
        if isinstance(image, list):
            image = image[0] if image else ""
        return {
            "name": str(item.get("name") or ""),
            "sku": str(item.get("sku") or item.get("mpn") or ""),
            "brand": _extract_brand(item.get("brand")),
            "price": parse_price(str(price)),
            "availability": str(availability).split("/")[-1] if availability else "",
            "image": str(image or ""),
            "description": str(item.get("description") or ""),
            "source": "json_ld",
        }
    return {}


def _extract_brand(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value or "")


async def text_from_first_selector(page: Page, selectors: list[str]) -> str:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                text = await locator.inner_text(timeout=1200)
                if text.strip():
                    return " ".join(text.split())
        except Exception:
            continue
    return ""


async def extract_product(page: Page, url: str, html: str, parser: PageDataParser, actor_input: dict[str, Any]) -> dict[str, Any]:
    json_ld_product = product_from_json_ld(iter_json_ld_objects(parser.json_ld_raw))
    title_selectors = actor_input.get("title_selectors") or DEFAULT_TITLE_SELECTORS
    price_selectors = actor_input.get("price_selectors") or DEFAULT_PRICE_SELECTORS
    selector_title = await text_from_first_selector(page, list(title_selectors))
    selector_price = await text_from_first_selector(page, list(price_selectors))
    title = json_ld_product.get("name") or selector_title or parser.title
    price = json_ld_product.get("price") or parse_price(selector_price)
    raw_availability = json_ld_product.get("availability") or await text_from_first_selector(
        page,
        [".availability", "[class*='stock']", "[class*='available']", "[itemprop='availability']"],
    )
    availability = normalize_availability(raw_availability)
    record = {
        "type": "product",
        "url": url,
        "normalizedUrl": url,  # url is already normalize_url(page.url) from the caller
        "title": title,
        "price": price,
        "currency": price.get("currency", "") if isinstance(price, dict) else "",
        "availability": availability,
        "description": json_ld_product.get("description") or parser.description,
        "canonical": parser.canonical,
        "brand": json_ld_product.get("brand", ""),
        "sku": json_ld_product.get("sku", ""),
        "image": json_ld_product.get("image") or (parser.images[0] if parser.images else ""),
        "images": parser.images[:25],
        "content_hash": content_hash(parser.text),
        "word_count": len(parser.text.split()),
        "link_count": len(parser.links),
        "extraction_source": json_ld_product.get("source", "selectors"),
    }
    return record


def looks_like_product_url(url: str, product_patterns: list[str]) -> bool:
    lower = url.lower()
    return any(pattern.lower() in lower for pattern in product_patterns if pattern)


def has_product_data(record: dict[str, Any]) -> bool:
    price = record.get("price") or {}
    return bool(record.get("title") and (price.get("amount") is not None or record.get("sku") or record.get("availability")))


@dataclass
class CrawlItem:
    url: str
    depth: int


@dataclass
class ScanConfig:
    timeout_ms: int
    max_retries: int
    max_depth: int
    wait_ms: int
    product_patterns: list[str]
    include_patterns: list[str]
    exclude_patterns: list[str]
    allowed_hosts: set[str]
    same_domain_only: bool
    detect_changes: bool
    take_screenshots: bool
    screenshot_on_change_only: bool
    screenshot_on_failure: bool
    min_change_percent: float
    actor_input: dict[str, Any]


@dataclass
class ScanResult:
    url: str
    new_items: list[CrawlItem] = field(default_factory=list)
    product: dict[str, Any] | None = None
    screenshot_saved: bool = False
    status: str = "failed"  # "changed" | "unchanged" | "failed"
    error_message: str = ""


async def goto_with_retry(page: Page, url: str, timeout_ms: int, max_retries: int) -> Any:
    """Navigate to a URL with exponential backoff retries."""
    for attempt in range(max_retries + 1):
        if attempt > 0:
            wait_secs = min(2 ** attempt, MAX_BACKOFF_SECS)
            Actor.log.info(f"Retry {attempt}/{max_retries} for {url} (backing off {wait_secs}s)")
            await asyncio.sleep(wait_secs)
        try:
            return await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        except Exception:
            if attempt < max_retries:
                continue
            raise


async def scan_page(
    item: CrawlItem,
    context: Any,
    store: Any,
    cfg: ScanConfig,
) -> ScanResult:
    """Scan a single page. Returns a ScanResult with extracted product data and discovered links."""
    current_url = normalize_url(item.url)
    result = ScanResult(url=current_url)
    page = await context.new_page()
    try:
        response = await goto_with_retry(page, current_url, cfg.timeout_ms, cfg.max_retries)
        if cfg.wait_ms:
            await page.wait_for_timeout(cfg.wait_ms)
        final_url = normalize_url(page.url) or current_url
        html = await page.content()
        parser = PageDataParser(final_url)
        parser.feed(html)

        if item.depth < cfg.max_depth:
            internal_links = sorted(
                link for link in parser.links if same_domain(link, cfg.allowed_hosts)
            )
            prioritized = sorted(
                internal_links,
                key=lambda link: 0 if looks_like_product_url(link, cfg.product_patterns) else 1,
            )
            result.new_items = [CrawlItem(url=link, depth=item.depth + 1) for link in prioritized]

        likely_product = looks_like_product_url(final_url, cfg.product_patterns)
        product = await extract_product(page, final_url, html, parser, cfg.actor_input)
        if not (likely_product or has_product_data(product)):
            result.status = "unchanged"
            return result

        timestamp = datetime.now(timezone.utc).isoformat()
        state_key = f"product:{stable_key(final_url)}"
        previous = await store.get_value(state_key)
        previous_record = previous if isinstance(previous, dict) else {}
        previous_price_dict = previous_record.get("price") or {}
        previous_amount: float | None = (
            previous_price_dict.get("amount") if isinstance(previous_price_dict, dict) else None
        )
        current_price_dict = product.get("price") or {}
        current_amount: float | None = (
            current_price_dict.get("amount") if isinstance(current_price_dict, dict) else None
        )

        change_amount, change_percent = compute_price_change(current_amount, previous_amount)
        price_changed = bool(
            cfg.detect_changes and previous_record and previous_amount != current_amount
        )
        availability_changed = bool(
            cfg.detect_changes
            and previous_record
            and previous_record.get("availability") != product.get("availability")
        )
        content_changed = bool(
            cfg.detect_changes
            and previous_record
            and previous_record.get("content_hash") != product.get("content_hash")
        )
        is_new = not previous_record

        # Apply minChangePercent threshold: suppress price_changed if the swing is too small
        if price_changed and cfg.min_change_percent > 0 and change_percent is not None:
            if abs(change_percent) < cfg.min_change_percent:
                price_changed = False

        any_changed = price_changed or availability_changed or content_changed or is_new
        result.status = "changed" if any_changed else "unchanged"

        product.update({
            "timestamp": timestamp,
            "status": result.status,
            "httpStatus": int(response.status) if response else 200,
            "depth": item.depth,
            # Structured change fields
            "previousPrice": previous_amount,
            "priceChanged": price_changed,
            "availabilityChanged": availability_changed,
            "contentChanged": content_changed,
            "changeAmount": change_amount,
            "changePercent": change_percent,
            "changed": any_changed,
            # Legacy fields for backward compatibility
            "previous_price": previous_price_dict,
            "price_changed": price_changed,
            "availability_changed": availability_changed,
            "content_changed": content_changed,
        })

        should_screenshot = cfg.take_screenshots and not (
            cfg.screenshot_on_change_only and not any_changed
        )
        if should_screenshot:
            screenshot_key = f"screenshot-{stable_key(final_url)}.png"
            screenshot = await page.screenshot(full_page=True)
            await store.set_value(screenshot_key, screenshot, content_type="image/png")
            product["screenshotKey"] = screenshot_key
            product["screenshot_key"] = screenshot_key  # legacy
            result.screenshot_saved = True

        await store.set_value(state_key, product)
        result.product = product

        if price_changed and change_amount is not None and change_percent is not None:
            direction = "▼ dropped" if change_amount < 0 else "▲ rose"
            Actor.log.info(
                f"Price {direction} {change_percent:+.1f}% for {final_url} "
                f"({previous_amount} → {current_amount} {current_price_dict.get('currency', '')})"
            )
        elif any_changed:
            Actor.log.info(f"[changed] {final_url}")
        else:
            Actor.log.info(f"[unchanged] {final_url}")

        return result

    except Exception as exc:
        error_message = str(exc)
        Actor.log.warning(f"[failed] {current_url}: {error_message}")

        if cfg.take_screenshots and cfg.screenshot_on_failure:
            try:
                screenshot_key = f"screenshot-{stable_key(current_url)}.png"
                screenshot = await page.screenshot(full_page=True)
                await store.set_value(screenshot_key, screenshot, content_type="image/png")
                result.screenshot_saved = True
            except Exception:
                pass

        result.product = {
            "type": "product",
            "url": current_url,
            "normalizedUrl": current_url,
            "status": "failed",
            "errorMessage": error_message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "depth": item.depth,
        }
        result.status = "failed"
        result.error_message = error_message
        return result
    finally:
        await page.close()


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        start_urls = [
            normalize_url(item.get("url", ""))
            for item in actor_input.get("start_urls", [])
            if isinstance(item, dict) and item.get("url")
        ]
        start_urls = [url for url in start_urls if url]
        if not start_urls:
            await Actor.push_data({"type": "error", "error": "No start URLs were provided."})
            return

        max_pages = int(actor_input.get("max_pages", 500) or 500)
        max_products = int(actor_input.get("max_products", 250) or 250)
        max_depth = int(actor_input.get("max_depth", 4) or 4)
        same_domain_only = bool(actor_input.get("same_domain_only", True))
        detect_changes = bool(actor_input.get("detect_changes", True))
        wait_ms = int(actor_input.get("wait_ms", 1000) or 0)
        timeout_secs = int(actor_input.get("timeoutSecs", 35) or 35)
        max_retries = int(actor_input.get("maxRetries", 2) or 0)
        max_concurrency = max(1, int(actor_input.get("maxConcurrency", 1) or 1))
        min_change_percent = float(actor_input.get("minChangePercent", 0) or 0)
        # takeScreenshots takes priority; falls back to legacy capture_screenshots
        take_screenshots = bool(
            actor_input.get("takeScreenshots", actor_input.get("capture_screenshots", True))
        )
        screenshot_on_change_only = bool(actor_input.get("screenshotOnChangeOnly", False))
        screenshot_on_failure = bool(actor_input.get("screenshotOnFailure", False))
        product_patterns = list(actor_input.get("product_url_patterns") or [])
        include_patterns = list(actor_input.get("include_patterns") or [])
        exclude_patterns = list(actor_input.get("exclude_patterns") or [])

        Actor.log.info(
            f"Commerce Price Scanner | URLs: {len(start_urls)} | "
            f"maxPages: {max_pages} | maxProducts: {max_products} | "
            f"maxRetries: {max_retries} | timeout: {timeout_secs}s | "
            f"concurrency: {max_concurrency} | screenshots: {take_screenshots}"
        )

        store = await Actor.open_key_value_store()
        allowed_hosts = {urlparse(url).netloc.lower() for url in start_urls}

        cfg = ScanConfig(
            timeout_ms=timeout_secs * 1000,
            max_retries=max_retries,
            max_depth=max_depth,
            wait_ms=wait_ms,
            product_patterns=product_patterns,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            allowed_hosts=allowed_hosts,
            same_domain_only=same_domain_only,
            detect_changes=detect_changes,
            take_screenshots=take_screenshots,
            screenshot_on_change_only=screenshot_on_change_only,
            screenshot_on_failure=screenshot_on_failure,
            min_change_percent=min_change_percent,
            actor_input=actor_input,
        )

        queue: deque[CrawlItem] = deque(CrawlItem(url=url, depth=0) for url in start_urls)
        seen: set[str] = set()
        pages_seen = 0
        products_saved = 0
        stats_changed = 0
        stats_unchanged = 0
        stats_failed = 0
        screenshots_saved = 0

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-gpu"]
            )
            context = await browser.new_context(ignore_https_errors=True)
            try:
                pending: dict[asyncio.Task, CrawlItem] = {}
                while True:
                    # Fill task slots up to max_concurrency
                    while (
                        queue
                        and len(pending) < max_concurrency
                        and pages_seen < max_pages
                        and products_saved < max_products
                    ):
                        item = queue.popleft()
                        current_url = normalize_url(item.url)
                        if not current_url or current_url in seen:
                            continue
                        if cfg.same_domain_only and not same_domain(current_url, allowed_hosts):
                            continue
                        if include_patterns and not regex_any(include_patterns, current_url):
                            continue
                        if exclude_patterns and regex_any(exclude_patterns, current_url):
                            continue
                        seen.add(current_url)
                        pages_seen += 1
                        task = asyncio.create_task(scan_page(item, context, store, cfg))
                        pending[task] = item

                    if not pending:
                        break

                    done, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        pending.pop(task)
                        try:
                            result: ScanResult = await task
                        except Exception as exc:
                            Actor.log.error(f"Unexpected task failure: {exc}")
                            stats_failed += 1
                            continue

                        if result.screenshot_saved:
                            screenshots_saved += 1

                        if result.product is not None:
                            await Actor.push_data(result.product)
                            if result.status == "changed":
                                stats_changed += 1
                                products_saved += 1
                            elif result.status == "unchanged":
                                stats_unchanged += 1
                                products_saved += 1
                            else:
                                stats_failed += 1

                        for new_item in result.new_items:
                            if new_item.url not in seen:
                                queue.append(new_item)

                        if products_saved >= max_products:
                            queue.clear()
                            break

                Actor.log.info(
                    f"Scan complete | scanned: {pages_seen} | changed: {stats_changed} | "
                    f"unchanged: {stats_unchanged} | failed: {stats_failed} | "
                    f"screenshots: {screenshots_saved}"
                )
                await Actor.push_data({
                    "type": "summary",
                    # New structured fields
                    "startUrls": start_urls,
                    "totalScanned": pages_seen,
                    "changed": stats_changed,
                    "unchanged": stats_unchanged,
                    "failed": stats_failed,
                    "screenshotsSaved": screenshots_saved,
                    # Legacy fields for backward compatibility
                    "start_urls": start_urls,
                    "pages_seen": pages_seen,
                    "products_saved": products_saved,
                    "changed_products": stats_changed,
                    "remaining_urls": len(queue),
                    "same_domain_only": same_domain_only,
                })
            finally:
                await context.close()
                await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
