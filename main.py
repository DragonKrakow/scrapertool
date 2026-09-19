"""
Product Importer – FastAPI backend
Mode: user pastes direct product URLs, backend fetches & parses each one.
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
import urllib.parse
from typing import Optional, List

app = FastAPI(title="Product Importer API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Models ───────────────────────────────────────────────────────────────────

class ProductInput(BaseModel):
    id: str = ""
    title: str = ""
    url: str = ""          # direct product URL
    qty: Optional[int] = None

class ScrapeRequest(BaseModel):
    products: List[ProductInput]
    site: str = ""
    default_qty: int = 10

# ── Helpers ──────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

def clean_price(text: str) -> float:
    if not text:
        return 0.0
    cleaned = re.sub(r"[^\d,\.]", "", text.strip())
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return 0.0

def rand_sku(prefix: str = "") -> str:
    suffix = "".join(random.choices(string.digits, k=5))
    return f"{prefix[:3].upper()}-{suffix}" if prefix else f"SKU-{suffix}"

# ── Search on site ────────────────────────────────────────────────────────────

async def search_on_site(client: httpx.AsyncClient, base_url: str, query: str) -> str:
    """Try common search URL patterns, return first working result URL."""
    base = base_url.rstrip("/")
    q = urllib.parse.quote_plus(query)
    patterns = [
        f"{base}/search?q={q}",
        f"{base}/?s={q}",
        f"{base}/szukaj?q={q}",
        f"{base}/search?phrase={q}",
        f"{base}/search?keyword={q}",
        f"{base}/catalogsearch/result/?q={q}",
        f"{base}/search?search={q}",
    ]
    for url in patterns:
        try:
            r = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=10)
            if r.status_code == 200 and len(r.text) > 1000:
                # find first product link
                soup = BeautifulSoup(r.text, "lxml")
                q_words = set(query.lower().split())
                best_link, best_score = None, 0.0
                for a in soup.find_all("a", href=True):
                    text = a.get_text(" ", strip=True)
                    words = set(text.lower().split())
                    if not words:
                        continue
                    score = len(q_words & words) / max(len(q_words), len(words))
                    if score > best_score and score > 0.25:
                        best_score = score
                        best_link = a["href"]
                if best_link:
                    return urllib.parse.urljoin(base, best_link), best_score
        except Exception:
            continue
    return None, 0.0

# ── Parse product page ────────────────────────────────────────────────────────

def parse_page(html: str, url: str, match: float, title_hint: str = "") -> dict:
    soup = BeautifulSoup(html, "lxml")

    # Title
    title = ""
    for sel in ["h1.product-title","h1.product_title","h1[itemprop='name']","h1"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(" ", strip=True)
            break
    if not title:
        title = title_hint

    # Price - try many selectors
    price = 0.0
    price_selectors = [
        "[itemprop='price']",
        ".price ins .woocommerce-Price-amount",
        ".woocommerce-Price-amount",
        ".price__current",
        ".product-price",
        ".price-box .price",
        ".price",
        ".summary .price",
        ".product__price",
        "span.price",
    ]
    for sel in price_selectors:
        el = soup.select_one(sel)
        if el:
            # try content attribute first (itemprop)
            p = el.get("content") or el.get_text()
            price = clean_price(p)
            if price:
                break

    # Stock
    stock_text = ""
    for sel in ["[itemprop='availability']",".stock",".availability",".in-stock",".product-availability"]:
        el = soup.select_one(sel)
        if el:
            stock_text = el.get_text(" ", strip=True).lower()
            break
    in_stock = (
        "instock" in stock_text.replace(" ","") or
        "dostępn" in stock_text or
        "available" in stock_text or
        "in stock" in stock_text or
        not stock_text
    )

    # Description
    short_desc = ""
    long_desc = ""
    for sel in [".short-description",".product-short-description","[itemprop='description']",".woocommerce-product-details__short-description"]:
        el = soup.select_one(sel)
        if el:
            short_desc = el.get_text(" ", strip=True)[:400]
            break
    if not short_desc:
        meta = soup.find("meta", {"name": "description"})
        if meta:
            short_desc = meta.get("content", "")[:400]
    for sel in ["#description",".description",".product-description",".tab-content #tab-description",".woocommerce-Tabs-panel--description"]:
        el = soup.select_one(sel)
        if el:
            long_desc = str(el)[:3000]
            break

    # Images
    images = []
    for img in soup.select(".woocommerce-product-gallery img, .product-gallery img, [itemprop='image'], .product__media img, .product-images img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or img.get("data-large_image")
        if src and not src.startswith("data:") and src not in images:
            images.append(urllib.parse.urljoin(url, src))
        if len(images) >= 5:
            break

    # Brand
    brand = ""
    for sel in ["[itemprop='brand']",".product-brand",".brand","span.brand"]:
        el = soup.select_one(sel)
        if el:
            brand = el.get_text(" ", strip=True)
            break

    # SKU
    sku_el = soup.select_one("[itemprop='sku'],.sku,.product-sku")
    sku = sku_el.get_text(strip=True) if sku_el else ""

    # Category from breadcrumb
    category = ""
    crumbs = soup.select(".breadcrumb a,.breadcrumbs a,nav[aria-label='breadcrumb'] a,.woocommerce-breadcrumb a")
    if len(crumbs) >= 2:
        category = " > ".join(a.get_text(strip=True) for a in crumbs[1:])

    # Attributes from spec table
    attributes = {}
    for row in soup.select("table.shop_attributes tr,.product-attributes tr,.specifications tr,.woocommerce-product-attributes tr"):
        cells = row.find_all(["th","td"])
        if len(cells) >= 2:
            k = cells[0].get_text(strip=True)
            v = cells[1].get_text(strip=True)
            if k and v and len(k) < 60:
                attributes[k] = v
        if len(attributes) >= 8:
            break

    # Weight/dimensions
    weight = attributes.pop("Weight","") or attributes.pop("Waga","") or attributes.pop("Gewicht","")
    
    status = "ok" if match >= 0.7 else "review"

    return {
        "status": status,
        "match": round(match, 2),
        "notes": "" if status == "ok" else "Partial match – please verify",
        "title": title,
        "brand": brand,
        "sku": sku,
        "price": price,
        "regular_price": round(price * 1.15, 2) if price else 0,
        "stock": 1 if in_stock else 0,
        "short_description": short_desc,
        "description": long_desc,
        "category": category,
        "weight": weight,
        "length": "", "width": "", "height": "",
        "images": images,
        "attributes": attributes,
        "source_url": url,
        "source_site": urllib.parse.urlparse(url).scheme + "://" + urllib.parse.urlparse(url).netloc,
    }

# ── Main scrape handler ───────────────────────────────────────────────────────

async def scrape_one(client: httpx.AsyncClient, product: ProductInput, site: str, default_qty: int) -> dict:
    url = product.url.strip()
    match = 1.0

    # If no direct URL given, try searching
    if not url and site and product.title:
        url, match = await search_on_site(client, site, product.title)

    if not url:
        return {
            "status": "not_found", "match": 0, "notes": "No URL provided and search failed",
            "title": product.title, "brand": "", "sku": product.id or rand_sku(product.title),
            "price": 0, "regular_price": 0, "stock": product.qty or default_qty,
            "short_description": "", "description": "", "category": "",
            "weight": "", "length": "", "width": "", "height": "",
            "images": [], "attributes": {}, "source_url": "", "source_site": site,
        }

    try:
        r = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=15)
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}")
        result = parse_page(r.text, url, match, product.title)
    except Exception as e:
        result = {
            "status": "not_found", "match": 0, "notes": str(e),
            "title": product.title, "brand": "", "sku": product.id or rand_sku(product.title),
            "price": 0, "regular_price": 0, "stock": product.qty or default_qty,
            "short_description": "", "description": "", "category": "",
            "weight": "", "length": "", "width": "", "height": "",
            "images": [], "attributes": {}, "source_url": url, "source_site": site,
        }

    # Override with user-supplied values
    if not result.get("sku"):
        result["sku"] = product.id or rand_sku(product.title or result.get("title",""))
    if product.qty is not None:
        result["stock"] = product.qty
    elif not result.get("stock"):
        result["stock"] = default_qty

    return result

# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "message": "Product Importer API v2"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/scrape")
async def scrape(req: ScrapeRequest):
    if not req.products:
        raise HTTPException(400, "No products provided")
    if len(req.products) > 50:
        raise HTTPException(400, "Max 50 products per request")

    async with httpx.AsyncClient(timeout=20) as client:
        tasks = [scrape_one(client, p, req.site, req.default_qty) for p in req.products]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    final = []
    for i, (p, r) in enumerate(zip(req.products, results)):
        if isinstance(r, Exception):
            r = {
                "status": "not_found", "match": 0, "notes": str(r),
                "title": p.title, "brand": "", "sku": p.id or rand_sku(p.title),
                "price": 0, "regular_price": 0, "stock": p.qty or req.default_qty,
                "short_description": "", "description": "", "category": "",
                "weight": "", "length": "", "width": "", "height": "",
                "images": [], "attributes": {}, "source_url": "", "source_site": req.site,
            }
        final.append(r)

    return {"results": final, "total": len(final)}
