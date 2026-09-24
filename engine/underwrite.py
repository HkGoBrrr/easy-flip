"""
Flip underwriting engine, v1.

The caller supplies house facts a user actually knows — ZIP, square footage,
beds, baths, and which trades need work — and the engine derives every
quantity, cost, and the offer. No line-item takeoff required.

    from underwrite import Deal, underwrite
    r = underwrite(Deal(zip_code="77002", sqft=1500, beds=3, baths=2))
"""
import sqlite3
from dataclasses import dataclass

DB = "market.db"

GAUGE_CEILING   = 0.40    # hard + GC + holding, as share of ARV
MARGIN_BASE     = 0.20
SELL_COMMISSION = 0.06
SELL_CLOSING    = 0.01
BUY_CLOSING_PCT = 0.02
ACQUISITION_DAYS  = 7
CONTRACT_TO_CLOSE = 35
REHAB_FIXED_DAYS  = 21
REHAB_DAYS_PER_DOLLAR = 1 / 1500
ROOF_PITCH_FACTOR = 1.20

SCOPE_LEVELS = {
    "cosmetic": ["Interior paint", "Flooring LVP", "Fixtures & devices",
                 "Dumpster & debris haul"],
    "standard": ["Interior paint", "Exterior paint", "Flooring LVP",
                 "Tear-off & replace 3-tab/architectural", "Interior doors & trim",
                 "Cabinets (stock)", "Countertops", "Appliance package",
                 "Refresh (vanity/toilet/tile/fixtures)", "Fixtures & devices",
                 "Full system replace (condenser + air handler)",
                 "Dumpster & debris haul", "Permits"],
    "gut":      ["Full drywall replace", "Interior paint", "Exterior paint",
                 "Flooring LVP", "Tear-off & replace 3-tab/architectural",
                 "Interior doors & trim", "Cabinets (stock)", "Countertops",
                 "Appliance package", "Full bath gut & rebuild", "Whole-house rewire",
                 "Repipe supply lines", "Full system replace (condenser + air handler)",
                 "Ductwork replacement", "Insulation (attic blown)",
                 "Panel upgrade 200A", "Water heater", "Windows",
                 "Dumpster & debris haul", "Permits"],
}

PER_SQFT = {"Interior paint", "Exterior paint", "Flooring LVP", "Carpet",
            "Full drywall replace", "Drywall demo / tear-out",
            "Insulation (attic blown)", "Siding replacement"}


def derive_quantities(sqft, beds, baths, trades):
    """House facts -> per-line quantities, so the user never does a takeoff."""
    q = {}
    for t in trades:
        if t in PER_SQFT:
            q[t] = sqft
        elif t == "Tear-off & replace 3-tab/architectural":
            q[t] = sqft * ROOF_PITCH_FACTOR
        elif t == "Interior doors & trim":
            q[t] = beds + baths + 3
        elif t == "Windows":
            q[t] = max(6, round(sqft / 130))
        elif t in ("Full bath gut & rebuild", "Refresh (vanity/toilet/tile/fixtures)"):
            q[t] = baths
        elif t == "Cabinets (stock)":
            q[t] = 14 if sqft < 1500 else 20
        elif t == "Countertops":
            q[t] = 35 if sqft < 1500 else 50
        elif t == "Dumpster & debris haul":
            q[t] = 1 if sqft < 1200 else 2
        else:
            q[t] = 1
    return q


@dataclass
class Deal:
    zip_code: str
    sqft: int
    beds: int = 3
    baths: int = 2
    scope_level: str = "standard"
    trades: list | None = None
    drywall_repair_sqft: float = 0.0
    finish: str = "standard"          # global default tier
    finish_by_line: dict | None = None  # per-line override: {line_item: tier}

    hold_days: int | None = None
    insurance_per_month: float = 42.0
    utilities_per_month: float = 13.0
    yard_per_month: float = 60.0
    tax_basis: str = "arv"
    assessed_value: float | None = None
    financed: bool = True
    loan_ltc: float = 0.85
    rate: float = 0.12
    points: float = 0.02
    gc_pct: float = 0.10
    contingency_pct: float = 0.20
    margin_pct: float | None = None


def _finish_mult(line_item, deal, meta, tiers):
    """Finish tier scales the MATERIAL portion only — a $300 faucet and an $80
    faucet take the same labor to install."""
    info = meta.get(line_item)
    if not info or not info["finish_sensitive"] or info["material_share"] is None:
        return 1.0
    tier = (deal.finish_by_line or {}).get(line_item, deal.finish)
    mult = tiers.get(tier, 1.0)
    ms = info["material_share"]
    return (1 - ms) + ms * mult


def default_finish_for_zip(psf):
    """Nobody puts builder-grade cabinets in a $2,400/sqft house."""
    if psf >= 600: return "luxury"
    if psf >= 400: return "high"
    if psf >= 250: return "mid"
    if psf >= 120: return "standard"
    return "builder"


def _line(rule, qty, idx):
    return max(rule["min_charge"], rule["mobilization"] + rule["variable_rate"] * qty) * idx


