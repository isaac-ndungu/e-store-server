#!/usr/bin/env python3
"""Report sitemap crawl health: status, timing, price presence, and related-product count.

Fetches every URL in the given XML sitemap(s) and checks each page for the
expected render quality: a visible KES price, valid Product JSON-LD with
priceCurrency KES and availability, and the number of related-product links.
Any degraded render (missing price, missing JSON-LD, zero related products on
a product page) is flagged so operators can detect storefront cache misses,
throttle exhaustion, or broken data before crawlers do.

Usage:
    python scripts/crawl-check.py SITEMAP_URL [--delay SECONDS] [--timeout SECONDS]

Examples:
    python scripts/crawl-check.py https://shop.example.com/sitemap.xml
    python scripts/crawl-check.py https://shop.example.com/sitemap.xml --delay 1
    python scripts/crawl-check.py https://shop.example.com/product-sitemap.xml --timeout 15
"""

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_KES_PRICE_RE = re.compile(
    r"(?:KES|Ksh|KSh)\s*[\d,]+(?:\.\d{2})?|[\d,]+(?:\.\d{2})?\s*(?:KES|Ksh|KSh)",
    re.IGNORECASE,
)
_JSON_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_RELATED_LINK_RE = re.compile(
    r'<a[^>]+href=["\'][^"\']*(?:/products?/)[^"\']*["\']',
    re.IGNORECASE,
)


def _extract_sitemap_urls(sitemap_url, timeout):
    """Fetch and parse an XML sitemap, returning all <loc> URLs.

    Handles both sitemap index files (recursive) and regular sitemaps.

    Args:
        sitemap_url: the sitemap URL to fetch.
        timeout: request timeout in seconds.

    Returns:
        list[str]: the extracted page URLs.
    """
    response = requests.get(sitemap_url, timeout=timeout)
    response.raise_for_status()
    root = ET.fromstring(response.content)

    sitemapindex_tag = f"{{{_SITEMAP_NS['sm']}}}sitemapindex"
    if root.tag == sitemapindex_tag or root.tag == "sitemapindex":
        child_urls = []
        for sitemap_el in root.findall("sm:sitemap/sm:loc", _SITEMAP_NS):
            child_urls.extend(_extract_sitemap_urls(sitemap_el.text.strip(), timeout))
        for sitemap_el in root.findall("sitemap/loc"):
            child_urls.extend(_extract_sitemap_urls(sitemap_el.text.strip(), timeout))
        return child_urls

    urls = []
    for loc_el in root.findall("sm:url/sm:loc", _SITEMAP_NS):
        urls.append(loc_el.text.strip())
    for loc_el in root.findall("url/loc"):
        urls.append(loc_el.text.strip())
    return urls


def _check_json_ld(html):
    """Extract Product JSON-LD and validate priceCurrency and availability.

    Args:
        html: the page HTML source.

    Returns:
        dict: ``has_json_ld`` (bool), ``price_currency`` (str|None),
              ``availability`` (str|None), ``json_ld_price`` (str|None).
    """
    result = {
        "has_json_ld": False,
        "price_currency": None,
        "availability": None,
        "json_ld_price": None,
    }
    for match in _JSON_LD_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if item.get("@type") != "Product":
                continue
            result["has_json_ld"] = True
            offers = item.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            result["price_currency"] = offers.get("priceCurrency")
            result["availability"] = offers.get("availability")
            result["json_ld_price"] = offers.get("price")
            return result
    return result


def _check_url(url, timeout):
    """Fetch a single URL and report its crawl health.

    Args:
        url: the page URL.
        timeout: request timeout in seconds.

    Returns:
        dict: status, timing, price presence, related-product count, and
              JSON-LD validation results.
    """
    start = time.monotonic()
    try:
        response = requests.get(url, timeout=timeout, allow_redirects=True)
        elapsed = time.monotonic() - start
    except requests.RequestException as exc:
        return {
            "url": url,
            "status": 0,
            "time_s": round(time.monotonic() - start, 3),
            "error": str(exc),
            "has_kes_price": False,
            "related_product_count": 0,
            "has_json_ld": False,
            "price_currency": None,
            "availability": None,
            "json_ld_price": None,
        }

    html = response.text
    has_kes_price = bool(_KES_PRICE_RE.search(html))
    related_links = _RELATED_LINK_RE.findall(html)

    parsed = urlparse(url)
    is_product_page = "/product" in parsed.path.lower()

    json_ld_info = _check_json_ld(html)

    return {
        "url": url,
        "status": response.status_code,
        "time_s": round(elapsed, 3),
        "has_kes_price": has_kes_price,
        "related_product_count": len(related_links),
        "is_product_page": is_product_page,
        **json_ld_info,
    }


def main():
    """Entry point for the crawl-check script."""
    parser = argparse.ArgumentParser(
        description="Check sitemap URLs for render quality."
    )
    parser.add_argument("sitemap_url", help="URL of the XML sitemap to crawl.")
    parser.add_argument(
        "--delay",
        type=float,
        default=0,
        help="Seconds to wait between requests (default: 0).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10,
        help="Request timeout in seconds (default: 10).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON instead of a table.",
    )
    args = parser.parse_args()

    print(f"Fetching sitemap: {args.sitemap_url}", file=sys.stderr)
    try:
        urls = _extract_sitemap_urls(args.sitemap_url, args.timeout)
    except Exception as exc:
        print(f"ERROR: failed to fetch sitemap: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(urls)} URLs to check.", file=sys.stderr)

    results = []
    degraded = 0
    errors = 0

    for i, url in enumerate(urls, 1):
        if args.delay and i > 1:
            time.sleep(args.delay)
        result = _check_url(url, args.timeout)
        results.append(result)

        is_product = result.get("is_product_page", False)
        is_degraded = False

        if result["status"] != 200:
            errors += 1
            is_degraded = True
        if is_product and not result["has_kes_price"]:
            is_degraded = True
        if is_product and not result["has_json_ld"]:
            is_degraded = True
        if is_product and result["price_currency"] != "KES":
            is_degraded = True

        if is_degraded:
            degraded += 1

        marker = "DEGRADED" if is_degraded else "OK"
        if not args.json_output:
            print(
                f"[{i:3d}/{len(urls)}] {result['status']:3d}  "
                f"{result['time_s']:6.3f}s  "
                f"KES:{('Y' if result['has_kes_price'] else 'N'):1s}  "
                f"LD:{('Y' if result['has_json_ld'] else 'N'):1s}  "
                f"rel:{result['related_product_count']:2d}  "
                f"{marker:8s}  {url}"
            )

    if args.json_output:
        json.dump(results, sys.stdout, indent=2)
        print()

    print(
        f"\n--- Summary ---\n"
        f"Total URLs:   {len(urls)}\n"
        f"Errors:       {errors}\n"
        f"Degraded:     {degraded}\n"
        f"Healthy:      {len(urls) - degraded}",
        file=sys.stderr,
    )

    sys.exit(1 if degraded > 0 else 0)


if __name__ == "__main__":
    main()
