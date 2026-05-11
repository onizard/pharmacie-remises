"""
Scraper OSPHARM DATASTAT — Téléchargement automatique des CSV de ventes
URL : https://accounts.dev.ospharm.org/

Dépendances :
    pip install playwright
    playwright install chromium

Usage :
    python scraper_ospharm.py
"""

import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ── Chargement .env ───────────────────────────────────────────────────────────

def load_env(filepath=None):
    env_path = Path(filepath) if filepath else Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ[key.strip()] = value

load_env()

# ── Configuration ─────────────────────────────────────────────────────────────

LOGIN_URL  = "https://accounts.dev.ospharm.org/"
USERNAME   = os.environ.get("OSPHARM_USER", "")
PASSWORD   = os.environ.get("OSPHARM_PASSWORD", "")
OUTPUT_DIR = Path("csv_ventes")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not USERNAME or not PASSWORD:
        print("❌  OSPHARM_USER et OSPHARM_PASSWORD non définis dans le fichier .env")
        return

    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"📁  Dossier de sortie : {OUTPUT_DIR.resolve()}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()

        # ── 1. Connexion ──────────────────────────────────────────────────────
        print("🔐  Connexion à OSPHARM DATASTAT…")
        page.goto(LOGIN_URL, wait_until="networkidle")

        # Remplissage du formulaire de connexion
        # ⚠️  Adapter les sélecteurs après inspection du site
        try:
            page.locator("input[type='email'], input[name='username'], input[name='email'], #username").first.fill(USERNAME)
            page.locator("input[type='password'], input[name='password'], #password").first.fill(PASSWORD)
            page.locator("button[type='submit'], input[type='submit'], button:has-text('Connexion'), button:has-text('Se connecter'), button:has-text('Login')").first.click()
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            print("⚠️  Timeout lors de la connexion.")

        if "login" in page.url.lower() or "accounts" in page.url.lower():
            print(f"❌  Échec de la connexion (URL : {page.url})")
            print("    Vérifie OSPHARM_USER / OSPHARM_PASSWORD dans le fichier .env")
            browser.close()
            return

        print(f"✅  Connecté ! (URL : {page.url})\n")

        # ── 2. Navigation vers les exports CSV ────────────────────────────────
        # ⚠️  Adapter la navigation selon la structure du site OSPHARM DATASTAT
        print("🔍  Recherche des exports CSV de ventes…\n")

        downloaded = 0

        # Stratégie : chercher des liens d'export CSV ou des boutons de téléchargement
        csv_links = page.query_selector_all(
            "a[href*='.csv'], a[href*='export'], a[href*='download'], "
            "button:has-text('Export'), button:has-text('CSV'), "
            "a:has-text('Export'), a:has-text('Télécharger'), a:has-text('CSV')"
        )

        if csv_links:
            print(f"📦  {len(csv_links)} lien(s) CSV détecté(s).\n")
            for i, link in enumerate(csv_links, 1):
                try:
                    with context.expect_download(timeout=30000) as dl_info:
                        link.click()
                    download = dl_info.value
                    filename = download.suggested_filename or f"ventes_{i}.csv"
                    dest = OUTPUT_DIR / filename
                    download.save_as(dest)
                    print(f"[{i}] ✅  {filename}")
                    downloaded += 1
                    time.sleep(0.5)
                except Exception as e:
                    print(f"[{i}] ❌  Erreur : {e}")
        else:
            print("⚠️  Aucun lien CSV détecté automatiquement.")
            print("    Prends une capture pour inspecter la page :")
            page.screenshot(path="ospharm_debug.png")
            print("    → Capture sauvegardée : ospharm_debug.png")
            print("    → Inspecte cette image et adapte les sélecteurs dans ce script.")

        browser.close()

    print(f"\n🎉  Terminé : {downloaded} fichier(s) téléchargé(s) dans « {OUTPUT_DIR.resolve()} »")


if __name__ == "__main__":
    main()
