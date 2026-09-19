# Product Importer → CSV / XML

A lightweight tool to scrape supplier websites and export WooCommerce-ready CSV/XML files.

```
product-importer/
├── backend/          ← Python FastAPI (real scraper)
│   ├── main.py
│   └── requirements.txt
├── frontend/         ← Static HTML (GitHub Pages)
│   └── index.html
├── render.yaml       ← Render.com free backend deploy
└── .github/
    └── workflows/
        └── deploy.yml  ← Auto-deploys frontend on push
```

---

## 🚀 Deploy in 3 steps (all free)

### Step 1 – Create the GitHub repository

1. Go to **https://github.com/new**
2. Repository name: `product-importer`
3. Set to **Public** (required for free GitHub Pages)
4. Click **Create repository**
5. Upload these files (drag & drop all folders) or use Git:

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/product-importer.git
git push -u origin main
```

---

### Step 2 – Deploy backend on Render.com (free)

1. Go to **https://render.com** → Sign up / Log in with GitHub
2. Click **New → Web Service**
3. Connect your `product-importer` GitHub repository
4. Settings:
   - **Root Directory**: `backend`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free
5. Click **Create Web Service**
6. Wait ~2 minutes. Copy your URL, e.g. `https://product-importer-api.onrender.com`

> ⚠️ Free Render services sleep after 15 min of inactivity. First request after sleep takes ~30 seconds to wake up.

---

### Step 3 – Enable GitHub Pages (frontend)

1. In your repo → **Settings → Pages**
2. Source: **GitHub Actions**
3. The workflow runs automatically on every push to `main`
4. Your frontend URL will be: `https://YOUR_USERNAME.github.io/product-importer/`

---

### Step 4 – Connect frontend to backend

1. Open your GitHub Pages URL
2. Paste your Render URL into the **Backend API URL** field
3. The URL is saved in your browser automatically

---

## How it works

1. You enter product names/IDs and the supplier site URL
2. The frontend sends a POST request to `/scrape` on your backend
3. The backend searches the supplier site for each product, scrapes:
   - Title, price, stock status
   - Description, images
   - Brand, SKU, category, attributes
4. Results appear in the table
5. Export to **WooCommerce CSV** or **XML**

---

## Local development

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
# API runs on http://localhost:8000
```

Open `frontend/index.html` in browser, set API URL to `http://localhost:8000`.

---

## Notes

- Max 50 products per request
- Works with any supplier site that has a search function
- CORS is open so any frontend can call the backend
- For heavy use, upgrade Render to a paid plan
