/**
 * EasyFlipEstimator — property lookup proxy (v2).
 *
 * Why two strategies: RentCast's address search is an exact-ish string match and
 * fails on small formatting differences. Coordinates can't be misformatted, so
 * when the address misses we search a tiny radius and take the nearest record.
 *
 * Query params:
 *   address  required  full address from the geocoder
 *   lat,lon  optional  coordinates for the fallback search
 *
 * Setup:
 *   npx wrangler login
 *   npx wrangler secret put RENTCAST_KEY
 *   npx wrangler deploy
 */

const CACHE_SECONDS = 60 * 60 * 24 * 30;
const FALLBACK_RADIUS_MI = 0.06;   // ~100 yards; tight enough to hit one parcel

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin") || "";
    const allowList = (env.ALLOWED_ORIGIN || "*").split(",").map((s) => s.trim()).filter(Boolean);
    const open = allowList.includes("*");
    const allowed = open ? "*" : (allowList.includes(origin) ? origin : allowList[0]);
    const cors = {
      "Access-Control-Allow-Origin": allowed,
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      Vary: "Origin",
    };

    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    if (request.method !== "GET") return json({ error: "GET only" }, 405, cors);
    // Browsers always send Origin on this cross-site call; scripts and curl usually don't.
    // Spoofable by a determined caller, which is why the rate limits below also exist.
    if (!open && !allowList.includes(origin))
      return json({ error: "origin not allowed" }, 403, cors);

    const url = new URL(request.url);
    const raw = (url.searchParams.get("address") || "").trim();
    const lat = parseFloat(url.searchParams.get("lat"));
    const lon = parseFloat(url.searchParams.get("lon"));
    const hasCoords = Number.isFinite(lat) && Number.isFinite(lon);

    if (raw.length < 6 && !hasCoords)
      return json({ error: "address or coordinates required" }, 400, cors);
    if (!env.RENTCAST_KEY) return json({ error: "not configured" }, 501, cors);

    const address = normalize(raw);

    const cacheKey = new Request(
      "https://cache.local/p2?a=" + encodeURIComponent(address.toLowerCase()) +
        (hasCoords ? "&c=" + lat.toFixed(5) + "," + lon.toFixed(5) : ""),
      { method: "GET" }
    );
    const cache = caches.default;
    const hit = await cache.match(cacheKey);
    if (hit) return json({ ...(await hit.json()), cached: true }, 200, cors);

    const ip = request.headers.get("cf-connecting-ip") || "unknown";
    if (env.PER_IP && !(await env.PER_IP.limit({ key: ip })).success)
      return json({ error: "slow down — too many lookups" }, 429, cors);
    if (env.ALL_USERS && !(await env.ALL_USERS.limit({ key: "all" })).success)
      return json({ error: "lookups are busy — try again in a minute" }, 429, cors);

    const headers = { "X-Api-Key": env.RENTCAST_KEY, Accept: "application/json" };
    let rec = null;
    let how = null;

    // 1. exact address, normalized
    if (address.length >= 6) {
      const r = await tryFetch(
        "https://api.rentcast.io/v1/properties?address=" + encodeURIComponent(address),
        headers
      );
      if (r.error) return json({ error: r.error }, 502, cors);
      rec = firstRecord(r.data);
      if (rec) how = "address";
    }

    // 2. nearest parcel to the geocoded point
    if (!rec && hasCoords) {
      const r = await tryFetch(
        "https://api.rentcast.io/v1/properties?latitude=" + lat +
          "&longitude=" + lon + "&radius=" + FALLBACK_RADIUS_MI + "&limit=5",
        headers
      );
      if (!r.error) {
        const list = Array.isArray(r.data) ? r.data : r.data ? [r.data] : [];
        rec = nearest(list, lat, lon, houseNumber(address));
        if (rec) how = "nearby";
      }
    }

    if (!rec) return json({ found: false, tried: address }, 200, cors);

    const assessments = rec.taxAssessments || {};
    const years = Object.keys(assessments).sort();
    const latest = years.length ? assessments[years[years.length - 1]] : null;

    const out = {
      found: true,
      matchedBy: how,
      address: rec.formattedAddress || address,
      zip: rec.zipCode || null,
      sqft: num(rec.squareFootage),
      beds: num(rec.bedrooms),
      baths: num(rec.bathrooms),
      yearBuilt: num(rec.yearBuilt),
      lotSize: num(rec.lotSize),
      propertyType: rec.propertyType || null,
      assessedValue: latest ? num(latest.value) : null,
      assessedYear: latest ? years[years.length - 1] : null,
    };

    const res = json(out, 200, {
      ...cors,
      "Cache-Control": "public, max-age=" + CACHE_SECONDS,
    });
    ctx.waitUntil(cache.put(cacheKey, res.clone()));
    return res;
  },
};

/* "123 Main St, Springfield, IL, 62701" -> "123 Main St, Springfield, IL 62701"
   Geocoders comma-separate every component; RentCast wants state and ZIP joined
   by a space, and chokes on trailing country names or doubled commas. */
function normalize(s) {
  let t = s
    .replace(/\s+/g, " ")
    .replace(/,\s*(United States|USA|US)\s*$/i, "")
    .trim()
    .replace(/,\s*,/g, ",")
    .replace(/,\s*$/, "");
  t = t.replace(/,\s*([A-Za-z]{2}),\s*(\d{5})(-\d{4})?$/, ", $1 $2");
  t = t.replace(/(\d{5})-\d{4}$/, "$1");
  return t;
}

function houseNumber(addr) {
  const m = addr.match(/^\s*(\d+)/);
  return m ? m[1] : null;
}

function firstRecord(data) {
  if (!data) return null;
  if (Array.isArray(data)) return data.length ? data[0] : null;
  return data.id || data.formattedAddress ? data : null;
}

/* Prefer a record whose house number matches; otherwise take the closest point. */
function nearest(list, lat, lon, hnum) {
  if (!list.length) return null;
  if (hnum) {
    const exact = list.filter(
      (p) => (p.addressLine1 || "").trim().startsWith(hnum + " ")
    );
    if (exact.length === 1) return exact[0];
    if (exact.length > 1) list = exact;
  }
  let best = null;
  let bd = Infinity;
  for (const p of list) {
    if (!Number.isFinite(p.latitude) || !Number.isFinite(p.longitude)) continue;
    const dy = p.latitude - lat;
    const dx = (p.longitude - lon) * Math.cos((lat * Math.PI) / 180);
    const d = dy * dy + dx * dx;
    if (d < bd) {
      bd = d;
      best = p;
    }
  }
  return best || list[0];
}

async function tryFetch(url, headers) {
  let r;
  try {
    r = await fetch(url, { headers });
  } catch (e) {
    return { error: "upstream unreachable" };
  }
  if (r.status === 404) return { data: null };
  if (!r.ok) return { error: "upstream " + r.status };
  try {
    return { data: await r.json() };
  } catch (e) {
    return { data: null };
  }
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : null;
}

function json(obj, status, headers) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}
