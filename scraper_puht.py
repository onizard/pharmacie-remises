"""
Scraper PUHT Astera - Récupère les prix PU HT par CIP13 depuis les pages ART123.
Pagination : navigation par texte du lien (page suivante = lien dont le texte = N+1).
Si le lien n'est pas dans la fenêtre visible, on clique le dernier lien visible
pour avancer la fenêtre, puis on re-cherche.
Génère puht_astera.json : {cip13: puht_float}

Usage :
    python scraper_puht.py
"""

import os
import re
import json
import math
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

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

LOGIN_URL = "https://pro.astera.coop/"
USERNAME  = os.environ.get("ASTERA_USER", "")
PASSWORD  = os.environ.get("ASTERA_PASSWORD", "")
OUTPUT         = Path("puht_astera.json")
CIPS_FILE      = Path("cips_manquants.json")
SEARCH_INPUT   = "#m_ctl00_main_tbxTexteRecherche"
SEARCH_BUTTON  = "#m_ctl00_main_cmdRechercher"
PUHT_SPAN      = "#m_ctl00_main_m_lblPrixHT"
SEARCH_BASE    = "https://pro.astera.coop/ART/ART123.aspx"

PAGES = [
    "https://pro.astera.coop/ART/ART123.aspx?pn=P383",
    "https://pro.astera.coop/ART/ART123.aspx?pn=0929",
    "https://pro.astera.coop/ART/ART123.aspx?pn=0952",
    "https://pro.astera.coop/ART/ART123.aspx?pn=P510",
    "https://pro.astera.coop/ART/ART123.aspx?pn=0950",
    "https://pro.astera.coop/ART/ART123.aspx?pn=0957",
    "https://pro.astera.coop/ART/ART123.aspx?pn=0850",
    "https://pro.astera.coop/ART/ART123.aspx?pn=0951",
]

PER_PAGE = 15

# ── Helpers ───────────────────────────────────────────────────────────────────

def nettoyer_prix(s: str) -> float | None:
    s = s.replace("\xa0", "").replace(" ", "").replace(",", ".").replace("€", "").strip()
    try:
        return float(s)
    except ValueError:
        return None

def extraire_cip13(texte: str) -> str | None:
    chiffres = re.sub(r"\D", "", texte)
    return chiffres if len(chiffres) == 13 else None

def lire_articles(page) -> dict:
    """Extrait les CIP13→PUHT de la page actuellement affichée."""
    resultats = {}
    page.wait_for_selector(".ligneArticle", timeout=15000)
    for ligne in page.query_selector_all(".ligneArticle"):
        cip_el  = ligne.query_selector(".codeColumn")
        prix_el = ligne.query_selector(".priceColumn")
        if not cip_el or not prix_el:
            continue
        cip13 = extraire_cip13(cip_el.inner_text())
        puht  = nettoyer_prix(prix_el.inner_text())
        if cip13 and puht is not None:
            resultats[cip13] = puht
    return resultats

def aller_a_page(page, target: int) -> bool:
    """
    Navigue vers la page target (1-indexé) en cherchant le lien dont le texte == str(target).
    Si le lien n'est pas dans la fenêtre visible, clique le dernier lien pour avancer la
    fenêtre, puis réessaie — jusqu'à 30 tentatives.
    Après le clic, attend que l'indicateur .paginationCurrentPageNumber affiche target.
    """
    js_condition = (
        f"Array.from(document.querySelectorAll('.paginationCurrentPageNumber'))"
        f".some(el => el.innerText.trim() === '{target}')"
    )
    for _ in range(30):
        liens = page.query_selector_all("a.paginationPageNumber")
        for lien in liens:
            if lien.inner_text().strip() == str(target):
                lien.click()
                try:
                    page.wait_for_function(js_condition, timeout=20000)
                except Exception:
                    return False
                return True
        # Lien non visible : avancer la fenêtre via le dernier lien visible
        if liens:
            last_text = liens[-1].inner_text().strip()
            liens[-1].click()
            # Attendre que la page se charge (indicateur ≥ dernier lien cliqué)
            try:
                page.wait_for_function(
                    f"Array.from(document.querySelectorAll('.paginationCurrentPageNumber'))"
                    f".some(el => parseInt(el.innerText.trim() || '0') >= {last_text or 0})",
                    timeout=15000
                )
            except Exception:
                page.wait_for_load_state("networkidle")
        else:
            return False
    return False

def est_page_login(page) -> bool:
    """Retourne True si la page actuelle est une page de login."""
    url = page.url
    return any(x in url.lower() for x in ["login", "usr101", "signin", "connect"])

