from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, quote_plus
from typing import List, Optional, Dict, Any
import json
import re
import hashlib


app = FastAPI(
    title="Product Importer API",
    version="2.0.0"
)


# ---------------------------------------------------------
# CORS
# ---------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------
# MODELS
# ---------------------------------------------------------

class ProductInput(BaseModel):
    id: Optional[str] = ""
    title: Optional[str] = ""
    url: Optional[str] = ""
    qty: Optional[int] = None


class ScrapeRequest(BaseModel):
    products: List[ProductInput]
    site: Optional[str] = ""
    default_qty: int = 10


# ---------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------

@app.get("/health")
def health_check():
    return {
        "status": "awake",
        "message": "Backend is running!"
    }


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,it-IT;q=0.8,pl;q=0.7",
    "Cache-Control": "no-cache",
}


def clean_text(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, list):
        value = " ".join(str(x) for x in value)

    if isinstance(value, dict):
        value = value.get("name") or value.get("value") or ""

    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def absolute_url(url: str, base_url: str) -> str:
    if not url:
        return ""

    url = str(url).strip()

    if url.startswith("//"):
        parsed = urlparse(base_url)
        return f"{parsed.scheme}:{url}"

    return urljoin(base_url, url)


def unique_list(items: List[str]) -> List[str]:
    result = []
    seen = set()

    for item in items:
        if not item:
            continue

        item = item.strip()

        if not item:
            continue

        if item not in seen:
            seen.add(item)
            result.append(item)

    return result


def parse_price(value: Any) -> float:
    """
    Handles examples such as:

    6.99
    6,99
    6,99 €
    €6.99
    1.299,99 €
    1,299.99
    """

    if value is None:
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()

    if not text:
        return 0.0

    # Remove spaces and currency symbols but retain separators
    text = text.replace("\xa0", " ")
    text = re.sub(r"[^\d,.\-]", "", text)

    if not text:
        return 0.0

    # Both comma and dot exist.
    if "," in text and "." in text:

        # European format:
        # 1.299,99
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "")
            text = text.replace(",", ".")

        # US format:
        # 1,299.99
        else:
            text = text.replace(",", "")

    elif "," in text:
        # 6,99 -> 6.99
        if len(text.split(",")[-1]) <= 2:
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    elif text.count(".") > 1:
        # 1.299.99 -> 1299.99
        parts = text.split(".")
        text = "".join(parts[:-1]) + "." + parts[-1]

    try:
        return float(text)
    except Exception:
        return 0.0


def get_meta_content(soup: BeautifulSoup, **attrs) -> str:
    tag = soup.find("meta", attrs=attrs)

    if tag:
        return clean_text(tag.get("content", ""))

    return ""


# ---------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------

def extract_json_ld(soup: BeautifulSoup) -> List[Any]:
    data = []

    scripts = soup.find_all(
        "script",
        attrs={"type": re.compile(r"application/ld\+json", re.I)}
    )

    for script in scripts:
        raw = script.string or script.get_text()

        if not raw:
            continue

        raw = raw.strip()

        # Remove accidental HTML comments
        raw = re.sub(r"^\s*<!--", "", raw)
        raw = re.sub(r"-->\s*$", "", raw)

        try:
            parsed = json.loads(raw)

            if isinstance(parsed, list):
                data.extend(parsed)
            else:
                data.append(parsed)

        except Exception:
            # Some websites contain multiple JSON objects or
            # slightly malformed JSON-LD. Ignore safely.
            continue

    return data


def find_product_schema(data: Any) -> Optional[Dict[str, Any]]:
    """
    Recursively finds a Schema.org Product object.
    """

    if isinstance(data, dict):

        item_type = data.get("@type")

        if isinstance(item_type, list):
            types = [str(x).lower() for x in item_type]
        else:
            types = [str(item_type).lower()]

        if "product" in types:
            return data

        # @graph
        if "@graph" in data:
            found = find_product_schema(data["@graph"])

            if found:
                return found

        for value in data.values():
            found = find_product_schema(value)

            if found:
                return found

    elif isinstance(data, list):

        for item in data:
            found = find_product_schema(item)

            if found:
                return found

    return None


# ---------------------------------------------------------
# DESCRIPTION
# ---------------------------------------------------------

