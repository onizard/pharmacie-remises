"""
Scraper OSPHARM DATASTAT — Téléchargement automatique des CSV de ventes
URL : https://accounts.dev.ospharm.org/

Prérequis dans .env :
    BP_EMAIL=votre_email_break_pharma
    BP_PASSWORD=votre_mot_de_passe_break_pharma

Les identifiants OSPHARM sont lus depuis votre compte break-pharma.fr
(bouton CONNECTEUR → OSPHARM DATASTAT).

Usage :
    python scraper_ospharm.py
"""

import time
from pathlib import Path
from get_connectors import get_connectors
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ── Configuration ─────────────────────────────────────────────────────────────

LOGIN_URL  = "https://accounts.dev.ospharm.org/"
OUTPUT_DIR = Path("csv_ventes")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("🔑  Récupération des identifiants depuis break-pharma.fr…")
    try:
        creds = get_connectors()
    except Exception as e:
        print(f"❌  {e}")
        return

    USERNAME = creds["ospharm"].get("user", "")
    PASSWORD = creds["ospharm"].get("pass", "")

    if not USERNAME or not PASSWORD:
        print("❌  Identifiants OSPHARM vides.")
        print("    Remplis-les dans break-pharma.fr → bouton CONNECTEUR → OSPHARM DATASTAT.")
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

        try:
            page.locator("input[type='email'], input[name='username'], input[name='email'], #username").first.fill(USERNAME)
            page.locator("input[type='password'], input[name='password'], #password").first.fill(PASSWORD)
            page.locator("button[type='submit'], input[type='submit'], button:has-text('Connexion'), button:has-text('Se connecter'), button:has-text('Login')").first.click()
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            print("⚠️  Timeout lors de la connexion.")

        if "login" in page.url.lower() or "accounts" in page.url.lower():
            print(f"❌  Échec de la connexion (URL : {page.url})")
            browser.close()
            return

        print(f"✅  Connecté ! (URL : {page.url})\n")

        # ── 2. Navigation vers les exports CSV ────────────────────────────────
        # ⚠️  Adapter la navigation selon la structure réelle du site
        print("🔍  Recherche des exports CSV de ventes…\n")

        downloaded = 0
        csv_links  = page.query_selector_all(
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
                    dest     = OUTPUT_DIR / filename
                    download.save_as(dest)
                    print(f"[{i}] ✅  {filename}")
                    downloaded += 1
                    time.sleep(0.5)
                except Exception as e:
                    print(f"[{i}] ❌  {e}")
        else:
            print("⚠️  Aucun lien CSV détecté automatiquement.")
            page.screenshot(path="ospharm_debug.png")
            print("    → Capture sauvegardée : ospharm_debug.png")
            print("    → Partage cette image pour adapter les sélecteurs.")

        browser.close()

    print(f"\n🎉  Terminé : {downloaded} fichier(s) dans « {OUTPUT_DIR.resolve()} »")


if __name__ == "__main__":
    main()
