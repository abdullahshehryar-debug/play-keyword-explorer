import gplay from 'google-play-scraper';

// Enrich up to 10 apps per call with installs, ratings, updated date (one Play request each).
export default async function handler(req, res) {
  const { ids = '', country = 'us', lang = 'en' } = req.query || {};
  const list = String(ids).split(',').map(s => s.trim()).filter(s => /^[\w.]+$/.test(s)).slice(0, 10);
  if (!list.length) return res.status(400).json({ error: 'ids is required (comma separated, max 10)' });
  const gl = /^[a-z]{2}$/.test(country) ? country : 'us';
  const out = {};
  await Promise.all(list.map(async id => {
    try {
      const a = await gplay.app({ appId: id, country: gl, lang, throttle: 10 });
      out[id] = {
        installs: a.installs ?? '', minInstalls: a.minInstalls ?? null, maxInstalls: a.maxInstalls ?? null,
        ratings: a.ratings ?? null, reviews: a.reviews ?? null, score: a.score ?? null,
        updated: a.updated ? new Date(a.updated).toISOString().slice(0, 10) : '',
        released: a.released ?? '', version: a.version ?? '', genre: a.genre ?? '', genreId: a.genreId ?? '',
        contentRating: a.contentRating ?? '', adSupported: a.adSupported ?? null, offersIAP: a.offersIAP ?? null,
        androidVersion: a.androidVersion ?? ''
      };
    } catch (e) { out[id] = { error: e && e.message ? e.message : String(e) }; }
  }));
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.status(200).json(out);
}