def extract_description(soup: BeautifulSoup, product_schema: Optional[Dict[str, Any]]) -> str:

    if product_schema:
        description = clean_text(
            product_schema.get("description")
        )

        if description:
            return description

    # Standard meta description
    description = get_meta_content(
        soup,
        attrs={"name": re.compile("^description$", re.I)}
    )

    if description:
        return description

    description = get_meta_content(
        soup,
        attrs={"property": "og:description"}
    )

    if description:
        return description

    # Common product description classes
    selectors = [
        ".product-description",
        ".product-description-wrapper",
        "#description",
        ".description",
        ".product-info-description",
        ".woocommerce-product-details__short-description",
        ".short-description",
        "[itemprop='description']",
    ]

    for selector in selectors:

        element = soup.select_one(selector)

        if element:
            text = clean_text(element)

            if len(text) > 20:
                return text

    return ""


# ---------------------------------------------------------
# TITLE
# ---------------------------------------------------------

def extract_title(
    soup: BeautifulSoup,
    product_schema: Optional[Dict[str, Any]],
    supplied_title: str = ""
) -> str:

    if product_schema:
        title = clean_text(product_schema.get("name"))

        if title:
            return title

    # H1 is usually the best HTML fallback
    h1 = soup.find("h1")

    if h1:
        title = clean_text(h1)

        if title:
            return title

    title = get_meta_content(
        soup,
        attrs={"property": "og:title"}
    )

    if title:
        return title

    if soup.title:
        title = clean_text(soup.title)

        # Remove common suffixes
        title = re.sub(
            r"\s*[|\-–]\s*(Benail|.*shop.*)$",
            "",
            title,
            flags=re.I
        )

        if title:
            return title

    return supplied_title or "Imported Product"


# ---------------------------------------------------------
# BRAND
# ---------------------------------------------------------

def extract_brand(
    soup: BeautifulSoup,
    product_schema: Optional[Dict[str, Any]]
) -> str:

    if product_schema:

        brand = product_schema.get("brand")

        if isinstance(brand, dict):
            brand = brand.get("name")

        brand = clean_text(brand)

        if brand:
            return brand

    # Meta brand
    for attrs in [
        {"property": "product:brand"},
        {"name": "brand"},
        {"itemprop": "brand"},
    ]:

        value = get_meta_content(soup, attrs=attrs)

        if value:
            return value

    # Common HTML selectors
    selectors = [
        "[itemprop='brand']",
        ".brand",
        ".product-brand",
        ".manufacturer",
        ".product-manufacturer",
    ]

    for selector in selectors:

        element = soup.select_one(selector)

        if element:
            value = clean_text(element)

            if value:
                return value

    return ""


# ---------------------------------------------------------
# SKU / EAN
# ---------------------------------------------------------

def extract_sku(
    soup: BeautifulSoup,
    product_schema: Optional[Dict[str, Any]]
) -> str:

    if product_schema:

        sku = clean_text(product_schema.get("sku"))

        if sku:
            return sku

    for attrs in [
        {"itemprop": "sku"},
        {"name": "sku"},
        {"property": "product:sku"},
    ]:

        value = get_meta_content(soup, attrs=attrs)

        if value:
            return value

    selectors = [
        "[itemprop='sku']",
        ".sku",
        ".product-reference",
        ".reference",
        ".product-code",
        ".product-ref",
    ]

    for selector in selectors:

        element = soup.select_one(selector)

        if element:

            value = clean_text(element)

            if value:
                return value

    # Search visible text for common reference labels
    text = soup.get_text(" ", strip=True)

    patterns = [
        r"(?:Riferimento|Reference|SKU|Codice prodotto|Product code)"
        r"\s*[:#]?\s*([A-Za-z0-9_\-./]+)",

        r"(?:Articolo|Article)"
        r"\s*[:#]?\s*([A-Za-z0-9_\-./]+)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            flags=re.I
        )

        if match:
            return match.group(1).strip()

    return ""


