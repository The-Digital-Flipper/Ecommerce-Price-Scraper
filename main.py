from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urldefrag, urljoin, urlparse

from apify import Actor
from playwright.async_api import Page, async_playwright


DEFAULT_PRICE_SELECTORS = [
    "[itemprop='price']",
    ".price",
    ".product-price",
    "[class*='price']",
]
DEFAULT_TITLE_SELECTORS = [
    "h1",
    "[itemprop='name']",
    ".product-title",
]


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
    match = re.search(r"([0-9]+(?:[,.][0-9]{2})?)", text.replace(",", ""))
    amount = float(match.group(1)) if match else None
    return {"raw": text, "amount": amount, "currency": currency}


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
    availability_text = json_ld_product.get("availability") or await text_from_first_selector(
        page,
        [".availability", "[class*='stock']", "[class*='available']", "[itemprop='availability']"],
    )
    record = {
        "type": "product",
        "url": url,
        "title": title,
        "description": json_ld_product.get("description") or parser.description,
        "canonical": parser.canonical,
        "price": price,
        "availability": availability_text,
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
        capture_screenshots = bool(actor_input.get("capture_screenshots", True))
        detect_changes = bool(actor_input.get("detect_changes", True))
        wait_ms = int(actor_input.get("wait_ms", 1000) or 0)
        snapshot_store_name = str(actor_input.get("snapshot_store_name") or "commerce-price-monitor-snapshots")
        product_patterns = list(actor_input.get("product_url_patterns") or [])
        include_patterns = list(actor_input.get("include_patterns") or [])
        exclude_patterns = list(actor_input.get("exclude_patterns") or [])

        try:
            snapshot_store = await Actor.open_key_value_store(name=snapshot_store_name)
        except Exception:
            Actor.log.warning(
                "Could not open named key-value store '%s' (insufficient permissions?); "
                "falling back to the default store.",
                snapshot_store_name,
            )
            snapshot_store = await Actor.open_key_value_store()
        try:
            media_store = await Actor.open_key_value_store(name="commerce-price-monitor-media")
        except Exception:
            Actor.log.warning(
                "Could not open named key-value store 'commerce-price-monitor-media' "
                "(insufficient permissions?); falling back to the default store."
            )
            media_store = await Actor.open_key_value_store()
        allowed_hosts = {urlparse(url).netloc.lower() for url in start_urls}
        queue = deque(CrawlItem(url=url, depth=0) for url in start_urls)
        seen: set[str] = set()
        pages_seen = 0
        products_saved = 0
        changed_products = 0

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            context = await browser.new_context(ignore_https_errors=True)
            try:
                while queue and pages_seen < max_pages and products_saved < max_products:
                    item = queue.popleft()
                    current_url = normalize_url(item.url)
                    if not current_url or current_url in seen:
                        continue
                    if same_domain_only and not same_domain(current_url, allowed_hosts):
                        continue
                    if include_patterns and not regex_any(include_patterns, current_url):
                        continue
                    if exclude_patterns and regex_any(exclude_patterns, current_url):
                        continue
                    seen.add(current_url)
                    pages_seen += 1

                    page = await context.new_page()
                    try:
                        response = await page.goto(current_url, wait_until="networkidle", timeout=35000)
                        if wait_ms:
                            await page.wait_for_timeout(wait_ms)
                        final_url = normalize_url(page.url) or current_url
                        html = await page.content()
                        parser = PageDataParser(final_url)
                        parser.feed(html)
                        internal_links = sorted(link for link in parser.links if same_domain(link, allowed_hosts))

                        likely_product = looks_like_product_url(final_url, product_patterns)
                        product = await extract_product(page, final_url, html, parser, actor_input)
                        if likely_product or has_product_data(product):
                            state_key = f"product:{stable_key(final_url)}"
                            previous = await snapshot_store.get_value(state_key)
                            previous_record = previous if isinstance(previous, dict) else {}
                            previous_price = previous_record.get("price", {}) if isinstance(previous_record, dict) else {}
                            price_changed = bool(
                                detect_changes
                                and previous_record
                                and previous_price.get("amount") != product.get("price", {}).get("amount")
                            )
                            availability_changed = bool(
                                detect_changes
                                and previous_record
                                and previous_record.get("availability") != product.get("availability")
                            )
                            content_changed = bool(
                                detect_changes
                                and previous_record
                                and previous_record.get("content_hash") != product.get("content_hash")
                            )
                            product.update({
                                "status": int(response.status) if response else 200,
                                "depth": item.depth,
                                "previous_price": previous_price,
                                "price_changed": price_changed,
                                "availability_changed": availability_changed,
                                "content_changed": content_changed,
                                "changed": price_changed or availability_changed or content_changed or not previous_record,
                            })
                            if product["changed"]:
                                changed_products += 1
                            if capture_screenshots:
                                screenshot_key = f"screenshot-{stable_key(final_url)}.png"
                                screenshot = await page.screenshot(full_page=True)
                                await media_store.set_value(screenshot_key, screenshot, content_type="image/png")
                                product["screenshot_key"] = screenshot_key
                            await snapshot_store.set_value(state_key, product)
                            await Actor.push_data(product)
                            products_saved += 1

                        if item.depth < max_depth:
                            prioritized = sorted(
                                internal_links,
                                key=lambda link: 0 if looks_like_product_url(link, product_patterns) else 1,
                            )
                            for link in prioritized:
                                if link not in seen:
                                    queue.append(CrawlItem(url=link, depth=item.depth + 1))
                    except Exception as exc:
                        await Actor.push_data({
                            "type": "error",
                            "url": current_url,
                            "depth": item.depth,
                            "error": str(exc),
                        })
                    finally:
                        await page.close()

                await Actor.push_data({
                    "type": "summary",
                    "start_urls": start_urls,
                    "pages_seen": pages_seen,
                    "products_saved": products_saved,
                    "changed_products": changed_products,
                    "remaining_urls": len(queue),
                    "same_domain_only": same_domain_only,
                })
            finally:
                await context.close()
                await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
