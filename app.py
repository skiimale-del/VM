# Streamlit dashboard: Pinnacle baseline vs svenska bolag via The Odds API
# Marknader: 1X2, Asian Handicap, Over/Under (pre‑match)
from __future__ import annotations
import os
import math
import time
from typing import Dict, List, Optional, Tuple
import requests
import pandas as pd
import streamlit as st

# Hemligheter sätts i Streamlit Cloud: Settings -> Secrets
PINN_USER = st.secrets.get("PINNACLE_USER", "")
PINN_PASS = st.secrets.get("PINNACLE_PASS", "")
ODDS_API_KEY = st.secrets.get("ODDS_API_KEY", "")

# Konfig
PINN_BASE = "api.pinnacle.com"
SPORT_ID_SOCCER = 29  # Pinnacle: Soccer
EDGE_DEFAULT = 0.02   # 2% edge default
SVENSKA_DOMAINS = {"dbet.se", "atg.se", "expekt.se", "luckysports.se"}

# The Odds API
ODDS_API_BASE = "[api.the-odds-api.com](https://api.the-odds-api.com/v4)"
# OBS: sport-ID kan variera per leverantör/år. Byt om din nyckel visar annat namn/id för VM.
ODDS_SPORT = "soccer_fifa_world_cup"
ODDS_REGIONS = "eu"
ODDS_FORMAT = "decimal"

# Sessions
pinn = requests.Session()
if PINN_USER and PINN_PASS:
    pinn.auth = (PINN_USER, PINN_PASS)
pinn.headers.update({"Accept": "application/json"})

agg = requests.Session()

def implied_prob(odds: float) -> float:
    return 1.0 / odds

def normalize_no_vig(probs: List[float]) -> List[float]:
    s = sum(probs)
    return [p / s for p in probs] if s > 0 else probs

def calc_ev_percent(odds: float, true_p: float) -> float:
    return ((odds - 1.0) * true_p - (1.0 - true_p)) * 100.0

def pinn_get(path: str, params: Optional[dict] = None):
    url = f"{PINN_BASE}{path}"
    r = pinn.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def get_world_cup_league_ids() -> List[int]:
    leagues = pinn_get(f"/sports/{SPORT_ID_SOCCER}/leagues")
    ids = []
    for lg in leagues:
        name = (lg.get("name") or "").lower()
        if "world cup" in name or "fifa world cup" in name:
            ids.append(lg["id"])
    return ids

def get_pinn_odds(league_ids: List[int], market: str) -> dict:
    params = {
        "sportId": SPORT_ID_SOCCER,
        "oddsFormat": "DECIMAL",
        "market": market
    }
    if league_ids:
        params["leagueIds"] = ",".join(map(str, league_ids))
    return pinn_get("/odds", params)

def merge_events(ml: dict, tot: dict, sp: dict) -> Dict[int, dict]:
    ev = {}
    for d in (ml, tot, sp):
        for l in d.get("leagues", []):
            for e in l.get("events", []):
                cur = ev.get(e["id"], {"id": e["id"], "home": e.get("home"), "away": e.get("away"), "starts": e.get("starts"), "periods": []})
                cur["home"] = cur.get("home") or e.get("home")
                cur["away"] = cur.get("away") or e.get("away")
                cur["starts"] = cur.get("starts") or e.get("starts")
                cur["periods"] += e.get("periods", [])
                ev[e["id"]] = cur
    return ev

