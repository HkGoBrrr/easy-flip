# EasyFlipEstimator

A fix-and-flip underwriter that fits on one screen. Type a ZIP code or an
address and it tells you the most you can pay for the house.

Live at **[easyflipestimator.com](https://easyflipestimator.com)**.

Built for a build-week competition themed "One Screen" — everything the app
does, it does without navigating anywhere.

---

## What it actually does

Most flip calculators make you supply the three hardest numbers yourself: what
it'll sell for, what the rehab costs, and how long you'll hold it. This one
derives all three from data, then lets you override anything you disagree with.

- **Resale** comes from real local asking prices, discounted by how often
  listings in that ZIP cut price before selling.
- **Rehab** comes from a contractor's actual invoices, scaled by job size,
  material grade and region.
- **Hold time** starts from how fast houses actually sell in that ZIP, and is
  draggable out to two years so you can see what a stalled sale costs.
- **Property tax** is computed per ZIP — or per census tract when you give it
  an address.

Where it's guessing, it says so: every price carries a source and a confidence
grade.

---

## Layout

```
web/       the app — static files, no build step
worker/    Cloudflare Worker proxying the property lookup API
engine/    reference implementation of the calculation chain (Python)
```

### web/

Self-contained. `index.html` holds the UI and the full calculation engine;
`data.json` holds 40,846 ZIP codes with prices, market velocity, tax rates and
a regional cost index; `tracts.txt` holds 81,580 census tracts for
neighborhood-level adjustment and is only fetched when you use the address
field. A service worker caches everything, so after the first load it runs with
no signal.

Run it locally:

```bash
cd web
cp config.example.js config.js     # then add your keys
python3 -m http.server 8000
```

Service workers only run on `localhost` or HTTPS, so install-to-home-screen and
offline mode won't work from `file://`.

Deploy: drag the **contents** of `web/` (not the folder) onto any static host.
Cloudflare Pages, Netlify, GitHub Pages all work. No build step.

### worker/

Holds the RentCast API key server-side and caches each address for 30 days.
Optional — leave `PROPERTY_API` empty in `config.js` and the app skips property
autofill. Everything else still works.

```bash
cd worker
npx wrangler login
npx wrangler secret put RENTCAST_KEY
npx wrangler deploy
```

### engine/

`underwrite.py` is the same calculation chain as the JavaScript in
`index.html`, against the same data in `market.db`. It exists so the math can
be tested and back-tested outside a browser.

```bash
cd engine
python3 underwrite.py
```

---

## Configuration

`web/config.js` is gitignored and holds two optional keys:

| Key | What it does | Without it |
|---|---|---|
| `MAPBOX_TOKEN` | Address autocomplete | Falls back to Photon (free, no key, patchier house numbers) |
| `PROPERTY_API` | Autofills sqft/beds/baths from assessor records | You type them in |

A Mapbox public token is safe in client code, but restrict it to your domain in
the Mapbox dashboard. The RentCast key is **not** safe in client code — that's
what the Worker is for.

---

## Where the data comes from

| What | Source |
|---|---|
| Price per sqft, days on market, price-reduced share | Realtor.com inventory data, ZIP level |
| Home values, market temperature, forecast | Zillow research data, metro level |
| Property tax rates | US Census ACS tables B25103 and B25077 |
| Census tract geography | US Census geocoder |
| Rehab unit costs | Real contractor invoices |
| Property records | RentCast (optional) |

All market data ships with the app. Nothing is fetched at runtime except the
optional address lookups.

---

## How the cost model works

Every trade prices as **`max(minimum, mobilization + rate × quantity)`**.
Mobilization is what it costs to show up and stage regardless of size; the rate
is marginal cost per unit. That's why a 200 sqft roof repair runs $13/sqft
where a full replacement runs $6 — which is what actually happens when a crew
mobilizes for half a day.

Drywall repair is the exception, fitted to three real jobs as
**`max($300, 19.8 × affected_sqft^0.614)`**. Patching costs more per square
foot than hanging new board, because the crew has a minimum and small sections
are slow. Below ~50 sqft the minimum governs entirely.

Material quality tiers scale only the **material** share of a line — a $300
faucet and an $80 faucet take the same labor to install.

The model was back-tested against a completed flip in Texas: it
predicted $41,906 in hard costs against $41,218 of actual invoices.

---

## Known limits

- Cost model is calibrated to one Texas market. The regional index scales it
  elsewhere, but that scaling is unproven outside that market.
- Electrical and plumbing rates are estimates rather than quotes.
- Neighborhood adjustment is tract-level, so two houses on the same block read
  the same.
- Texas is a non-disclosure state, so resale values are built from asking
  prices rather than recorded sales.

Nothing here is a substitute for walking the property.
