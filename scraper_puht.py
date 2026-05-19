"""
Scraper PUHT Webuy - Récupère les prix PU HT par CIP13 depuis webuy.astera.coop/ART412.
Portail : webuy.astera.coop (remplace pro.astera.coop depuis 2025)
Login via agora.cerp.fr (OIDC SSO).
Génère puht_astera.json : {cip13: puht_float}

Usage :
    python scraper_puht.py
"""

import os, re, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# ── Chargement .env ───────────────────────────────────────────────────────────

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

USERNAME  = os.environ.get("ASTERA_USER", "")
PASSWORD  = os.environ.get("ASTERA_PASSWORD", "")
OUTPUT    = Path("puht_astera.json")
GRP_ID    = 84          # CERP — catalogue pharmaceutique
PAGE_SIZE = 64

WEBUY_BASE  = "https://webuy.astera.coop"
LIST_URL    = f"{WEBUY_BASE}/ART412/ArticlesList"

CIP_PATTERN = re.compile(r'^340\d{10}$')

# ── Helpers ───────────────────────────────────────────────────────────────────

def nettoyer_prix(s: str) -> float | None:
    s = s.replace('\xa0', '').replace(' ', '').replace(' ', '')
    s = s.replace(',', '.').replace('€', '').strip()
    try:
        return float(s)
    except ValueError:
        return None

def parser_page(html: str) -> dict:
    """Extrait les CIP pharma → (pu_cat, pu_net) depuis le HTML d'une page ART412."""
    soup = BeautifulSoup(html, "html.parser")
    resultats = {}

    for card in soup.select("div.item-content"):
        # CIP
        cip_el = card.select_one("div.item-title-2")
        if not cip_el:
            continue
        cip = cip_el.get_text(strip=True)
        if not CIP_PATTERN.match(cip):
            continue

        # Prix : cherche les blocs item-price-content
        pu_cat = None
        pu_net = None
        for bloc in card.select("div.item-price-content"):
            label_el = bloc.select_one("span.item-content-title")
            prix_el  = bloc.select_one("span.item-price-discount")
            if not label_el or not prix_el:
                continue
            label = label_el.get_text(strip=True).lower()
            prix  = nettoyer_prix(prix_el.get_text(strip=True))
            if prix is None:
                continue
            if "net" in label:
                pu_net = prix
            elif "cat" in label:
                pu_cat = prix

        # Priorité : PU catalogue (brut) > PU net — le brut est la base de calcul du CA
        prix_final = pu_cat if pu_cat is not None else pu_net
        if prix_final is not None:
            resultats[cip] = prix_final

    return resultats

def extraire_nb_pages(html: str) -> int:
    """Extrait le nombre total de pages depuis le HTML."""
    m = re.search(r'page=(\d+)">Fin', html)
    if m:
        return int(m.group(1))
    # Fallback : cherche le dernier numéro de page dans la pagination
    pages = re.findall(r'page=(\d+)', html)
    return max(int(p) for p in pages) if pages else 1

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not USERNAME or not PASSWORD:
        print("❌  Variables ASTERA_USER / ASTERA_PASSWORD non définies dans .env")
        return

    prix_total: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx  = browser.new_context()
        page = ctx.new_page()

        # ── 1. Login agora.cerp.fr (OIDC) ────────────────────────────────────
        print("🔐  Connexion à Astera Pro…")
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

        if "agora.cerp.fr" not in page.url and "login" in page.url:
            print("❌  Échec de la connexion.")
            browser.close()
            return
        print(f"✅  Connecté ({page.url})")

        # ── 2. SSO → webuy.astera.coop grpId=84 (CERP pharma) ────────────────
        print(f"🔑  Établissement session webuy (grpId={GRP_ID})…")
        page.goto(f"{WEBUY_BASE}/Login/Connect?returnUrl=/USR404", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        page.goto(f"{WEBUY_BASE}/USR404/MyShop?grpId={GRP_ID}", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        page.goto(f"{WEBUY_BASE}/ART412", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        print(f"   → {page.url}")

        # ── 3. Bloque images/fonts pour accélérer ────────────────────────────
        def bloquer_ressources(route):
            if route.request.resource_type in ("image", "font", "stylesheet", "media"):
                route.abort()
            else:
                route.continue_()
        ctx.route("**/*", bloquer_ressources)

        # ── 4. Page 1 — déjà chargée, récupère le HTML ───────────────────────
        html1 = page.content()
        nb_pages = extraire_nb_pages(html1)
        print(f"\n📦  {nb_pages} pages détectées ({nb_pages * PAGE_SIZE:,} articles max)")
        prix_total.update(parser_page(html1))
        print(f"   page   1/{nb_pages}  ({len(prix_total)} prix CIP pharma)", end="\r")

        # ── 5. Pages suivantes via Playwright (session conservée) ─────────────
        for num in range(2, nb_pages + 1):
            try:
                page.goto(f"{LIST_URL}?page={num}", timeout=30000)
                page.wait_for_load_state("domcontentloaded", timeout=20000)
                nouveaux = parser_page(page.content())
                prix_total.update(nouveaux)
                print(f"   page {num:4d}/{nb_pages}  ({len(prix_total)} prix CIP pharma)", end="\r")
                time.sleep(0.05)
            except Exception as e:
                print(f"\n   ⚠️  page {num} : {e}")

        browser.close()

    print(f"\n✅  {len(prix_total)} prix CIP pharma récupérés")

    # ── 6. Merge avec le fichier existant ─────────────────────────────────────
    if OUTPUT.exists():
        anciens = json.loads(OUTPUT.read_text(encoding="utf-8"))
        anciens.update(prix_total)
        prix_total = anciens

    OUTPUT.write_text(json.dumps(prix_total, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✅  {len(prix_total)} prix sauvegardés dans {OUTPUT}")
    print(f"📁  {OUTPUT.resolve()}")


if __name__ == "__main__":
    main()
