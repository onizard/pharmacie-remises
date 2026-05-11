"""
Scraper OSPHARM DATASTAT — Ventes produits 2025
Flux : OAuth PKCE → Analyse des ventes → Toutes les ventes
        → période Année précédente → onglet Produits → export CSV

Prérequis dans .env :
    BP_EMAIL=votre_email_break_pharma
    BP_PASSWORD=votre_mot_de_passe_break_pharma

Les identifiants OSPHARM sont lus depuis votre compte break-pharma.fr
(bouton CONNECTEUR → OSPHARM DATASTAT).

Usage :
    python scraper_ospharm.py
"""

import csv
import hashlib
import base64
import secrets
import time
from pathlib import Path
from get_connectors import get_connectors
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ── Configuration ─────────────────────────────────────────────────────────────

OUTPUT_DIR    = Path("csv_ventes")
CLIENT_ID     = "c44d25be-29b4-4379-a38a-83eb1473f5bd"
CLIENT_SECRET = "02b7df13-cec6-4808-afb2-d04635a7ae1f"
REDIRECT_URI  = "https://datastat.ospharm.org"
AUTH_BASE     = "https://accounts.dev.ospharm.org/"

# ── Helpers ───────────────────────────────────────────────────────────────────

def pkce_challenge():
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return challenge