def extract_true_probs(event: dict) -> Dict[str, Dict]:
    true_probs = {"H2H": {}, "Totals": {}, "Handicap": {}}
    periods = event.get("periods", [])
    ft = next((p for p in periods if p.get("number") == 0), None)
    if not ft:
        return true_probs

    ml = ft.get("moneyline") or ft.get("threeWayMoneyline") or ft.get("3way")
    if ml and all(k in ml for k in ("home", "draw", "away")):
        raw = [implied_prob(ml["home"]), implied_prob(ml["draw"]), implied_prob(ml["away"])]
        true_probs["H2H"]["main"] = normalize_no_vig(raw)

    for tot in ft.get("totals", []):
        if tot.get("over") and tot.get("under") and "points" in tot:
            pts = float(tot["points"])
            raw = [implied_prob(tot["over"]), implied_prob(tot["under"])]
            true_probs["Totals"][pts] = normalize_no_vig(raw)

    for sp in ft.get("spreads", []):
        if sp.get("home") and sp.get("away") and "points" in sp:
            pts = float(sp["points"])
            raw = [implied_prob(sp["home"]), implied_prob(sp["away"])]
            true_probs["Handicap"][pts] = normalize_no_vig(raw)

    return true_probs

def fetch_swe_odds() -> dict:
    url = f"{ODDS_API_BASE}/sports/{ODDS_SPORT}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": ODDS_REGIONS,
        "markets": "h2h,totals,spreads",
        "oddsFormat": ODDS_FORMAT
    }
    r = agg.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    out = {}
    for g in data:
        key = (g.get("home_team"), g.get("away_team"), g.get("commence_time"))
        for bm in g.get("bookmakers", []):
            title = (bm.get("title") or "").lower()
            # koppla till våra .se-domäner via enkla heuristiker
            domain = None
            if "expekt" in title: domain = "expekt.se"
            elif "atg" in title: domain = "atg.se"
            elif "dbet" in title: domain = "dbet.se"
            elif "lucky" in title: domain = "luckysports.se"
            if domain not in SVENSKA_DOMAINS:
                continue
            for mk in bm.get("markets", []):
                mkey = mk.get("key")  # "h2h", "spreads", "totals"
                if mkey not in {"h2h", "spreads", "totals"}:
                    continue
                for o in mk.get("outcomes", []):
                    name = (o.get("name") or "").lower()  # "home","draw","away" eller "over","under"
                    price = float(o.get("price"))
                    point = o.get("point")
                    out.setdefault(key, {}).setdefault(mkey, {}).setdefault(point if point is not None else "main", {}).setdefault(domain, {})
                    out[key][mkey][point if point is not None else "main"][domain][name] = price
    return out

def compute_values(pinn_events: Dict[int, dict], swe_odds: dict, edge_threshold: float) -> List[dict]:
    rows = []
    for ev in pinn_events.values():
        home = ev.get("home"); away = ev.get("away"); starts = ev.get("starts")
        tp = extract_true_probs(ev)
        offered = swe_odds.get((home, away, starts), {})

        # 1X2
        if tp["H2H"].get("main"):
            probs = tp["H2H"]["main"]
            fair = [1/x for x in probs]
            offers = offered.get("h2h", {}).get("main", {})
            for domain, price_map in offers.items():
                for sel, idx in (("home",0), ("draw",1), ("away",2)):
                    if sel in price_map:
                        odds = float(price_map[sel])
                        edge = probs[idx] - implied_prob(odds)
                        if edge >= edge_threshold:
                            rows.append({
                                "match": f"{home} - {away}",
                                "kickoff": starts,
                                "market": "1X2",
                                "selection": sel.upper(),
                                "line": "",
                                "bookmaker": domain,
                                "offered_odds": round(odds, 3),
                                "fair_odds": round(fair[idx], 3),
                                "true_p": round(probs[idx], 4),
                                "edge_%": round(edge*100, 2),
                                "ev_%": round(calc_ev_percent(odds, probs[idx]), 2),
                            })

        # Totals
        for pts, probs in tp["Totals"].items():
            if len(probs) != 2: continue
            fair_over, fair_under = 1/probs[0], 1/probs[1]
            offers = offered.get("totals", {}).get(pts, {})
            for domain, price_map in offers.items():
                for sel, idx in (("over",0), ("under",1)):
                    if sel in price_map:
                        odds = float(price_map[sel])
                        edge = probs[idx] - implied_prob(odds)
                        if edge >= edge_threshold:
                            rows.append({
                                "match": f"{home} - {away}",
                                "kickoff": starts,
                                "market": "Over/Under",
                                "selection": f"{sel.capitalize()} {pts}",
                                "line": pts,
                                "bookmaker": domain,
                                "offered_odds": round(odds, 3),
                                "fair_odds": round(fair_over if idx==0 else fair_under, 3),
                                "true_p": round(probs[idx], 4),
                                "edge_%": round(edge*100, 2),
                                "ev_%": round(calc_ev_percent(odds, probs[idx]), 2),
                            })

        # Asian Handicap
        for pts, probs in tp["Handicap"].items():
            if len(probs) != 2: continue
            fair_home, fair_away = 1/probs[0], 1/probs[1]
            offers = offered.get("spreads", {}).get(pts, {})
            for domain, price_map in offers.items():
                for sel, idx in (("home",0), ("away",1)):
                    if sel in price_map:
                        odds = float(price_map[sel])
                        edge = probs[idx] - implied_prob(odds)
                        if edge >= edge_threshold:
                            rows.append({
                                "match": f"{home} - {away}",
                                "kickoff": starts,
                                "market": "Asian Handicap",
                                "selection": f"{'Home' if idx==0 else 'Away'} {pts:+}",
                                "line": pts,
                                "bookmaker": domain,
                                "offered_odds": round(odds, 3),
                                "fair_odds": round(fair_home if idx==0 else fair_away, 3),
                                "true_p": round(probs[idx], 4),
                                "edge_%": round(edge*100, 2),
                                "ev_%": round(calc_ev_percent(odds, probs[idx]), 2),
                            })
    return rows