def extract_gtin(
    soup: BeautifulSoup,
    product_schema: Optional[Dict[str, Any]]
) -> str:

    if product_schema:

        for key in [
            "gtin",
            "gtin8",
            "gtin12",
            "gtin13",
            "gtin14",
            "ean",
        ]:

            value = clean_text(product_schema.get(key))

            if value:
                return value

    for attrs in [
        {"itemprop": "gtin"},
        {"itemprop": "gtin13"},
        {"itemprop": "gtin12"},
        {"name": "gtin"},
        {"name": "ean"},
    ]:

        value = get_meta_content(soup, attrs=attrs)

        if value:
            return value

    return ""


# ---------------------------------------------------------
# PRICE
# ---------------------------------------------------------

def extract_price(
    soup: BeautifulSoup,
    product_schema: Optional[Dict[str, Any]]
):

    price = 0.0
    regular_price = 0.0
    currency = ""

    # Best option: Schema.org
    if product_schema:

        offers = product_schema.get("offers")

        if isinstance(offers, list):
            offers = offers[0] if offers else None

        if isinstance(offers, dict):

            price = parse_price(
                offers.get("price")
            )

            currency = clean_text(
                offers.get("priceCurrency")
            )

            # Sometimes there is a lowPrice instead
            if price == 0:
                price = parse_price(
                    offers.get("lowPrice")
                )

    # Meta price
    if price == 0:

        meta_price = get_meta_content(
            soup,
            attrs={"property": "product:price:amount"}
        )

        price = parse_price(meta_price)

    if not currency:

        currency = get_meta_content(
            soup,
            attrs={"property": "product:price:currency"}
        )

    # Common HTML selectors
    price_selectors = [
        "[itemprop='price']",
        ".current-price",
        ".product-price",
        ".price",
        ".current_price",
        ".special-price",
        ".sale-price",
        ".woocommerce-Price-amount",
    ]

    if price == 0:

        for selector in price_selectors:

            element = soup.select_one(selector)

            if not element:
                continue

            value = (
                element.get("content")
                or element.get_text(" ", strip=True)
            )

            parsed = parse_price(value)

            if parsed > 0:
                price = parsed
                break

    # Currency fallback
    if not currency:

        currency_element = soup.select_one(
            "[itemprop='priceCurrency']"
        )

        if currency_element:

            currency = (
                currency_element.get("content")
                or clean_text(currency_element)
            )

    if not currency:

        page_text = soup.get_text(" ", strip=True)

        if "€" in page_text or "EUR" in page_text:
            currency = "EUR"

        elif "$" in page_text or "USD" in page_text:
            currency = "USD"

        elif "£" in page_text or "GBP" in page_text:
            currency = "GBP"

        elif "zł" in page_text or "PLN" in page_text:
            currency = "PLN"

    # For now regular price defaults to current price.
    regular_price = price

    return price, regular_price, currency


# ---------------------------------------------------------
# AVAILABILITY
# ---------------------------------------------------------

def extract_stock(
    soup: BeautifulSoup,
    product_schema: Optional[Dict[str, Any]],
    default_qty: int
):

    availability = ""

    if product_schema:

        offers = product_schema.get("offers")

        if isinstance(offers, list):
            offers = offers[0] if offers else None

        if isinstance(offers, dict):
            availability = clean_text(
                offers.get("availability")
            )

    if not availability:

        element = soup.select_one(
            "[itemprop='availability']"
        )

        if element:
            availability = (
                element.get("content")
                or element.get("href")
                or clean_text(element)
            )

    low = availability.lower()

    if any(
        x in low
        for x in [
            "outofstock",
            "out_of_stock",
            "unavailable",
            "not available",
        ]
    ):
        return 0, availability

    if any(
        x in low
        for x in [
            "instock",
            "in_stock",
            "available",
        ]
    ):
        return default_qty, availability

    # Look for common visible stock wording
    text = soup.get_text(" ", strip=True).lower()

    if any(
        x in text
        for x in [
            "in stock",
            "in magazzino",
            "disponibile",
            "disponibilità",
        ]
    ):
        return default_qty, availability

    return default_qty, availability


# ---------------------------------------------------------
# IMAGES
# ---------------------------------------------------------

