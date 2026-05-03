# Commerce Price Monitor

Monitor public e-commerce pages for products, prices, availability, screenshots, and changes over time.

This Actor is built for stores, agencies, resellers, SEO teams, and price-monitoring workflows where the user is allowed to crawl the target pages.

## What it does

- Crawls store, category, collection, and product URLs
- Renders pages with Playwright
- Detects product pages by URL patterns and structured data
- Extracts product title, price, currency, availability, brand, SKU, image, and page hash
- Reads JSON-LD Product data when available
- Falls back to common price and title selectors
- Captures optional screenshots
- Compares results against previous runs
- Marks price, availability, and content changes
- Saves structured records to the default Apify Dataset

## Best use cases

- Price monitoring
- Product availability monitoring
- Competitor product tracking
- Store catalog discovery
- Client site audit snapshots
- Reseller listing watchlists

## Input

The Actor uses `INPUT_SCHEMA.json`.

Important settings:

- `start_urls`: store, category, collection, or product URLs
- `max_pages`: total pages to visit
- `max_products`: total product records to save
- `max_depth`: link depth from the start URLs
- `product_url_patterns`: URL fragments that identify product pages
- `price_selectors`: optional CSS selectors for price fallback
- `title_selectors`: optional CSS selectors for title fallback
- `capture_screenshots`: save product screenshots
- `detect_changes`: compare against previous run state

## Output

Each product record includes:

- `url`
- `title`
- `price.raw`
- `price.amount`
- `price.currency`
- `availability`
- `brand`
- `sku`
- `image`
- `images`
- `content_hash`
- `previous_price`
- `price_changed`
- `availability_changed`
- `content_changed`
- `changed`
- `screenshot_key`

The final Dataset item is a summary with crawl totals.

## Monetization

Recommended Apify pricing:

- keep `apify-actor-start`
- keep `apify-default-dataset-item`
- price per product/result item

This Actor has a clear paid use case: users pay to monitor products and price changes without building their own crawler.

## Responsible use

Use this Actor only on websites you own, manage, or are allowed to crawl. It does not bypass CAPTCHA, paywalls, logins, or access controls.