def underwrite(deal: Deal, db=DB):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM zip_market WHERE zip=?", (deal.zip_code,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"ZIP {deal.zip_code} not found")
    m = dict(row)
    rules = {r["line_item"]: r for r in conn.execute("SELECT * FROM cost_scaling")}
    flat = {r["line_item"]: r["unit_cost"] for r in conn.execute(
        "SELECT line_item, unit_cost FROM rehab_costs WHERE unit_cost IS NOT NULL")}
    meta = {r["line_item"]: r for r in conn.execute(
        "SELECT line_item, material_share, finish_sensitive FROM rehab_costs")}
    tiers = {r["tier"]: r["material_multiplier"] for r in conn.execute(
        "SELECT tier, material_multiplier FROM finish_tiers")}
    conn.close()
    idx = m["cost_index"]

    # ARV as a range. No blending — the spread is the confidence signal.
    list_basis = m["psf"] * deal.sqft
    s2l = 1.00 - 0.22 * (m["price_reduced_share"] or 0.0)
    band = {"high": 0.06, "medium": 0.10, "low": 0.16}[m["psf_confidence"]]
    arv_lo, arv_hi = list_basis * s2l * (1 - band), list_basis * s2l * (1 + band)
    arv = (arv_lo + arv_hi) / 2

    trades = deal.trades or SCOPE_LEVELS[deal.scope_level]
    qty = derive_quantities(deal.sqft, deal.beds, deal.baths, trades)
    hard, breakdown = 0.0, {}
    for t, q in qty.items():
        if t in rules:
            c = _line(rules[t], q, idx)
        elif t in flat:
            c = flat[t] * q * idx
        else:
            continue
        c *= _finish_mult(t, deal, meta, tiers)
        breakdown[t] = c
        hard += c
    if deal.drywall_repair_sqft > 0:
        c = max(300.0, 19.8 * deal.drywall_repair_sqft ** 0.614) * idx
        breakdown["Drywall repair & texture"] = c
        hard += c

    gc = hard * deal.gc_pct
    contingency = hard * deal.contingency_pct
    rehab_total = hard + gc + contingency

    if deal.hold_days is not None:
        hold_days = float(deal.hold_days)
    else:
        hold_days = (ACQUISITION_DAYS + REHAB_FIXED_DAYS
                     + rehab_total * REHAB_DAYS_PER_DOLLAR
                     + (m["dom"] or 60) + CONTRACT_TO_CLOSE)
    months = hold_days / 30.4

    # ACS effective rates are taxes over MARKET value, so ARV is the right basis.
    tax_base = deal.assessed_value if (deal.tax_basis == "assessed"
                                       and deal.assessed_value) else arv
    taxes = tax_base * m["eff_tax_rate"] / 12 * months
    holding = (deal.insurance_per_month + deal.utilities_per_month
               + deal.yard_per_month) * months + taxes

    gauge_cost = hard + gc + holding
    gauge_pct = gauge_cost / arv

    if deal.margin_pct is not None:
        margin_pct = deal.margin_pct
    else:
        margin_pct = MARGIN_BASE
        t = m["market_temp"]
        if t is not None:
            margin_pct += -0.02 if t >= 61 else (0.02 if t <= 25 else 0.0)
        if (m["forecast_12mo_pct"] or 0) < 0:
            margin_pct += 0.03
    margin = arv * margin_pct
    selling = arv * (SELL_COMMISSION + SELL_CLOSING)

    offer, financing = arv * 0.5, 0.0
    for _ in range(50):
        if deal.financed:
            loan = (offer + rehab_total) * deal.loan_ltc
            financing = loan * deal.points + loan * deal.rate / 12 * months
        new = (arv - rehab_total - holding - financing - selling
               - offer * BUY_CLOSING_PCT - margin)
        if abs(new - offer) < 1:
            offer = new
            break
        offer = new

    return {
        "market": m, "quantities": qty, "breakdown": breakdown,
        "arv_low": arv_lo, "arv_high": arv_hi, "sale_to_list": s2l,
        "hard": hard, "gc": gc, "contingency": contingency, "rehab_total": rehab_total,
        "hold_days": hold_days, "taxes": taxes, "holding": holding,
        "gauge_pct": gauge_pct, "gauge_ceiling": GAUGE_CEILING,
        "gauge_status": "over" if gauge_pct > GAUGE_CEILING else "ok",
        "financing": financing, "selling": selling,
        "margin_pct": margin_pct, "margin": margin,
        "finish": deal.finish, "suggested_finish": default_finish_for_zip(m["psf"]),
        "mao": offer, "mao_pct_of_arv": offer / arv,
        "profit_at_mao": arv - offer - rehab_total - holding - financing - selling,
    }


def profit_at_price(deal: Deal, price, db=DB):
    """What you'd actually net at a given purchase price."""
    r = underwrite(deal, db)
    arv = (r["arv_low"] + r["arv_high"]) / 2
    months = r["hold_days"] / 30.4
    fin = 0.0
    if deal.financed:
        loan = (price + r["rehab_total"]) * deal.loan_ltc
        fin = loan * deal.points + loan * deal.rate / 12 * months
    return arv - price - r["rehab_total"] - r["holding"] - fin - r["selling"] \
        - price * BUY_CLOSING_PCT


if __name__ == "__main__":
    d = Deal(zip_code="77002", sqft=1500, beds=3, baths=2, scope_level="standard",
             hold_days=730, insurance_per_month=42, utilities_per_month=13,
             yard_per_month=60, financed=False)
    r = underwrite(d)
    print(f"ARV        ${r['arv_low']:,.0f} - ${r['arv_high']:,.0f}")
    print(f"hard       ${r['hard']:,.0f}  GC ${r['gc']:,.0f}  cont ${r['contingency']:,.0f}")
    print(f"rehab      ${r['rehab_total']:,.0f}")
    print(f"holding    ${r['holding']:,.0f}  ({r['hold_days']:.0f}d, taxes ${r['taxes']:,.0f})")
    print(f"gauge      {r['gauge_pct']:.1%} (ceiling {r['gauge_ceiling']:.0%}) -> {r['gauge_status']}")
    print(f"margin     {r['margin_pct']:.0%}   selling ${r['selling']:,.0f}")
    print(f"MAO        ${r['mao']:,.0f}")
    print(f"\nat your actual $40,000 purchase: profit ${profit_at_price(d, 40000):,.0f}"
          f"   (compare against your own numbers)")
