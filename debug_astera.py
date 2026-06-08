"""Debug : cherche la bonne URL de recherche Astera pour un CIP."""

import os, re
from pathlib import Path
from playwright.sync_api import sync_playwright

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

TARGET_CIP = "3400930227978"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # Login
        print("🔐  Connexion…")
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
        page.goto(f"{WEBUY_BASE}/Login/Connect?returnUrl=/USR404", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        page.goto(f"{WEBUY_BASE}/USR404/MyShop?grpId={GRP_ID}", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        page.goto(f"{WEBUY_BASE}/ART412", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        print(f"   ART412 URL: {page.url}")

        # Essayer la recherche via le champ texte
        print(f"\n🔍  Tentative recherche '{TARGET_CIP}' via input…")
        try:
            # Trouver l'input de recherche
            inp = page.locator("input[type='search'], input[placeholder*='search'], input[placeholder*='recherch'], input[name*='search'], #search, .search-input").first
            if inp.count() > 0:
                print(f"   Input trouvé, saisie…")
                inp.fill(TARGET_CIP)
                page.keyboard.press("Enter")
                page.wait_for_load_state("networkidle", timeout=30000)
                print(f"   URL après recherche: {page.url}")
                html = page.content()
                if TARGET_CIP in html:
                    idx = html.find(TARGET_CIP)
                    print(f"   ✅  CIP trouvé dans le HTML!")
                    print(html[max(0,idx-300):idx+600])
                else:
                    print(f"   CIP non trouvé. Début HTML:")
                    print(html[:2000])
            else:
                print("   Aucun input trouvé")
        except Exception as e:
            print(f"   Erreur: {e}")

        # Essai URL directe avec différents paramètres
        for test_url in [
            f"{WEBUY_BASE}/ART412/ArticlesList?q={TARGET_CIP}",
            f"{WEBUY_BASE}/ART412/ArticlesList?SearchText={TARGET_CIP}",
            f"{WEBUY_BASE}/ART412/ArticlesList?cip={TARGET_CIP}",
            f"{WEBUY_BASE}/ART412/ArticlesList?code={TARGET_CIP}",
            f"{WEBUY_BASE}/ART412/ArticlesList?filter={TARGET_CIP}",
        ]:
            page.goto(test_url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            html = page.content()
            if TARGET_CIP in html:
                print(f"\n✅  Trouvé avec URL: {test_url}")
                idx = html.find(TARGET_CIP)
                print(html[max(0,idx-300):idx+600])
                break
            else:
                print(f"   ❌ {test_url.split('?')[1]} → non trouvé")

        browser.close()

if __name__ == "__main__":
    main()