def scraper_url(page, url: str) -> dict:
    resultats = {}
    pn = url.split("pn=")[1]

    page.goto(url, timeout=60000)
    page.wait_for_load_state("networkidle", timeout=60000)

    if est_page_login(page):
        raise RuntimeError(f"Redirigé vers login ({page.url}) — session expirée")

    # Total et nombre de pages
    pg_text  = page.locator(".paginationTotalRecords").first.inner_text(timeout=30000)
    m        = re.search(r'sur\s+un\s+total\s+de\s+([\d\s]+)', pg_text)
    total    = int(re.sub(r"\s", "", m.group(1))) if m else PER_PAGE
    nb_pages = math.ceil(total / PER_PAGE)

    print(f"  pn={pn:6s}  {total:4d} articles  {nb_pages:3d} pages")

    # Page 1 — déjà chargée
    resultats.update(lire_articles(page))
    print(f"    page   1/{nb_pages}  ({len(resultats)} prix)", end="\r")

    # Pages suivantes : navigation par texte du lien
    for target in range(2, nb_pages + 1):
        ok = aller_a_page(page, target)
        if not ok:
            print(f"\n    ⚠️  lien page {target} introuvable, arrêt")
            break

        resultats.update(lire_articles(page))
        print(f"    page {target:3d}/{nb_pages}  ({len(resultats)} prix)", end="\r")
        time.sleep(0.05)

    ok_str = "✅" if len(resultats) >= total * 0.99 else "⚠️ "
    print(f"    {ok_str}  {len(resultats)}/{total} prix récupérés" + " " * 25)
    return resultats


def scraper_cips(page, cips: list[dict]) -> dict:
    """
    Recherche chaque CIP via la barre de recherche Astera et extrait le PU HT
    depuis la fiche produit ART120.aspx.
    cips : liste de {cip, libelle, labo}
    Retourne {cip13: puht_float}.
    """
    resultats = {}
    total = len(cips)
    print(f"\n🔍  Recherche individuelle de {total} CIP(s) manquants…")

    for i, item in enumerate(cips, 1):
        cip     = item["cip"]
        libelle = item.get("libelle", "?")
        print(f"  {i:2d}/{total}  {cip}  {libelle}", end="\r")

        try:
            # Navigue vers la page de recherche et lance la recherche
            page.goto(SEARCH_BASE, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)
            if est_page_login(page):
                raise RuntimeError(f"Redirigé vers login ({page.url})")
            page.locator(SEARCH_INPUT).fill(cip)
            page.locator(SEARCH_BUTTON).click()
            page.wait_for_load_state("networkidle")

            # Vérifie qu'on est bien sur une fiche ART120
            if "ART120" not in page.url:
                print(f"  {i:2d}/{total}  {cip}  ⚠️  pas de fiche directe (url={page.url})")
                continue

            # Extrait le PU HT
            span = page.locator(PUHT_SPAN).first
            if span.count() == 0:
                print(f"  {i:2d}/{total}  {cip}  ⚠️  span PU HT introuvable")
                continue

            puht = nettoyer_prix(span.inner_text())
            if puht is None:
                print(f"  {i:2d}/{total}  {cip}  ⚠️  prix illisible : {span.inner_text()!r}")
                continue

            resultats[cip] = puht
            print(f"  {i:2d}/{total}  {cip}  {libelle:<45s}  {puht:.2f} €")
            time.sleep(0.2)

        except Exception as e:
            print(f"  {i:2d}/{total}  {cip}  ⚠️  erreur : {e}")

    ok = len(resultats)
    print(f"\n  ✅  {ok}/{total} prix récupérés par recherche CIP")
    return resultats


def main():
    if not USERNAME or not PASSWORD:
        print("❌  Variables ASTERA_USER / ASTERA_PASSWORD non définies dans .env")
        return

    prix_total: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_context().new_page()

        # ── Connexion ─────────────────────────────────────────────────────────
        print("🔐  Connexion à Astera Pro…")
        page.goto(LOGIN_URL)
        page.wait_for_load_state("networkidle")
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
        page.wait_for_load_state("networkidle")

        if "agora.cerp.fr" not in page.url and "login" in page.url:
            print("❌  Échec de la connexion.")
            browser.close()
            return
        print(f"✅  Connecté ({page.url})\n")

        # ── Établir la session sur pro.astera.coop ─────────────────────────────
        # Après login OIDC → agora.cerp.fr, il faut naviguer sur pro.astera.coop
        # pour que le SSO y établisse aussi la session.
        print("🔑  Établissement de la session sur pro.astera.coop…")
        page.goto("https://pro.astera.coop/", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        if est_page_login(page):
            # Le SSO devrait se déclencher automatiquement ; attendre un peu
            page.wait_for_url("**/pro.astera.coop/**", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=30000)
        print(f"   → {page.url}")

        for url in PAGES:
            try:
                resultats = scraper_url(page, url)
                prix_total.update(resultats)
            except Exception as e:
                print(f"\n  ⚠️  Erreur pour {url} : {e}")

        # ── Recherche individuelle des CIPs manquants ──────────────────────────
        if CIPS_FILE.exists():
            cips = json.loads(CIPS_FILE.read_text(encoding="utf-8"))
            # Filtre ceux déjà connus
            cips_a_chercher = [c for c in cips if c["cip"] not in prix_total]
            if cips_a_chercher:
                resultats_cips = scraper_cips(page, cips_a_chercher)
                prix_total.update(resultats_cips)
            else:
                print("\n✅  Tous les CIPs manquants sont déjà dans le cache.")

        browser.close()

    # Merge avec les prix déjà connus (préserve les labs qui auraient échoué)
    if OUTPUT.exists():
        anciens = json.loads(OUTPUT.read_text(encoding="utf-8"))
        anciens.update(prix_total)
        prix_total = anciens

    OUTPUT.write_text(json.dumps(prix_total, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n✅  {len(prix_total)} prix sauvegardés dans {OUTPUT}")
    print(f"📁  {OUTPUT.resolve()}")


if __name__ == "__main__":
    main()