def extract_images(
    soup: BeautifulSoup,
    product_schema: Optional[Dict[str, Any]],
    base_url: str
) -> List[str]:

    images = []

    if product_schema:

        schema_images = product_schema.get("image")

        if isinstance(schema_images, str):
            schema_images = [schema_images]

        if isinstance(schema_images, list):

            for image in schema_images:

                if isinstance(image, dict):
                    image = (
                        image.get("url")
                        or image.get("contentUrl")
                    )

                if image:
                    images.append(
                        absolute_url(
                            str(image),
                            base_url
                        )
                    )

    # OpenGraph image
    for tag in soup.find_all(
        "meta",
        attrs={"property": "og:image"}
    ):

        value = tag.get("content")

        if value:
            images.append(
                absolute_url(value, base_url)
            )

    # Product gallery
    selectors = [
        ".product-images img",
        ".product-gallery img",
        ".product-cover img",
        ".product-image img",
        ".product-thumbnails img",
        ".woocommerce-product-gallery img",
        "[itemprop='image']",
    ]

    for selector in selectors:

        for img in soup.select(selector):

            for attr in [
                "data-src",
                "data-original",
                "data-image",
                "data-large-image",
                "src",
            ]:

                value = img.get(attr)

                if value:
                    images.append(
                        absolute_url(
                            value,
                            base_url
                        )
                    )
                    break

    # General fallback: images containing product-like URLs
    if not images:

        for img in soup.find_all("img"):

            value = (
                img.get("data-src")
                or img.get("data-original")
                or img.get("src")
            )

            if value:
                images.append(
                    absolute_url(
                        value,
                        base_url
                    )
                )

    # Remove tiny/icon images where possible
    filtered = []

    for image in images:

        lower = image.lower()

        if any(
            x in lower
            for x in [
                "logo",
                "icon",
                "favicon",
                "payment",
                "facebook",
                "instagram",
                "google",
                "whatsapp",
            ]
        ):
            continue

        filtered.append(image)

    return unique_list(filtered)


# ---------------------------------------------------------
# CATEGORY
# ---------------------------------------------------------

def extract_category(
    soup: BeautifulSoup,
    product_schema: Optional[Dict[str, Any]]
) -> str:

    if product_schema:

        category = clean_text(
            product_schema.get("category")
        )

        if category:
            return category

    # Breadcrumbs
    breadcrumb_selectors = [
        ".breadcrumb",
        ".breadcrumbs",
        "[aria-label='breadcrumb']",
        "[itemtype*='BreadcrumbList']",
    ]

    for selector in breadcrumb_selectors:

        element = soup.select_one(selector)

        if element:

            links = [
                clean_text(x)
                for x in element.find_all("a")
            ]

            links = [
                x for x in links
                if x and x.lower() not in [
                    "home",
                    "homepage",
                ]
            ]

            if links:
                return " > ".join(links[-2:])

    return "Imported"


# ---------------------------------------------------------
# ATTRIBUTES
# ---------------------------------------------------------

def extract_attributes(
    soup: BeautifulSoup
) -> Dict[str, str]:

    attributes = {}

    # HTML tables
    for row in soup.select(
        "table tr"
    ):

        cells = row.find_all(
            ["th", "td"]
        )

        if len(cells) >= 2:

            key = clean_text(cells[0])
            value = clean_text(cells[1])

            if key and value:
                attributes[key] = value

    # Definition lists
    for dt in soup.find_all("dt"):

        dd = dt.find_next_sibling("dd")

        if dd:

            key = clean_text(dt)
            value = clean_text(dd)

            if key and value:
                attributes[key] = value

    return attributes


# ---------------------------------------------------------
# SHORT DESCRIPTION
# ---------------------------------------------------------

def make_short_description(
    description: str,
    title: str
) -> str:

    if not description:
        return title

    # Keep it reasonably short for WooCommerce-style imports
    if len(description) <= 500:
        return description

    return description[:497].rstrip() + "..."


# ---------------------------------------------------------
# PRODUCT SCRAPER
# ---------------------------------------------------------

