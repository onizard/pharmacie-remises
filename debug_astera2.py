"""Debug : inspecte la structure HTML de ART412 + cherche le CIP dans les pages."""

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

TARGET_CIPS = {"3400930227978", "3400922495194"}

def extraire_nb_pages(html: str) -> int:
    m = re.search(r'page=(\d+)">Fin', html)
    if m:
        return int(m.group(1))
    pages = re.findall(r'page=(\d+)', html)
    return max(int(p) for p in pages) if pages else 1

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

        # SSO webuy
        page.goto(f"{WEBUY_BASE}/Login/Connect?returnUrl=/USR404", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        page.goto(f"{WEBUY_BASE}/USR404/MyShop?grpId={GRP_ID}", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)

        # Charger ART412 page 1 et voir la structure
        page.goto(f"{WEBUY_BASE}/ART412/ArticlesList?page=1", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)
        html1 = page.content()

        nb_pages = extraire_nb_pages(html1)
        print(f"Nb pages détectées: {nb_pages}")

        # Afficher les 200 premiers caractères du premier item-content
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html1, "html.parser")

        # Chercher les inputs de recherche
        inputs = soup.select("input")
        print(f"\nInputs trouvés sur la page: {len(inputs)}")
        for inp in inputs[:10]:
            print(f"  <input type={inp.get('type')} name={inp.get('name')} id={inp.get('id')} placeholder={inp.get('placeholder')}>")

        # Forms
        forms = soup.select("form")
        print(f"\nForms trouvés: {len(forms)}")
        for form in forms[:5]:
            print(f"  action={form.get('action')} method={form.get('method')}")
            for inp in form.select("input")[:5]:
                print(f"    <input type={inp.get('type')} name={inp.get('name')} id={inp.get('id')}>")

        # Chercher dans toutes les pages
        print(f"\n🔍  Recherche dans {nb_pages} pages…")

        # Bloquer ressources
        def bloquer(route):
            if route.request.resource_type in ("image","font","stylesheet","media"):
                route.abort()
            else:
                route.continue_()
        ctx.route("**/*", bloquer)

        found = {}
        for p_num in range(1, min(nb_pages+1, 200)):  # max 200 pages
            url = f"{WEBUY_BASE}/ART412/ArticlesList?page={p_num}"
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            html = page.content()
            for cip in list(TARGET_CIPS):
                if cip in html:
                    print(f"   ✅  CIP {cip} trouvé page {p_num}!")
                    idx = html.find(cip)
                    print(html[max(0,idx-300):idx+600])
                    found.add(cip) if isinstance(found, set) else None
                    TARGET_CIPS.discard(cip)
            if p_num % 10 == 0:
                print(f"   page {p_num}/{nb_pages}…", end="\r")
            if not TARGET_CIPS:
                break

        if TARGET_CIPS:
            print(f"\n❌  CIPs non trouvés après {nb_pages} pages: {TARGET_CIPS}")

        browser.close()

if __name__ == "__main__":
    main()
