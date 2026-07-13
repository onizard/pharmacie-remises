"""
Peuple la table price_history (cf. migrations/2026-06_price_history.sql) à partir
des PDF de tarifs labo. Détecte la colonne PFHT par position d'en-tête (robuste
aux mises en page différentes entre éditions Mai / Juin).

Usage : python load_prices_history.py
Prérequis : table price_history créée (migration appliquée), SUPABASE_KEY dans .env.
"""

import os, re, json, urllib.request, collections
from pathlib import Path
import pdfplumber

SUPA_URL = os.environ.get("SUPABASE_URL", "https://api.break-pharma.fr")
SUPA_KEY = os.environ.get("SUPABASE_KEY", "")

# (fichier PDF, date d'effet, libellé source)
TARIFS = [
    ("Tarif Zydus France Mai 2026.pdf",  "2026-05-01", "Tarif Zydus France Mai 2026"),
    ("Tarif Zydus France Juin 2026.pdf", "2026-06-01", "Tarif Zydus France Juin 2026"),
]
LABO = "Zydus"


def num(s):
    s = s.replace("€", "").replace(" ", "").replace("\xa0", "").replace(",", ".").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_tarif(path):
    """Renvoie {cip13: puht}. Colonne PFHT détectée via la position du header 'PFHT'."""
    pdf = pdfplumber.open(path)
    out = {}
    for page in pdf.pages:
        words = page.extract_words()
        # x de la colonne PFHT depuis l'en-tête
        xs = [w for w in words if w["text"].upper() == "PFHT"]
        if not xs:
            continue
        px0 = min(w["x0"] for w in xs) - 12
        px1 = max(w["x1"] for w in xs) + 12
        rows = collections.defaultdict(list)
        for w in words:
            if w["top"] > min(x["top"] for x in xs) + 6:  # sous l'en-tête
                rows[round(w["top"] / 3)].append(w)
        for wl in rows.values():
            wl = sorted(wl, key=lambda z: z["x0"])
            cip = next((re.sub(r"\D", "", w["text"]) for w in wl
                        if len(re.sub(r"\D", "", w["text"])) == 13
                        and re.sub(r"\D", "", w["text"]).startswith("34")), None)
            if not cip:
                continue
            toks = [w["text"] for w in wl if px0 <= w["x0"] <= px1]
            joined = "".join(toks).replace(" ", "").replace("\xa0", "").replace("€", "").replace(",", ".")
            m = re.search(r"\d+\.\d+|\d+", joined)
            if m:
                out[cip] = float(m.group(0))
    return out


def upsert(rows):
    req = urllib.request.Request(
        f"{SUPA_URL}/rest/v1/price_history?on_conflict=cip13,effective_date",
        data=json.dumps(rows).encode(), method="POST",
        headers={"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal"})
    return urllib.request.urlopen(req, timeout=60).status


def main():
    payload = []
    for pdf_name, eff, source in TARIFS:
        if not Path(pdf_name).exists():
            print(f"⚠️  {pdf_name} introuvable, ignoré")
            continue
        prices = parse_tarif(pdf_name)
        print(f"{pdf_name}: {len(prices)} prix (effet {eff})")
        for cip, puht in prices.items():
            payload.append({"cip13": cip, "labo": LABO, "effective_date": eff,
                            "puht": puht, "source": source})
    if not SUPA_KEY:
        print(f"\n(dry-run : {len(payload)} lignes prêtes — SUPABASE_KEY absent, pas d'insertion)")
        return
    for i in range(0, len(payload), 500):
        upsert(payload[i:i + 500])
    print(f"✅ {len(payload)} lignes insérées dans price_history")


if __name__ == "__main__":
    main()
