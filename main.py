from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx
from bs4 import BeautifulSoup

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ProductRequest(BaseModel):
    product_id: str = Field(..., alias="id")
    name: str
    url: str
    quantity: int = 1

    class Config:
        populate_by_name = True

@app.get("/")
def health_check():
    return {"status": "awake", "message": "Backend is running!"}

@app.post("/scrape")
async def scrape_product(data: ProductRequest):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            response = await client.get(data.url, headers=headers)
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Failed to fetch URL, status code: {response.status_code}")
            
            soup = BeautifulSoup(response.text, "html.parser")
            
            # Fallback title extraction if specific selectors fail
            page_title = soup.title.string.strip() if soup.title else data.name
            
            return {
                "success": True,
                "id": data.product_id,
                "input_name": data.name,
                "scraped_title": page_title,
                "url": data.url,
                "quantity": data.quantity
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