async def scrape_product(
    client: httpx.AsyncClient,
    url: str,
    supplied_title: str,
    supplied_id: str,
    quantity: int,
    source_site: str
) -> Dict[str, Any]:

    try:

        response = await client.get(
            url,
            headers=HEADERS
        )

        status_code = response.status_code

        if status_code < 200 or status_code >= 400:

            return {
                "status": "not_found",
                "sku": supplied_id or "—",
                "title": supplied_title or url,
                "brand": "",
                "price": 0.0,
                "regular_price": 0.0,
                "currency": "",
                "stock": 0,
                "availability": "",
                "short_description": "",
                "description": "",
                "category": "Imported",
                "images": [],
                "attributes": {},
                "gtin": "",
                "match": 0.0,
                "source_url": url,
                "source_site": source_site,
                "error": f"HTTP {status_code}",
            }

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        # JSON-LD
        json_ld = extract_json_ld(soup)

        product_schema = find_product_schema(
            json_ld
        )

        # Extract fields
        title = extract_title(
            soup,
            product_schema,
            supplied_title
        )

        brand = extract_brand(
            soup,
            product_schema
        )

        sku = extract_sku(
            soup,
            product_schema
        )

        gtin = extract_gtin(
            soup,
            product_schema
        )

        price, regular_price, currency = extract_price(
            soup,
            product_schema
        )

        stock, availability = extract_stock(
            soup,
            product_schema,
            quantity
        )

        description = extract_description(
            soup,
            product_schema
        )

        short_description = make_short_description(
            description,
            title
        )

        images = extract_images(
            soup,
            product_schema,
            url
        )

        category = extract_category(
            soup,
            product_schema
        )

        attributes = extract_attributes(
            soup
        )

        # Generate a stable fallback SKU
        if not sku:

            sku = (
                supplied_id
                or "SKU-"
                + hashlib.md5(
                    url.encode("utf-8")
                ).hexdigest()[:10].upper()
            )

        # If the site didn't provide a brand,
        # use the domain name as a conservative fallback.
        if not brand:

            hostname = urlparse(url).hostname or ""

            hostname = hostname.lower()

            hostname = re.sub(
                r"^www\.",
                "",
                hostname
            )

            brand = hostname.split(".")[0].capitalize()

        return {
            "status": "ok",

            "sku": sku,

            "gtin": gtin,

            "ean": gtin,

            "title": title,

            "brand": brand,

            "price": price,

            "regular_price": regular_price,

            "currency": currency,

            "stock": stock,

            "availability": availability,

            "short_description": short_description,

            "description": description,

            "category": category,

            "images": images,

            "attributes": attributes,

            "match": 1.0,

            "source_url": url,

            "source_site": source_site,

            "error": "",

        }

    except httpx.TimeoutException:

        return {
            "status": "not_found",
            "sku": supplied_id or "—",
            "title": supplied_title or url,
            "brand": "",
            "price": 0.0,
            "regular_price": 0.0,
            "currency": "",
            "stock": 0,
            "availability": "",
            "short_description": "",
            "description": "",
            "category": "Imported",
            "images": [],
            "attributes": {},
            "gtin": "",
            "match": 0.0,
            "source_url": url,
            "source_site": source_site,
            "error": "Request timed out",
        }

    except Exception as exc:

        return {
            "status": "not_found",
            "sku": supplied_id or "—",
            "title": supplied_title or url,
            "brand": "",
            "price": 0.0,
            "regular_price": 0.0,
            "currency": "",
            "stock": 0,
            "availability": "",
            "short_description": "",
            "description": "",
            "category": "Imported",
            "images": [],
            "attributes": {},
            "gtin": "",
            "match": 0.0,
            "source_url": url,
            "source_site": source_site,
            "error": str(exc),
        }


# ---------------------------------------------------------
# SEARCH PAGE
# ---------------------------------------------------------

def extract_product_links(
    soup: BeautifulSoup,
    base_url: str
) -> List[str]:

    links = []

    domain = urlparse(base_url).netloc

    for link in soup.find_all("a", href=True):

        href = link.get("href")

        if not href:
            continue

        absolute = absolute_url(
            href,
            base_url
        )

        parsed = urlparse(absolute)

        if parsed.netloc and parsed.netloc != domain:
            continue

        # Ignore obvious navigation links
        lower = absolute.lower()

        if any(
            x in lower
            for x in [
                "/cart",
                "/login",
                "/account",
                "/wishlist",
                "/contact",
                "/privacy",
                "/terms",
            ]
        ):
            continue

        links.append(absolute)

    return unique_list(links)


