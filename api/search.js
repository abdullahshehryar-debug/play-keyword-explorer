import gplay from 'google-play-scraper';

const ALLOWED_COUNTRIES = /^[a-z]{2}$/;

export default async function handler(req, res) {
  const { term = '', country = 'us', lang = 'en', num = '200' } = req.query || {};
  const t = String(term).trim();
  if (!t) return res.status(400).json({ error: 'term is required' });
  const gl = ALLOWED_COUNTRIES.test(country) ? country : 'us';
  const hl = /^[a-z]{2}(-[A-Z]{2})?$/.test(lang) ? lang : 'en';
  const n = Math.min(Math.max(parseInt(num, 10) || 200, 1), 250);
  try {
    const results = await gplay.search({ term: t, num: n, country: gl, lang: hl, throttle: 10 });
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.status(200).json({
      term: t, country: gl, lang: hl, count: results.length, fetchedAt: new Date().toISOString(),
      results: results.map((r, i) => ({
        rank: i + 1, appId: r.appId, title: r.title, developer: r.developer, developerId: r.developerId,
        icon: r.icon, url: r.url, score: r.score ?? null, scoreText: r.scoreText ?? '',
        price: r.price ?? null, currency: r.currency ?? '', free: r.free ?? null, summary: r.summary ?? ''
      }))
    });
  } catch (err) {
    res.status(502).json({ error: 'Google Play did not answer: ' + (err && err.message ? err.message : String(err)) });
  }
}
