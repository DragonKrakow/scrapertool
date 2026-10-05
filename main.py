from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import unicodedata
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse, urlencode

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


APP_VERSION = "2.0.0"
MAX_PRODUCTS_PER_REQUEST = 30
MAX_CANDIDATES = 25
MAX_IMAGES = 20
MAX_HTML_BYTES = 4_000_000
REQUEST_TIMEOUT = 18.0
CRAWL_TIMEOUT = 10.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0 Safari/537.36 Scrapertool/2.0"
)

PLATFORM_NAMES = {
    "auto": "Auto-detect",
    "prestashop": "PrestaShop",
    "woocommerce": "WooCommerce",
    "shopify": "Shopify",
    "magento": "Magento",
    "opencart": "OpenCart",
    "shopware": "Shopware",
    "bigcommerce": "BigCommerce",
    "generic": "Generic website",
}


class ProductInput(BaseModel):
    id: str = ""
    title: str = ""
    qty: int | None = Field(default=None, ge=1, le=100000)
    url: str = ""


class ScrapeRequest(BaseModel):
    products: list[ProductInput] = Field(default_factory=list)
    site: str = ""
    default_qty: int = Field(default=1, ge=1, le=100000)
    platform: str = "auto"


app = FastAPI(title="Scrapertool API", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(clean_text(x) for x in value if clean_text(x))
    if isinstance(value, dict):
        return clean_text(value.get("name") or value.get("value") or "")
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(value: Any) -> set[str]:
    return {x for x in normalize_text(value).split() if len(x) > 1}


def absolute_url(base: str, value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    return urljoin(base, value)


def valid_http_url(value: str) -> bool:
    try:
        p = urlparse(value)
        return p.scheme in {"http", "https"} and bool(p.netloc)
    except Exception:
        return False


def public_host(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"}:
        return False
    if host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                return False
    except (socket.gaierror, ValueError, OSError):
        # Let the HTTP request report DNS errors for public-looking hosts.
        pass
    return True


def validate_target_url(value: str) -> str:
    if not valid_http_url(value):
        raise ValueError("Only http:// and https:// URLs are supported.")
    p = urlparse(value)
    if not public_host(p.hostname or ""):
        raise ValueError("Private/local network URLs are not allowed.")
    return value


def site_root(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    p = urlparse(value)
    return f"{p.scheme}://{p.netloc}/"


def first_nonempty(*values: Any) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def parse_price(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = clean_text(value)
    if not raw:
        return None
    raw = re.sub(r"[^\d,.\-]", "", raw)
    if not raw:
        return None
    # Handle 1.234,56 / 1,234.56 / 12,99 / 12.99
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        tail = raw.rsplit(",", 1)[-1]
        raw = raw.replace(",", ".") if len(tail) in {1, 2} else raw.replace(",", "")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def normalize_image(url: str, base: str) -> str:
    url = absolute_url(base, url)
    if not valid_http_url(url):
        return ""
    return url


def unique_keep_order(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        value = clean_text(value)
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def parse_json_ld(soup: BeautifulSoup) -> list[Any]:
    found = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            found.append(json.loads(raw))
        except Exception:
            # Some shops wrap JSON-LD with HTML comments or have minor trailing junk.
            raw2 = raw.strip().strip("<!--").strip("-->").strip()
            try:
                found.append(json.loads(raw2))
            except Exception:
                continue
    return found


def flatten_jsonld(node: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(value: Any):
        if isinstance(value, dict):
            if "@graph" in value:
                walk(value["@graph"])
            if isinstance(value.get("@type"), list):
                types = value["@type"]
            else:
                types = [value.get("@type")]
            if any(str(t).lower() in {"product", "productgroup"} for t in types if t):
                out.append(value)
            if value.get("item") and isinstance(value["item"], (dict, list)):
                walk(value["item"])
            if value.get("itemListElement"):
                walk(value["itemListElement"])
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for item in node if isinstance(node, list) else [node]:
        walk(item)
    return out


def meta_content(soup: BeautifulSoup, *names: str) -> str:
    wanted = {x.lower() for x in names}
    for tag in soup.find_all("meta"):
        key = first_nonempty(tag.get("property"), tag.get("name"), tag.get("itemprop")).lower()
        if key in wanted:
            return clean_text(tag.get("content"))
    return ""


def first_selector_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            text = clean_text(el.get("content") if el.name == "meta" else el.get_text(" ", strip=True))
            if text:
                return text
    return ""


def extract_brand(value: Any) -> str:
    if isinstance(value, dict):
        return first_nonempty(value.get("name"), value.get("brand"))
    return clean_text(value)


def extract_offer(offers: Any) -> dict[str, Any]:
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    return offers if isinstance(offers, dict) else {}


def availability_label(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    last = text.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
    low = last.lower()
    if "instock" in low or "in stock" in low:
        return "In stock"
    if "outofstock" in low or "out of stock" in low:
        return "Out of stock"
    if "preorder" in low or "pre order" in low:
        return "Pre-order"
    if "backorder" in low or "back order" in low:
        return "Backorder"
    return last.strip().title()


def extract_attributes(soup: BeautifulSoup) -> dict[str, str]:
    attrs: dict[str, str] = {}

    for row in soup.select("table tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            key = clean_text(cells[0].get_text(" ", strip=True))
            value = clean_text(cells[1].get_text(" ", strip=True))
            if key and value and len(key) <= 100 and len(value) <= 500:
                attrs.setdefault(key, value)

    for item in soup.select(
        "[class*='attribute'], [class*='specification'], [class*='product-attribute'], "
        "[data-attribute], [itemprop='additionalProperty']"
    )[:80]:
        name = first_nonempty(
            item.get("data-attribute"),
            item.select_one(".name").get_text(" ", strip=True) if item.select_one(".name") else "",
            item.get("name"),
        )
        value = first_nonempty(
            item.select_one(".value").get_text(" ", strip=True) if item.select_one(".value") else "",
            item.get("content"),
        )
        if name and value:
            attrs.setdefault(name, value)

    return dict(list(attrs.items())[:50])


def extract_product_from_html(
    html: str,
    url: str,
    requested_title: str = "",
    platform: str = "generic",
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    product_nodes = []
    for block in parse_json_ld(soup):
        product_nodes.extend(flatten_jsonld(block))

    product = product_nodes[0] if product_nodes else {}
    offers = extract_offer(product.get("offers"))

    title = first_nonempty(
        product.get("name"),
        meta_content(soup, "og:title", "twitter:title"),
        first_selector_text(soup, ["h1", "[itemprop='name']", ".product-title", ".product-name"]),
        clean_text(soup.title.get_text()) if soup.title else "",
    )

    description = first_nonempty(
        product.get("description"),
        meta_content(soup, "og:description", "description"),
        first_selector_text(
            soup,
            [
                "[itemprop='description']",
                ".product-description",
                ".product-description-content",
                "#description",
                ".description",
                ".short-description",
            ],
        ),
    )

    short_description = first_nonempty(
        product.get("shortDescription"),
        product.get("short_description"),
        first_selector_text(
            soup,
            [
                ".short-description",
                "[itemprop='description']",
                ".product-short-description",
            ],
        ),
        description[:500] if description else "",
    )

    brand = extract_brand(
        product.get("brand")
        or meta_content(soup, "product:brand", "brand")
        or first_selector_text(soup, ["[itemprop='brand']", ".brand", ".product-brand"])
    )

    sku = first_nonempty(
        product.get("sku"),
        product.get("mpn"),
        meta_content(soup, "product:retailer_item_id", "sku"),
        first_selector_text(soup, ["[itemprop='sku']", ".sku", ".product-reference", ".reference"]),
    )

    gtin = first_nonempty(
        product.get("gtin13"),
        product.get("gtin12"),
        product.get("gtin14"),
        product.get("gtin8"),
        product.get("gtin"),
        meta_content(soup, "product:gtin", "gtin", "gtin13", "gtin12", "gtin14", "gtin8"),
    )

    price = parse_price(
        first_nonempty(
            offers.get("price"),
            product.get("price"),
            meta_content(soup, "product:price:amount", "price"),
            first_selector_text(soup, ["[itemprop='price']", ".price", ".product-price", ".current-price"]),
        )
    )

    regular_price = parse_price(
        first_nonempty(
            offers.get("highPrice") if offers.get("lowPrice") else "",
            meta_content(soup, "product:original_price"),
            first_selector_text(soup, [".regular-price", ".old-price", ".was-price", "del .amount"]),
        )
    )

    currency = first_nonempty(
        offers.get("priceCurrency"),
        product.get("priceCurrency"),
        meta_content(soup, "product:price:currency", "priceCurrency"),
    ).upper()

    availability = availability_label(
        first_nonempty(
            offers.get("availability"),
            product.get("availability"),
            meta_content(soup, "availability"),
            first_selector_text(soup, [".availability", ".stock", ".product-stock", "[itemprop='availability']"]),
        )
    )

    category = first_nonempty(
        product.get("category"),
        meta_content(soup, "product:category"),
        first_selector_text(
            soup,
            [
                "[itemprop='category']",
                ".breadcrumb li:last-child",
                ".breadcrumbs li:last-child",
                ".breadcrumb a:last-child",
                ".category",
            ],
        ),
    )

    images: list[str] = []
    raw_images = product.get("image", [])
    if isinstance(raw_images, str):
        raw_images = [raw_images]
    if isinstance(raw_images, list):
        images.extend(normalize_image(str(x), url) for x in raw_images)

    for name in ("og:image", "twitter:image"):
        images.append(normalize_image(meta_content(soup, name), url))

    for img in soup.select(
        "img[itemprop='image'], .product img, .product-gallery img, .product-images img, "
        ".woocommerce-product-gallery img, .gallery img"
    ):
        src = first_nonempty(img.get("src"), img.get("data-src"), img.get("data-original"))
        if src:
            images.append(normalize_image(src, url))

    images = unique_keep_order(images)[:MAX_IMAGES]

    attrs = extract_attributes(soup)

    # Breadcrumb/category fallback.
    if not category:
        crumbs = [
            clean_text(x.get_text(" ", strip=True))
            for x in soup.select(".breadcrumb a, .breadcrumbs a")
        ]
        if crumbs:
            category = crumbs[-1]

    # Try common textual stock signals only when structured stock was absent.
    if not availability:
        body_text = clean_text(soup.get_text(" ", strip=True)).lower()
        if re.search(r"\bin stock\b|\binstock\b|\bdisponibile\b|\bdostępny\b", body_text):
            availability = "In stock"
        elif re.search(r"\bout of stock\b|\boutofstock\b|\besaurito\b|\bbrak w magazynie\b", body_text):
            availability = "Out of stock"

    source_title = title or requested_title
    return {
        "status": "ok" if source_title else "partial",
        "title": source_title,
        "brand": brand,
        "sku": sku,
        "ean": gtin,
        "gtin": gtin,
        "price": price,
        "regular_price": regular_price,
        "currency": currency,
        "availability": availability,
        "stock": availability,
        "description": description,
        "short_description": short_description,
        "category": category,
        "images": images,
        "attributes": attrs,
        "source_url": url,
        "source_site": urlparse(url).netloc,
        "platform": platform,
    }


def platform_score(html: str, url: str) -> dict[str, int]:
    text = html[:1_500_000].lower()
    scores = {
        "prestashop": 0,
        "woocommerce": 0,
        "shopify": 0,
        "magento": 0,
        "opencart": 0,
        "shopware": 0,
        "bigcommerce": 0,
    }

    signatures = {
        "prestashop": [
            "prestashop",
            "prestashop_version",
            "id_product",
            "product-reference",
            "product-prices",
        ],
        "woocommerce": [
            "woocommerce",
            "wp-content/plugins/woocommerce",
            "wc-ajax",
            "wp-json/wc/store",
            "woocommerce-product-gallery",
        ],
        "shopify": [
            "shopify",
            "cdn.shopify.com",
            "myshopify.com",
            "shopify.theme",
        ],
        "magento": [
            "magento",
            "mage/cookies",
            "requirejs-config.js",
            "catalog-product-view",
            "magento-init",
        ],
        "opencart": [
            "opencart",
            "catalog/view/theme",
            "route=product/",
            "oc-",
        ],
        "shopware": [
            "shopware",
            "store-api",
            "data-shopware",
            "shopware.init",
        ],
        "bigcommerce": [
            "bigcommerce",
            "cdn11.bigcommerce.com",
            "stencil-utils",
            "bcapp",
        ],
    }

    for platform, needles in signatures.items():
        for needle in needles:
            if needle in text:
                scores[platform] += 1

    host = urlparse(url).netloc.lower()
    if "myshopify.com" in host:
        scores["shopify"] += 5
    return scores


def detect_platform(html: str, url: str, requested: str = "auto") -> str:
    requested = (requested or "auto").lower().strip()
    if requested in PLATFORM_NAMES and requested != "auto":
        return requested
    scores = platform_score(html, url)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "generic"


async def fetch_text(client: httpx.AsyncClient, url: str, timeout: float = REQUEST_TIMEOUT) -> tuple[str, str]:
    validate_target_url(url)
    response = await client.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        return "", str(response.url)
    content = response.content[:MAX_HTML_BYTES]
    return content.decode(response.encoding or "utf-8", errors="replace"), str(response.url)


def candidate_links(html: str, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for a in soup.find_all("a", href=True):
        href = normalize_image(a.get("href"), base)
        if not href or urlparse(href).netloc != urlparse(base).netloc:
            continue
        label = clean_text(a.get_text(" ", strip=True))
        if not label:
            label = clean_text(a.get("title"))
        results.append((href, label))
    return results


def score_candidate(url: str, label: str, requested: str, identifier: str = "") -> float:
    target = normalize_text(requested)
    candidate = normalize_text(label)
    slug = normalize_text(urlparse(url).path.replace("-", " ").replace("_", " "))
    if not target:
        return 0.0
    target_tokens = tokens(target)
    cand_tokens = tokens(candidate)
    slug_tokens = tokens(slug)
    overlap_label = len(target_tokens & cand_tokens) / max(1, len(target_tokens))
    overlap_slug = len(target_tokens & slug_tokens) / max(1, len(target_tokens))
    seq_label = SequenceMatcher(None, target, candidate).ratio() if candidate else 0.0
    seq_slug = SequenceMatcher(None, target, slug).ratio() if slug else 0.0
    fuzzy_label = (
        sum(max(SequenceMatcher(None, a, b).ratio() for b in cand_tokens)
            for a in target_tokens) / len(target_tokens)
        if target_tokens and cand_tokens else 0.0
    )
    fuzzy_slug = (
        sum(max(SequenceMatcher(None, a, b).ratio() for b in slug_tokens)
            for a in target_tokens) / len(target_tokens)
        if target_tokens and slug_tokens else 0.0
    )
    score = (
        overlap_label * 0.25 + overlap_slug * 0.18 +
        seq_label * 0.12 + seq_slug * 0.08 +
        fuzzy_label * 0.22 + fuzzy_slug * 0.15
    )
    if identifier:
        ident = normalize_text(identifier)
        if ident and ident in normalize_text(url + " " + label):
            score += 0.20
    low_path = urlparse(url).path.lower()
    if any(h in low_path for h in ("/product/", "/products/", "/catalog/", "/p/", "/item/", ".html")):
        score += 0.04
    return min(score, 1.0)


def build_search_urls(site: str, query: str, platform: str) -> list[str]:
    q = quote_plus(query)
    encoded = quote_plus(query.replace(" ", "-"))
    urls = []

    if platform == "prestashop":
        urls += [
            f"{site}search?controller=search&s={q}",
            f"{site}search?s={q}",
        ]
    elif platform == "woocommerce":
        urls += [
            f"{site}?s={q}&post_type=product",
            f"{site}wp-json/wc/store/v1/products?search={q}&per_page=20",
        ]
    elif platform == "shopify":
        urls += [
            f"{site}search?q={q}",
            f"{site}products.json?limit=20&title={q}",
        ]
    elif platform == "magento":
        urls += [f"{site}catalogsearch/result/?q={q}"]
    elif platform == "opencart":
        urls += [f"{site}index.php?route=product/search&search={q}"]
    elif platform == "shopware":
        urls += [f"{site}search?search={q}"]
    elif platform == "bigcommerce":
        urls += [f"{site}search.php?search_query={q}"]

    # Generic/common routes are always tried after platform routes.
    urls += [
        f"{site}search?q={q}",
        f"{site}search?query={q}",
        f"{site}?s={q}",
        f"{site}?search={q}",
        f"{site}search/{encoded}",
    ]

    return unique_keep_order(urls)


def json_api_candidates(data: Any, base: str, requested: str, identifier: str) -> list[tuple[str, str, float]]:
    candidates = []

    def walk(obj: Any):
        if isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            title = first_nonempty(obj.get("name"), obj.get("title"), obj.get("productName"))
            link = first_nonempty(obj.get("permalink"), obj.get("url"), obj.get("link"))
            if link:
                link = absolute_url(base, link)
                if valid_http_url(link):
                    candidates.append(
                        (link, title, score_candidate(link, title, requested, identifier))
                    )
            for key in ("products", "items", "results", "data"):
                if key in obj:
                    walk(obj[key])

    walk(data)
    return candidates


async def discover_candidates(
    client: httpx.AsyncClient, site: str, requested: str, identifier: str, platform: str
) -> tuple[list[tuple[str, float]], list[str], list[str]]:
    warnings, attempted = [], []
    all_candidates: dict[str, float] = {}
    platforms = ([platform] if platform != "auto" else
                 ["prestashop","woocommerce","shopify","magento","opencart","shopware","bigcommerce","generic"])
    queries = [requested] + ([identifier] if identifier and identifier != requested else [])
    words = [x for x in clean_text(requested).split() if len(x) >= 3]
    if len(words) > 1:
        queries.extend(words[:3])

    search_urls = []
    for q in queries:
        for plat in platforms:
            search_urls.extend(build_search_urls(site, q, plat)[:6])

    for search_url in unique_keep_order(search_urls)[:45]:
        attempted.append(search_url)
        try:
            validate_target_url(search_url)
            response = await client.get(search_url, timeout=CRAWL_TIMEOUT, follow_redirects=True)
            if response.status_code >= 400:
                continue
            ctype = response.headers.get("content-type", "").lower()
            if "json" in ctype or response.text.lstrip().startswith(("{", "[")):
                try:
                    data = response.json()
                    for link, label, score in json_api_candidates(data, str(response.url), requested, identifier):
                        all_candidates[link] = max(all_candidates.get(link, 0), score + 0.12)
                    continue
                except Exception:
                    pass
            html = response.content[:MAX_HTML_BYTES].decode(response.encoding or "utf-8", errors="replace")
            detected = detect_platform(html, str(response.url), "auto")
            for link, label in candidate_links(html, str(response.url)):
                score = score_candidate(link, label, requested, identifier)
                if score >= 0.20:
                    all_candidates[link] = max(all_candidates.get(link, 0), score)
            soup = BeautifulSoup(html, "html.parser")
            for block in parse_json_ld(soup):
                for node in flatten_jsonld(block):
                    link = absolute_url(str(response.url), first_nonempty(node.get("url")))
                    if link:
                        score = score_candidate(link, node.get("name", ""), requested, identifier)
                        if score >= 0.18:
                            all_candidates[link] = max(all_candidates.get(link, 0), score + 0.08)
            if detected != "generic":
                warnings.append(f"Detected {PLATFORM_NAMES.get(detected, detected)}.")
        except Exception as exc:
            if len(warnings) < 5:
                warnings.append(f"Search attempt failed: {type(exc).__name__}")

    try:
        robots_url = site + "robots.txt"
        attempted.append(robots_url)
        response = await client.get(robots_url, timeout=CRAWL_TIMEOUT, follow_redirects=True)
        if response.status_code < 400:
            sitemap_urls = re.findall(r"(?im)^\s*Sitemap:\s*(\S+)", response.text) or [site + "sitemap.xml"]
            for sitemap in sitemap_urls[:3]:
                attempted.append(sitemap)
                try:
                    sm = await client.get(sitemap, timeout=CRAWL_TIMEOUT, follow_redirects=True)
                    if sm.status_code >= 400:
                        continue
                    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", sm.text, flags=re.I | re.S)
                    for loc in locs[:3000]:
                        loc = clean_text(loc)
                        if urlparse(loc).netloc != urlparse(site).netloc:
                            continue
                        score = score_candidate(loc, loc, requested, identifier)
                        if score >= 0.30:
                            all_candidates[loc] = max(all_candidates.get(loc, 0), score * 0.95)
                except Exception as exc:
                    if len(warnings) < 5:
                        warnings.append(f"Sitemap attempt failed: {type(exc).__name__}")
    except Exception as exc:
        warnings.append(f"Sitemap discovery failed: {type(exc).__name__}")

    return sorted(all_candidates.items(), key=lambda x: x[1], reverse=True)[:MAX_CANDIDATES], warnings, attempted


def fallback_sku(title: str, url: str) -> str:
    import hashlib
    digest = hashlib.sha1(f"{title}|{url}".encode("utf-8")).hexdigest()[:10].upper()
    return f"SCR-{digest}"


async def scrape_one(
    client: httpx.AsyncClient,
    item: ProductInput,
    site: str,
    default_qty: int,
    requested_platform: str,
) -> dict[str, Any]:
    title = clean_text(item.title)
    identifier = clean_text(item.id)
    qty = item.qty or default_qty

    direct_url = clean_text(item.url)
    warnings: list[str] = []
    match_score = 1.0
    match_reason = "Direct product URL"

    try:
        if direct_url:
            url = validate_target_url(direct_url)
            html, final_url = await fetch_text(client, url)
            platform = detect_platform(html, final_url, requested_platform)
            result = extract_product_from_html(html, final_url, title, platform)
        else:
            if not site:
                raise ValueError("Supplier website is required when using title search.")
            root = site_root(site)
            validate_target_url(root)

            # Do not make the supplier homepage a hard dependency. Some shops have
            # slow/WAF-protected homepages while their search/product URLs work.
            platform = requested_platform
            candidates, discover_warnings, attempted = await discover_candidates(
                client, root, title or identifier, identifier, platform
            )
            warnings.extend(discover_warnings)

            if not candidates:
                raise ValueError(
                    "No product candidate was found after trying search endpoints "
                    "and sitemap. Try the exact supplier title, SKU/EAN, or Direct URL."
                )

            best_url, match_score = candidates[0]
            html, final_url = await fetch_text(client, best_url)
            detected_from_product = detect_platform(html, final_url, requested_platform)
            platform = detected_from_product or platform
            result = extract_product_from_html(html, final_url, title, platform)

            extracted_title = result.get("title", "")
            title_score = SequenceMatcher(
                None, normalize_text(title), normalize_text(extracted_title)
            ).ratio() if title and extracted_title else 0.0
            match_score = min(1.0, (match_score * 0.65) + (title_score * 0.35))
            match_reason = f"Best title match ({round(match_score * 100)}%)"

        if not result.get("title"):
            result["title"] = title

        if not result.get("sku"):
            result["sku"] = fallback_sku(result.get("title") or title, result.get("source_url", ""))

        result["quantity"] = qty
        result["requested_id"] = identifier
        result["requested_title"] = title
        result["match"] = round(match_score, 4)
        result["match_percent"] = round(match_score * 100)
        result["match_reason"] = match_reason
        result["platform"] = platform
        result["platform_name"] = PLATFORM_NAMES.get(platform, platform)
        result["warnings"] = warnings
        if not direct_url:
            result["debug"] = {
                "requested_title": title,
                "requested_id": identifier,
                "candidate_count": len(candidates) if "candidates" in locals() else 0,
                "attempted_search_count": len(attempted) if "attempted" in locals() else 0,
                "top_candidates": [
                    {"url": u, "score": round(sc, 4)}
                    for u, sc in (candidates[:5] if "candidates" in locals() else [])
                ],
            }

        if match_score < 0.55 and not direct_url:
            result["status"] = "review"
            warnings.append("Low-confidence title match; please review the source URL.")
        elif not result.get("price") and not result.get("images"):
            result["status"] = "partial"
            warnings.append("Only limited product data could be extracted.")

        return result

    except Exception as exc:
        return {
            "status": "error",
            "title": title,
            "requested_id": identifier,
            "quantity": qty,
            "error": clean_text(exc),
            "source_site": urlparse(site_root(site)).netloc if site else "",
            "platform": requested_platform,
            "platform_name": PLATFORM_NAMES.get(requested_platform, requested_platform),
            "warnings": warnings,
            "debug": {
                "requested_title": title,
                "requested_id": identifier,
                "site": site,
                "platform": requested_platform,
            },
        }


@app.get("/")
async def root():
    return {
        "name": "Scrapertool API",
        "version": APP_VERSION,
        "status": "online",
        "platforms": PLATFORM_NAMES,
        "features": [
            "auto platform detection",
            "title search",
            "direct URL scraping",
            "JSON-LD extraction",
            "OpenGraph/meta fallback",
            "bounded sitemap discovery",
            "fuzzy product matching",
        ],
    }


@app.get("/health")
async def health():
    return {"status": "awake", "message": "Backend is running!", "version": APP_VERSION}


@app.post("/scrape")
async def scrape(request: ScrapeRequest):
    if not request.products:
        raise HTTPException(status_code=400, detail="Add at least one product.")

    if len(request.products) > MAX_PRODUCTS_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_PRODUCTS_PER_REQUEST} products per request.",
        )

    requested_platform = (request.platform or "auto").lower().strip()
    if requested_platform not in PLATFORM_NAMES:
        raise HTTPException(status_code=400, detail="Unknown platform option.")

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"},
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    ) as client:
        semaphore = asyncio.Semaphore(4)

        async def run(item: ProductInput):
            async with semaphore:
                return await scrape_one(
                    client,
                    item,
                    request.site,
                    request.default_qty,
                    requested_platform,
                )

        results = await asyncio.gather(*(run(item) for item in request.products))

    ok = sum(1 for x in results if x.get("status") == "ok")
    review = sum(1 for x in results if x.get("status") == "review")
    errors = sum(1 for x in results if x.get("status") == "error")

    return {
        "status": "ok",
        "version": APP_VERSION,
        "requested_platform": requested_platform,
        "results": results,
        "stats": {
            "total": len(results),
            "ok": ok,
            "review": review,
            "errors": errors,
        },
    }
