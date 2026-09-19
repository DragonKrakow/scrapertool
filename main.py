from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx
from bs4 import BeautifulSoup
import re
from typing import List, Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ProductInput(BaseModel):
    id: Optional[str] = ""
    title: Optional[str] = ""
    url: Optional[str] = ""
    qty: Optional[int] = None

class ScrapeRequest(BaseModel):
    products: List[ProductInput]
    site: Optional[str] = ""
    default_qty: int = 10

@app.get("/health")
def health_check():
    return {"status": "awake", "message": "Backend is running!"}

@app.post("/scrape")
async def scrape_products(data: ScrapeRequest):
    results = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        for item in data.products:
            target_url = item.url
            
            # If no direct URL was given, fallback or construct search URL if site is provided
            if not target_url and data.site:
                query_term = item.title or item.id
                target_url = f"{data.site.rstrip('/')}/s?q={query_term.replace(' ', '+')}"

            if not target_url:
                results.append({
                    "status": "not_found",
                    "sku": item.id or "—",
                    "title": item.title or "Unknown",
                    "brand": "",
                    "price": 0.0,
                    "regular_price": 0.0,
                    "stock": item.qty if item.qty is not None else data.default_qty,
                    "match": 0.0,
                    "source_url": "",
                    "source_site": data.site or ""
                })
                continue

            try:
                response = await client.get(target_url, headers=headers)
                if response.status_code != 200:
                    results.append({
                        "status": "not_found",
                        "sku": item.id or "—",
                        "title": item.title or target_url,
                        "brand": "",
                        "price": 0.0,
                        "regular_price": 0.0,
                        "stock": item.qty if item.qty is not None else data.default_qty,
                        "match": 0.0,
                        "source_url": target_url,
                        "source_site": data.site or ""
                    })
                    continue

                soup = BeautifulSoup(response.text, "html.parser")
                
                # Extract title safely
                page_title = item.title
                if not page_title:
                    if soup.title and soup.title.string:
                        page_title = soup.title.string.strip()
                    else:
                        h1 = soup.find("h1")
                        page_title = h1.get_text(strip=True) if h1 else "Imported Product"

                # Attempt generic price extraction
                price = 0.0
                price_elem = soup.find(class_=re.compile("price|cost|amount", re.I))
                if price_elem:
                    price_text = re.sub(r"[^\d.,]", "", price_elem.get_text())
                    price_text = price_text.replace(",", ".")
                    try:
                        price = float(price_text)
                    except ValueError:
                        pass

                results.append({
                    "status": "ok",
                    "sku": item.id or f"SKU-{abs(hash(target_url)) % 100000}",
                    "title": page_title,
                    "brand": "",
                    "price": price,
                    "regular_price": price,
                    "stock": item.qty if item.qty is not None else data.default_qty,
                    "short_description": page_title,
                    "description": f"Imported from {target_url}",
                    "category": "Imported",
                    "images": [],
                    "attributes": {},
                    "match": 1.0,
                    "source_url": target_url,
                    "source_site": data.site or ""
                })

            except Exception as e:
                results.append({
                    "status": "not_found",
                    "sku": item.id or "—",
                    "title": item.title or target_url,
                    "brand": "",
                    "price": 0.0,
                    "regular_price": 0.0,
                    "stock": item.qty if item.qty is not None else data.default_qty,
                    "match": 0.0,
                    "source_url": target_url,
                    "source_site": data.site or ""
                })

    return {"results": results}
