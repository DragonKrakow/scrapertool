"""
Product Importer – FastAPI backend
Real scraper: searches supplier site for each product,
extracts price, stock, description, images.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup
import asyncio
import re
import random
import string
from typing import Optional, List
import urllib.parse

app = FastAPI(title="Product Importer API", version="1.0.0")

# Allow all origins so the GitHub Pages frontend can call this
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Models ──────────────────────────────────────────────────────────────────

class ProductInput(BaseModel):
    id: str = ""
    title: str
    qty: Optional[int] = None

class ScrapeRequest(BaseModel):
    products: List[ProductInput]
    site: str
    default_qty: int = 10

# ── Helpers ──────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def clean_price(text: str) -> float:
    """Extract a float price from a messy string."""
    if not text:
        return 0.0
    # Remove currency symbols, spaces, then normalise decimal
    cleaned = re.sub(r"[^\d,\.]", "", text.strip())
    # European format: 1.234,56  → 1234.56
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return 0.0

def similarity_score(query: str, found: str) -> float:
    """Very simple word-overlap similarity."""
    q_words = set(query.lower().split())
    f_words = set(found.lower().split())
    if not q_words:
        return 0.0
    overlap = len(q_words & f_words)
    return round(overlap / max(len(q_words), len(f_words)), 2)

def rand_sku(prefix: str = "") -> str:
    suffix = "".join(random.choices(string.digits, k=5))
    return f"{prefix[:3].upper()}-{suffix}" if prefix else f"SKU-{suffix}"

# ── Scraping logic ────────────────────────────────────────────────────────────

async def search_product_on_site(client: httpx.AsyncClient, base_url: str, query: str) -> dict:
    """
    Try to find a product on the supplier site.
    Strategy:
      1. Try common search URL patterns (?search=, ?q=, /search?phrase=, etc.)
      2. Parse the first result page for product info
    """
    base = base_url.rstrip("/")
    encoded_query = urllib.parse.quote_plus(query)

    search_patterns = [
        f"{base}/search?q={encoded_query}",
        f"{base}/szukaj?q={encoded_query}",
        f"{base}/?s={encoded_query}",
        f"{base}/search?phrase={encoded_query}",
        f"{base}/catalogsearch/result/?q={encoded_query}",
        f"{base}/search?keyword={encoded_query}",
    ]

    html = None
    search_url_used = None

    for url in search_patterns:
        try:
            r = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=12)
            if r.status_code == 200 and len(r.text) > 500:
                html = r.text
                search_url_used = url
                break
        except Exception:
            continue

    if not html:
        return {"status": "not_found", "source_url": "", "notes": "Site unreachable or no search endpoint found"}

    soup = BeautifulSoup(html, "html.parser")

    # ── Try to find a product link from search results ──
    # Heuristic: look for <a> tags that contain the product name words
    query_words = set(query.lower().split())
    best_link = None
    best_score = 0.0

    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        score = similarity_score(query, text)
        if score > best_score and score > 0.3:
            best_score = score
            best_link = a["href"]

    if not best_link:
        return {
            "status": "not_found",
            "source_url": search_url_used,
            "notes": "No matching product link found in search results"
        }

    # Resolve relative URL
    product_url = urllib.parse.urljoin(base, best_link)

    # ── Fetch the product page ──
    try:
        pr = await client.get(product_url, headers=HEADERS, follow_redirects=True, timeout=12)
        if pr.status_code != 200:
            return {"status": "not_found", "source_url": product_url, "notes": f"Product page returned {pr.status_code}"}
        product_html = pr.text
    except Exception as e:
        return {"status": "not_found", "source_url": product_url, "notes": str(e)}

    return parse_product_page(product_html, product_url, query, best_score)


def parse_product_page(html: str, url: str, query: str, match_score: float) -> dict:
    """Extract product fields from a product page using heuristics."""
    soup = BeautifulSoup(html, "html.parser")

    # ── Title ──
    title = ""
    for sel in ["h1.product-title", "h1.product_title", "h1[itemprop='name']", "h1"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(" ", strip=True)
            break

    # ── Price ──
    price = 0.0
    for sel in [
        "[itemprop='price']", ".price", ".product-price", ".price-box",
        ".woocommerce-Price-amount", ".price__current", ".summary .price"
    ]:
        el = soup.select_one(sel)
        if el:
            price = clean_price(el.get_text())
            if price:
                break

    # ── Stock ──
    stock_text = ""
    for sel in [
        "[itemprop='availability']", ".stock", ".availability",
        ".product-availability", ".qty-box", ".in-stock"
    ]:
        el = soup.select_one(sel)
        if el:
            stock_text = el.get_text(" ", strip=True).lower()
            break
    in_stock = ("stock" in stock_text and "out" not in stock_text) or \
               ("dostępn" in stock_text) or \
               ("available" in stock_text) or \
               (not stock_text)  # assume in stock if we can't tell

    # ── Description ──
    short_desc = ""
    long_desc = ""
    for sel in [".short-description", ".product-short-description", "[itemprop='description']"]:
        el = soup.select_one(sel)
        if el:
            short_desc = el.get_text(" ", strip=True)[:300]
            break
    for sel in ["#description", ".description", ".product-description", ".tab-content"]:
        el = soup.select_one(sel)
        if el:
            long_desc = str(el)[:2000]
            break
    if not short_desc:
        short_desc = (soup.find("meta", {"name": "description"}) or {}).get("content", "")[:300]

    # ── Images ──
    images = []
    for img in soup.select(".product-gallery img, .woocommerce-product-gallery img, [itemprop='image'], .product img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if src and not src.startswith("data:") and src not in images:
            images.append(urllib.parse.urljoin(url, src))
        if len(images) >= 5:
            break

    # ── Brand / SKU ──
    brand = ""
    for sel in ["[itemprop='brand']", ".product-brand", ".brand"]:
        el = soup.select_one(sel)
        if el:
            brand = el.get_text(" ", strip=True)
            break

    sku_el = soup.select_one("[itemprop='sku'], .sku")
    sku = sku_el.get_text(strip=True) if sku_el else ""

    # ── Category ──
    category = ""
    breadcrumb = soup.select(".breadcrumb a, .breadcrumbs a, nav[aria-label='breadcrumb'] a")
    if len(breadcrumb) >= 2:
        category = " > ".join(a.get_text(strip=True) for a in breadcrumb[1:])

    # ── Attributes ──
    attributes = {}
    for row in soup.select("table.shop_attributes tr, .product-attributes tr, .specifications tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            k = cells[0].get_text(strip=True)
            v = cells[1].get_text(strip=True)
            if k and v and len(k) < 60:
                attributes[k] = v
        if len(attributes) >= 8:
            break

    status = "ok" if match_score >= 0.85 else "review"

    return {
        "status": status,
        "match": match_score,
        "notes": "" if status == "ok" else "Partial match – please verify",
        "title": title or query,
        "brand": brand,
        "sku": sku,
        "price": price,
        "regular_price": round(price * 1.15, 2) if price else 0,
        "stock": 1 if in_stock else 0,
        "short_description": short_desc,
        "description": long_desc,
        "category": category,
        "weight": "",
        "length": "",
        "width": "",
        "height": "",
        "images": images,
        "attributes": attributes,
        "source_url": url,
        "source_site": url.split("/")[0] + "//" + url.split("/")[2] if "//" in url else url,
    }

# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "message": "Product Importer API is running"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/scrape")
async def scrape(req: ScrapeRequest):
    if not req.products:
        raise HTTPException(400, "No products provided")
    if len(req.products) > 50:
        raise HTTPException(400, "Maximum 50 products per request")

    base_url = req.site.strip().rstrip("/")
    if not base_url.startswith("http"):
        base_url = "https://" + base_url

    results = []

    async with httpx.AsyncClient(timeout=15) as client:
        tasks = [
            search_product_on_site(client, base_url, p.title)
            for p in req.products
        ]
        scraped = await asyncio.gather(*tasks, return_exceptions=True)

    for i, (product_input, result) in enumerate(zip(req.products, scraped)):
        if isinstance(result, Exception):
            result = {
                "status": "not_found",
                "source_url": "",
                "notes": str(result),
                "title": product_input.title,
                "brand": "",
                "sku": product_input.id or rand_sku(),
                "price": 0,
                "regular_price": 0,
                "stock": product_input.qty or req.default_qty,
                "short_description": "",
                "description": "",
                "category": "",
                "weight": "",
                "length": "",
                "width": "",
                "height": "",
                "images": [],
                "attributes": {},
                "match": 0,
                "source_site": base_url,
            }

        # Fill in input-provided values that override scraped data
        if not result.get("sku"):
            result["sku"] = product_input.id or rand_sku(product_input.title)
        if product_input.qty is not None:
            result["stock"] = product_input.qty
        elif not result.get("stock"):
            result["stock"] = req.default_qty
        if not result.get("ean"):
            result["ean"] = ""

        results.append(result)

    return {"results": results, "total": len(results)}
