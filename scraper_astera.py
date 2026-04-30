"""
Scraper Astera Pro - Téléchargement automatique de tous les PDFs
Répertoire : https://pro.astera.coop/DNL/PTN/

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

LOGIN_URL   = "https://pro.astera.coop/PUB/USR101.aspx?ReturnUrl=%2fUSR%2fUSR106.aspx"
PARTENARIAT_URL = "https://pro.astera.coop/USR/USR131.aspx"
BASE_URL    = "https://pro.astera.coop"

USERNAME    = os.environ.get("ASTERA_USER", "")
PASSWORD    = os.environ.get("ASTERA_PASSWORD", "")
OUTPUT_DIR  = Path("pdfs_remises")

# ── Liste de secours ──────────────────────────────────────────────────────────

FALLBACK_URLS = [
    "https://pro.astera.coop/DNL/PTN/GE13%20-%20Zydus%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE01%20-%20Arrow%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE02%20-%20Biogaran%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE12%20-%20Cristers%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/EG%20LABO%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE04%20-%20Viatris%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE05%20-%20Pfizer%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE06%20-%20Sandoz%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE08%20-%20Zentiva%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/GE07%20-%20Teva%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/P510%20-%20CORREVIO%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
    "https://pro.astera.coop/DNL/PTN/P656%20-%20ABACUS%20-%20Liste%20des%20CIP%20d_offres%20Partenariat%20ciblees.pdf",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def url_to_filename(url: str) -> str:
    name = unquote(url.split("/")[-1])
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name

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

        # ── 1. Connexion ──────────────────────────────────────────────────────
        print("🔐  Connexion à Astera Pro…")
        page.goto(LOGIN_URL)
        page.wait_for_load_state("networkidle")
        dismiss_cookies(page)

        page.locator("#ctl00_ctl00_main_m_logLogin_UserName").fill(USERNAME)
        page.locator("#ctl00_ctl00_main_m_logLogin_Password").fill(PASSWORD)
        page.locator("#ctl00_ctl00_main_m_logLogin_btnLogin").click()
        page.wait_for_load_state("networkidle")

        error_visible = page.locator(".failureNotification, .loginError, [id*='Failure']").is_visible()
        if error_visible:
            print("❌  Échec de la connexion. Vérifie tes identifiants dans le fichier .env")
            browser.close()
            return

        print(f"✅  Connecté ! (URL: {page.url})\n")

        
        print(f"🔍  Scan de la page partenariat : {PARTENARIAT_URL}\n")
        page.goto(PARTENARIAT_URL)
        page.wait_for_load_state("networkidle")

        anchors = page.query_selector_all("a[href$='.pdf'], a[href$='.PDF']")
        pdf_urls = []
        for anchor in anchors:
            href = anchor.get_attribute("href") or ""
            if href.startswith("http"):
                pdf_urls.append(href)
            elif href.startswith("/"):
                pdf_urls.append(BASE_URL + href)
            else:
                pdf_urls.append(urljoin(PARTENARIAT_URL, href))

        pdf_urls = list(dict.fromkeys(pdf_urls))

        if not pdf_urls:
            print("⚠️  Aucun PDF détecté automatiquement → utilisation de la liste connue.\n")
            pdf_urls = FALLBACK_URLS

        print(f"📦  {len(pdf_urls)} PDF(s) détecté(s).\n")

        # ── 3. Téléchargement via cookies de session ──────────────────────────
        print("⬇️  Téléchargement en cours…\n")
        success = 0

        for i, url in enumerate(pdf_urls, 1):
            filename = url_to_filename(url)
            dest     = OUTPUT_DIR / filename

            print(f"[{i}/{len(pdf_urls)}] {filename}")

            if dest.exists():
                print(f"  ⏭️  Déjà téléchargé, ignoré.")
                success += 1
                continue

            if download_pdf_via_cookies(context, url, dest):
                print(f"  ✅  Sauvegardé")
                success += 1

            time.sleep(0.3)

        browser.close()

    import subprocess
    subprocess.run(["find", str(OUTPUT_DIR), "-name", "*.pdf", "!", "-name", "*CIP*", "-delete"])
    print(f"\n🎉  Terminé : {success}/{len(pdf_urls)} PDF(s) téléchargés dans « {OUTPUT_DIR.resolve()} »")


if __name__ == "__main__":
    main()