# UI
st.set_page_config(page_title="Valuebets — Pinnacle vs svenska bolag", layout="wide")
st.title("Valuebets (VM) — Pinnacle baseline vs svenska bolag")

with st.sidebar:
    st.header("Inställningar")
    edge_th = st.slider("Edge‑tröskel (%)", 0.0, 10.0, 2.0, 0.5) / 100.0
    st.caption("Edge = true_p − 1/odds. EV% = förväntad avkastning per insats.")

missing = []
if not PINN_USER: missing.append("PINNACLE_USER")
if not PINN_PASS: missing.append("PINNACLE_PASS")
if not ODDS_API_KEY: missing.append("ODDS_API_KEY")

if missing:
    st.warning("Fyll i Secrets i appens inställningar: " + ", ".join(missing))
    st.stop()

# Hämta data
try:
    league_ids = get_world_cup_league_ids()
    if not league_ids:
        st.info("Hittar inga World Cup‑ligor i API:t — försöker ändå hämta odds som fallback.")
    ml = get_pinn_odds(league_ids, "moneyline")
    tot = get_pinn_odds(league_ids, "totals")
    sp  = get_pinn_odds(league_ids, "spreads")
except requests.HTTPError as e:
    st.error(f"Pinnacle API‑fel: {e}")
    st.stop()

try:
    swe = fetch_swe_odds()
except requests.HTTPError as e:
    st.error(f"Odds‑aggregator fel: {e}")
    st.stop()

events = merge_events(ml, tot, sp)
rows = compute_values(events, swe, edge_threshold=edge_th)
df = pd.DataFrame(rows).sort_values(by=["edge_%","ev_%"], ascending=False) if rows else pd.DataFrame([])

st.subheader("Värdespel")
st.dataframe(df, use_container_width=True)

# Export
col_a, col_b = st.columns(2)
with col_a:
    if not df.empty:
        st.download_button(
            "Ladda ner CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name="valuebets.csv",
            mime="text/csv",
        )
with col_b:
    if not df.empty:
        st.download_button(
            "Ladda ner JSON",
            data=df.to_json(orient="records", force_ascii=False, indent=2),
            file_name="valuebets.json",
            mime="application/json",
        )

st.caption("Visar endast bolag: dbet.se, atg.se, expekt.se, luckysports.se. Uppdatera sidan för nya odds.")
