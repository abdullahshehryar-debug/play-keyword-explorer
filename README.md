# Play Keyword Explorer

Keyword in → top 200 Google Play search results out (live, in Google's rank order).

## Deploy (free, ~2 minutes)

**Option A — Vercel CLI**
```
npm i -g vercel
vercel          # answer the prompts, accept defaults
vercel --prod   # gives you the final https://...vercel.app link
```

**Option B — GitHub + Vercel website**
1. Push this folder to a GitHub repo.
2. Go to vercel.com → Add New Project → import the repo → Deploy. No settings needed.

## Run locally
```
npm install
npx vercel dev     # opens http://localhost:3000
```

## Endpoints
- `GET /api/search?term=screen+mirroring&country=us&lang=en&num=200` → ranked results
- `GET /api/details?ids=com.a,com.b&country=us` → installs, ratings, updated date (max 10 ids)

Data comes from play.google.com via `google-play-scraper`. Google has no official search API; this reads the public store pages, so if Google changes its page markup, bump the package version.