def js_click_text(page, text):
    """Click the first visible element whose trimmed text matches `text` via JS."""
    return page.evaluate(f'''() => {{
        const all = document.querySelectorAll(
            ".webix_list_item, .webix_el_button button, button, [role=option], [role=button]"
        );
        for (const el of all) {{
            if (el.textContent.trim() === {repr(text)}) {{
                el.click();
                return true;
            }}
        }}
        return false;
    }}''')


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

    login_url = (
        f"{AUTH_BASE}"
        f"?client_id={CLIENT_ID}"
        f"&client_secret={CLIENT_SECRET}"
        f"&code_challenge_method=S256"
        f"&code_challenge={pkce_challenge()}"
        f"&redirect_uri={REDIRECT_URI}"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page    = context.new_page()

        # ── 1. Connexion OAuth ────────────────────────────────────────────────
        print("🔐  Connexion à OSPHARM DATASTAT…")
        page.goto(login_url, wait_until="networkidle", timeout=30000)

        try:
            page.locator(
                "input[type='email'], input[name='username'], input[name='email']"
            ).first.fill(USERNAME, timeout=10000)
            page.locator(
                "input[type='password'], input[name='password']"
            ).first.fill(PASSWORD, timeout=5000)
            page.locator(
                "button[type='submit'], input[type='submit']"
            ).first.click(timeout=5000)
            # Wait for redirect — use networkidle; ignore timeout (SPA hash routing)
            try:
                page.wait_for_url("*datastat.ospharm.org*", timeout=30000)
            except PlaywrightTimeoutError:
                pass  # hash-based SPA: URL may already be correct
        except PlaywrightTimeoutError as e:
            print(f"❌  Timeout lors du remplissage du formulaire : {e}")
            page.screenshot(path="ospharm_login_debug.png")
            browser.close()
            return

        # Verify we landed on datastat
        if "datastat.ospharm.org" not in page.url:
            print(f"❌  Échec de la connexion (URL : {page.url})")
            page.screenshot(path="ospharm_login_debug.png")
            browser.close()
            return

        print(f"✅  Connecté ! (URL : {page.url})\n")
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(2000)

        # ── 2. Navigation vers Toutes les ventes ──────────────────────────────
        print("🔍  Navigation vers Analyse des ventes → Toutes les ventes…")

        # Try sidebar navigation first, fall back to direct URL
        navigated = False
        for label in ["Analyse des ventes", "Toutes les ventes"]:
            try:
                page.get_by_text(label, exact=True).first.click(timeout=8000)
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(1000)
                navigated = True
            except Exception:
                pass

        if not navigated or "sellout" not in page.url:
            page.goto(
                "https://datastat.ospharm.org/#!/top/sellout.all",
                wait_until="networkidle", timeout=20000
            )
            page.wait_for_timeout(2000)

        print(f"  URL : {page.url}")

        # ── 3. Sélection période Année précédente (01/01/2025–31/12/2025) ─────
        print("📅  Sélection de la période 2025…")
        page.wait_for_timeout(1500)

        # Open the date picker (first htmlbutton)
        try:
            page.locator("button.webix_el_htmlbutton").first.click(timeout=10000)
            page.wait_for_timeout(1200)
        except Exception as e:
            print(f"  ⚠️  Impossible d'ouvrir le sélecteur de dates : {e}")
            page.screenshot(path="ospharm_datepicker_debug.png")

        # Click "Année précédente" via JS (bypasses Webix visibility check)
        ok = js_click_text(page, "Année précédente")
        if not ok:
            print("  ⚠️  JS click échoué, tentative force click…")
            try:
                page.get_by_text("Année précédente", exact=True).first.click(
                    force=True, timeout=5000
                )
                ok = True
            except Exception as e2:
                print(f"  ❌  Impossible de sélectionner 'Année précédente' : {e2}")

        if ok:
            print("  ✓  'Année précédente' sélectionnée")
            page.wait_for_timeout(500)

            # Click "Valider"
            val_ok = js_click_text(page, "Valider")
            if not val_ok:
                try:
                    page.get_by_text("Valider", exact=True).first.click(
                        force=True, timeout=5000
                    )
                    val_ok = True
                except Exception as e3:
                    print(f"  ⚠️  'Valider' non trouvé : {e3}")

            if val_ok:
                print("  ✓  'Valider' cliqué")
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(3000)
        else:
            print("  ❌  Impossible de sélectionner la période 2025.")
            page.screenshot(path="ospharm_period_debug.png")

        # ── 4. Onglet Produits ────────────────────────────────────────────────
        print("📦  Clic sur l'onglet Produits…")

        prod_ok = page.evaluate('''() => {
            // Webix tabbar items or any visible element with text "Produits"
            const candidates = document.querySelectorAll(
                ".webix_item_tab, .webix_list_item, [class*='tab'], li, span, div, a"
            );
            for (const el of candidates) {
                if (el.textContent.trim() === "Produits" && el.offsetParent !== null) {
                    el.click();
                    return true;
                }
            }
            return false;
        }''')

        if not prod_ok:
            try:
                page.get_by_text("Produits", exact=True).first.click(timeout=8000)
                prod_ok = True
            except Exception as e:
                print(f"  ⚠️  Onglet Produits non trouvé : {e}")
                page.screenshot(path="ospharm_produits_debug.png")

        if prod_ok:
            print("  ✓  Onglet Produits sélectionné")
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(3000)

        # ── 5. Extraction via Webix JS API ────────────────────────────────────
        print("📊  Extraction des données via Webix…")

        result = page.evaluate('''() => {
            if (typeof webix === "undefined") return { error: "webix non défini" };

            const views = Object.values(webix.ui.views || {});
            const grids = views.filter(v => v.name === "datatable" && v.isVisible());

            if (!grids.length) return { error: "aucun datatable visible" };

            const grid = grids[0];

            // Build column map: id → human label
            const columns = (grid.config.columns || []).map(c => {
                let label = c.id;
                if (typeof c.header === "string") {
                    label = c.header;
                } else if (Array.isArray(c.header)) {
                    label = c.header
                        .map(h => (typeof h === "string" ? h : (h && h.text ? h.text : "")))
                        .filter(Boolean)
                        .join(" ");
                }
                return { id: String(c.id), label: label || String(c.id) };
            });

            const rows = [];
            grid.eachRow(id => {
                const item = grid.getItem(id);
                if (item) rows.push(item);
            });

            return { columns, rows, total: rows.length };
        }''')

        if not isinstance(result, dict):
            print(f"  ❌  Résultat inattendu : {result}")
            page.screenshot(path="ospharm_extract_debug.png")
        elif "error" in result:
            print(f"  ❌  Erreur Webix : {result['error']}")
            page.screenshot(path="ospharm_extract_debug.png")
            print("  → Capture sauvegardée : ospharm_extract_debug.png")
        else:
            total   = result["total"]
            columns = result["columns"]
            rows    = result["rows"]
            print(f"  ✓  {total} lignes × {len(columns)} colonnes")

            out_file = OUTPUT_DIR / "ventes_produits_2025_ospharm.csv"
            with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
                headers = [c["label"] for c in columns]
                col_ids = [c["id"]    for c in columns]
                writer  = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({
                        h: row.get(cid, "")
                        for h, cid in zip(headers, col_ids)
                    })

            print(f"\n🎉  Fichier sauvegardé : {out_file.resolve()}")

        browser.close()


if __name__ == "__main__":
    main()
