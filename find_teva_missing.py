"""
Cherche les prix catalogue Astera pour les 2 CIP Teva manquants
et met à jour references_pharmacie.
"""

import os, re, json, psycopg2
from pathlib import Path
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

def load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_env()

USERNAME = os.environ.get("ASTERA_USER", "105216")
PASSWORD = os.environ.get("ASTERA_PASSWORD", "Pharmacie95360!?")
WEBUY_BASE = "https://webuy.astera.coop"
GRP_ID = 84

MISSING_CIPS = ["3400930227978", "3400922495194"]

DB_PARAMS = dict(
    host="aws-0-eu-west-1.pooler.supabase.com", port=5432,
    user="postgres.fmterazwesiwpwjpkyqi", password="lDXWqP1SsuchEIRH",
    dbname="postgres"
)

CIP_PATTERN = re.compile(r'^340\d{10}$')

def nettoyer_prix(s: str):
    s = s.replace('\xa0','').replace(' ','').replace(' ','')
    s = s.replace(',','.').replace('€','').strip()
    try: return float(s)
    except: return None

def search_cip(page, cip13: str):
    """Navigue sur la page de recherche Astera et renvoie le prix catalogue."""
    search_url = f"{WEBUY_BASE}/ART412/ArticlesList?search={cip13}&grpId={GRP_ID}"
    page.goto(search_url, timeout=60000)
    page.wait_for_load_state("networkidle", timeout=30000)
    html = page.content()

    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select("div.item-content"):
        cip_el = card.select_one("div.item-title-2")
        if not cip_el:
            continue
        cip = cip_el.get_text(strip=True)
        if cip != cip13:
            continue
        # Libellé
        name_el = card.select_one("div.item-title, h3.item-title, div.item-name")
        libelle = name_el.get_text(strip=True) if name_el else "(inconnu)"

        # Prix catalogue
        pu_cat = None
        for bloc in card.select("div.item-price-content"):
            label_el = bloc.select_one("span.item-content-title")
            prix_el  = bloc.select_one("span.item-price-discount")
            if not label_el or not prix_el:
                continue
            label = label_el.get_text(strip=True).lower()
            prix  = nettoyer_prix(prix_el.get_text(strip=True))
            if prix is None:
                continue
            if "cat" in label:
                pu_cat = prix
        return libelle, pu_cat

    # Fallback : chercher dans tout le HTML
    print(f"   CIP {cip13} non trouvé dans les cards, extrait HTML brut…")
    idx = html.find(cip13)
    if idx >= 0:
        snippet = html[max(0,idx-200):idx+500]
        print(f"   Snippet : {snippet[:300]}")
    return None, None


def main():
    found = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # Login
        print("🔐  Connexion Astera…")
        page.goto("https://pro.astera.coop/", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        try:
            btn = page.locator("#tarteaucitronAllDenied2, #tarteaucitronPersonalize2").first
            if btn.is_visible(timeout=3000):
                btn.click()
                page.wait_for_load_state("networkidle")
        except Exception:
            pass
        page.locator("#Username").fill(USERNAME)
        page.locator("#password").fill(PASSWORD)
        page.get_by_text("Se connecter").click()
        page.wait_for_load_state("networkidle", timeout=60000)
        print(f"   → {page.url}")

        # SSO webuy
        print("🔑  Session webuy…")
        page.goto(f"{WEBUY_BASE}/Login/Connect?returnUrl=/USR404", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        page.goto(f"{WEBUY_BASE}/USR404/MyShop?grpId={GRP_ID}", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        print(f"   → {page.url}")

        # Recherche chaque CIP
        for cip in MISSING_CIPS:
            print(f"\n🔍  Recherche CIP {cip}…")
            libelle, prix = search_cip(page, cip)
            if prix is not None:
                print(f"   ✅  {libelle} → {prix:.4f}€")
                found[cip] = prix
            else:
                print(f"   ❌  Prix non trouvé (libellé={libelle})")

        browser.close()

    if not found:
        print("\nAucun prix trouvé, base non modifiée.")
        return

    # Mise à jour DB
    print(f"\n💾  Mise à jour de {len(found)} ref(s) en base…")
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    for cip13, prix in found.items():
        cur.execute(
            "UPDATE references_pharmacie SET puht=%s WHERE cip13=%s AND labo='Teva'",
            (prix, cip13)
        )
        print(f"   {cip13} → puht={prix:.4f}  ({cur.rowcount} ligne(s) modifiée(s))")
    conn.commit()
    cur.close()
    conn.close()
    print("✅  Terminé.")


if __name__ == "__main__":
    main()