async def find_search_product_url(
    client: httpx.AsyncClient,
    site: str,
    search_term: str
) -> str:

    if not site or not search_term:
        return ""

    site = site.rstrip("/")

    # Existing behavior first
    search_urls = [
        f"{site}/s?q={quote_plus(search_term)}",
        f"{site}/search?q={quote_plus(search_term)}",
        f"{site}/?s={quote_plus(search_term)}",
    ]

    for search_url in search_urls:

        try:

            response = await client.get(
                search_url,
                headers=HEADERS
            )

            if response.status_code >= 400:
                continue

            soup = BeautifulSoup(
                response.text,
                "html.parser"
            )

            links = extract_product_links(
                soup,
                search_url
            )

            if not links:
                continue

            # Try to find a link whose visible text
            # resembles the requested product.
            search_words = [
                x.lower()
                for x in re.findall(
                    r"\w+",
                    search_term
                )
                if len(x) > 2
            ]

            best_url = ""
            best_score = 0

            for link in links:

                element = soup.find(
                    "a",
                    href=re.compile(
                        re.escape(
                            urlparse(link).path
                        ),
                        re.I
                    )
                )

                visible_text = (
                    clean_text(element)
                    if element
                    else ""
                )

                haystack = (
                    visible_text
                    + " "
                    + link
                ).lower()

                score = sum(
                    1
                    for word in search_words
                    if word in haystack
                )

                if score > best_score:

                    best_score = score
                    best_url = link

            if best_url:
                return best_url

            # If no text match, use first likely product link.
            for link in links:

                path = urlparse(link).path.lower()

                if any(
                    x in path
                    for x in [
                        "/product/",
                        "/products/",
                        "/prodotto/",
                        ".html",
                    ]
                ):
                    return link

        except Exception:
            continue

    return ""


# ---------------------------------------------------------
# SCRAPE ENDPOINT
# ---------------------------------------------------------

@app.post("/scrape")
async def scrape_products(
    data: ScrapeRequest
):

    results = []

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(
            30.0,
            connect=10.0
        )
    ) as client:

        for item in data.products:

            target_url = (
                item.url.strip()
                if item.url
                else ""
            )

            source_site = (
                data.site.rstrip("/")
                if data.site
                else ""
            )

            # -------------------------------------------------
            # DIRECT PRODUCT URL
            # -------------------------------------------------

            if target_url:

                result = await scrape_product(
                    client=client,
                    url=target_url,
                    supplied_title=item.title or "",
                    supplied_id=item.id or "",
                    quantity=(
                        item.qty
                        if item.qty is not None
                        else data.default_qty
                    ),
                    source_site=source_site
                )

                results.append(result)

                continue

            # -------------------------------------------------
            # SEARCH MODE
            # -------------------------------------------------

            query = (
                item.title
                or item.id
                or ""
            ).strip()

            if source_site and query:

                found_url = await find_search_product_url(
                    client,
                    source_site,
                    query
                )

                if found_url:

                    result = await scrape_product(
                        client=client,
                        url=found_url,
                        supplied_title=query,
                        supplied_id=item.id or "",
                        quantity=(
                            item.qty
                            if item.qty is not None
                            else data.default_qty
                        ),
                        source_site=source_site
                    )

                    # Slightly lower match score because
                    # the product was discovered through search.
                    if result.get("status") == "ok":
                        result["match"] = 0.95

                    results.append(result)

                    continue

            # -------------------------------------------------
            # NOTHING FOUND
            # -------------------------------------------------

            results.append({
                "status": "not_found",
                "sku": item.id or "—",
                "gtin": "",
                "ean": "",
                "title": item.title or "Unknown",
                "brand": "",
                "price": 0.0,
                "regular_price": 0.0,
                "currency": "",
                "stock": (
                    item.qty
                    if item.qty is not None
                    else data.default_qty
                ),
                "availability": "",
                "short_description": "",
                "description": "",
                "category": "Imported",
                "images": [],
                "attributes": {},
                "match": 0.0,
                "source_url": "",
                "source_site": source_site,
                "error": "No product URL or matching product found",
            })

    return {
        "results": results
    }


# ---------------------------------------------------------
# ROOT
# ---------------------------------------------------------

@app.get("/")
def root():
    return {
        "name": "Product Importer API",
        "status": "online",
        "version": "2.0.0",
        "endpoints": [
            "/health",
            "/scrape"
        ]
    }
