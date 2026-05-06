"""
Scraper Astera Pro (nouveau portail agora.cerp.fr) - Téléchargement automatique des PDFs
Répertoire : https://agora.cerp.fr/mes-achats/offres-generiques

Dépendances :
    pip install playwright
    playwright install chromium

Usage :
    1. Crée un fichier .env dans le même dossier que ce script :
        ASTERA_USER='ton_code_utilisateur'
        ASTERA_PASSWORD='ton_mot_de_passe'

    2. Lance simplement :
        python scraper_astera.py
"""

import os
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urljoin
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ── Chargement du fichier .env ────────────────────────────────────────────────

def load_env(filepath=None):
    env_path = Path(filepath) if filepath else Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ[key.strip()] = value

load_env()

# ── Configuration ─────────────────────────────────────────────────────────────

LOGIN_URL  = "https://pro.astera.coop/"   # redirige vers OIDC login2-web.astera.coop
BASE_URL   = "https://agora.cerp.fr"
OFFRES_URL = "https://agora.cerp.fr/mes-achats/offres-generiques"

USERNAME  = os.environ.get("ASTERA_USER", "")
PASSWORD  = os.environ.get("ASTERA_PASSWORD", "")
OUTPUT_DIR = Path("pdfs_remises")

# Mapping code → nom labo (utilisé pour nommer les fichiers)
LABO_CODES = {
    "GE01": "Arrow",
    "GE02": "Biogaran",
    "GE03": "EG Labo",
    "GE04": "Viatris",
    "GE05": "Pfizer",
    "GE06": "Sandoz",
    "GE07": "Teva",
    "GE08": "Zentiva",
    "GE12": "Cristers",
    "GE13": "Zydus",
    "P510": "Correvio",
    "P656": "Abacus",
    "EE04": "Viatris First",
    "P167": "Wegovy",
    "ZZ":   "Biogaran Depositaire",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def code_to_filename(code: str) -> str:
    labo = LABO_CODES.get(code, code)
    return f"{code} - {labo} - Liste des CIP d_offres Partenariat ciblees.pdf"

def dismiss_cookies(page):
    try:
        btn = page.locator("#tarteaucitronAllDenied2, #tarteaucitronPersonalize2").first
        if btn.is_visible(timeout=3000):
            btn.click()
            page.wait_for_load_state("networkidle")
            print("  🍪  Bannière cookies fermée.")
    except Exception:
        pass

def download_pdf_via_cookies(context, url: str, dest: Path) -> bool:
    """Télécharge un PDF en réutilisant les cookies de session Playwright."""
    try:
        cookies = context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        req = urllib.request.Request(url, headers={
            "Cookie": cookie_str,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Referer": BASE_URL,
            "Accept": "application/pdf,*/*",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read()
            if len(data) > 500 and ("pdf" in content_type.lower() or data[:4] == b"%PDF"):
                dest.write_bytes(data)
                return True
            else:
                print(f"  ⚠️  Réponse inattendue (Content-Type: {content_type}, taille: {len(data)} octets)")
    except Exception as e:
        print(f"  ❌  Erreur : {e}")
    return False

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not USERNAME or not PASSWORD:
        print("❌  Variables ASTERA_USER et ASTERA_PASSWORD non définies.")
        print("    Crée un fichier .env avec ces deux variables.")
        return

    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"📁  Dossier de sortie : {OUTPUT_DIR.resolve()}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page    = context.new_page()

        # ── 1. Connexion (OIDC via login2-web.astera.coop) ────────────────────
        print("🔐  Connexion à Astera Pro…")
        page.goto(LOGIN_URL, wait_until="networkidle")
        dismiss_cookies(page)

        page.locator("#Username").fill(USERNAME)
        page.locator("#password").fill(PASSWORD)
        page.get_by_text("Se connecter").click()
        page.wait_for_load_state("networkidle")

        if "agora.cerp.fr" not in page.url and "login" in page.url:
            print(f"❌  Échec de la connexion (URL: {page.url}). Vérifie tes identifiants dans le fichier .env")
            browser.close()
            return

        print(f"✅  Connecté ! (URL: {page.url})\n")

        # ── 2. Scan de la page des offres génériques ──────────────────────────
        print(f"🔍  Scan de la page : {OFFRES_URL}\n")
        page.goto(OFFRES_URL, wait_until="networkidle")

        # Cherche les codes d'offres dans les liens PDF ou les attributs data-
        pdf_entries = []  # liste de (code, url)

        # Stratégie A : liens directs vers /api/generic-offers/{code}/pdf
        api_pattern = re.compile(r'/api/generic-offers/([^/]+)/pdf', re.IGNORECASE)
        anchors = page.query_selector_all("a[href*='generic-offers'], a[href$='.pdf'], a[href$='.PDF']")
        for anchor in anchors:
            href = anchor.get_attribute("href") or ""
            m = api_pattern.search(href)
            if m:
                code = m.group(1)
                url = urljoin(BASE_URL, href) if not href.startswith("http") else href
                pdf_entries.append((code, url))
            elif href.lower().endswith(".pdf"):
                url = urljoin(BASE_URL, href) if not href.startswith("http") else href
                code = url.split("/")[-1].replace(".pdf", "").replace(".PDF", "")
                pdf_entries.append((code, url))

        # Stratégie B : construire les URLs depuis LABO_CODES si rien trouvé
        if not pdf_entries:
            print("⚠️  Aucun PDF détecté automatiquement → construction depuis la liste connue.\n")
            pdf_entries = [
                (code, f"{BASE_URL}/api/generic-offers/{code}/pdf")
                for code in LABO_CODES
            ]

        # Dédoublonnage
        seen = set()
        unique_entries = []
        for code, url in pdf_entries:
            if url not in seen:
                seen.add(url)
                unique_entries.append((code, url))
        pdf_entries = unique_entries

        print(f"📦  {len(pdf_entries)} PDF(s) détecté(s).\n")

        # ── 3. Téléchargement via cookies de session ──────────────────────────
        print("⬇️  Téléchargement en cours…\n")
        success = 0

        for i, (code, url) in enumerate(pdf_entries, 1):
            filename = code_to_filename(code)
            dest     = OUTPUT_DIR / filename

            print(f"[{i}/{len(pdf_entries)}] {filename}")

            if dest.exists():
                print(f"  ⏭️  Déjà téléchargé, ignoré.")
                success += 1
                continue

            if download_pdf_via_cookies(context, url, dest):
                print(f"  ✅  Sauvegardé")
                success += 1

            time.sleep(0.3)

        browser.close()

    print(f"\n🎉  Terminé : {success}/{len(pdf_entries)} PDF(s) téléchargés dans « {OUTPUT_DIR.resolve()} »")


if __name__ == "__main__":
    main()
