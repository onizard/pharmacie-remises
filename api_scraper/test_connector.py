"""
test_connector.py — Teste uniquement le login sur OSPHARM ou DIGIPHARMACIE.
Écrit le résultat dans Supabase : state_json.conn_test.{connector}
"""

import json
import os
import sys
import urllib.request

SUPA_URL    = "https://fmterazwesiwpwjpkyqi.supabase.co"
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
USER_ID     = os.environ["USER_ID"]
CONNECTOR   = os.environ["CONNECTOR"]   # 'ospharm' or 'digipharmacie'

OSPHARM_URL = "https://datastat.ospharm.org/"


# ── Supabase ───────────────────────────────────────────────────────────────────

def _get_state() -> dict:
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}&select=state_json&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    return rows[0]["state_json"] if rows else {}


def _write_result(ok: bool, message: str = ""):
    state = _get_state()
    state.setdefault("conn_test", {})[CONNECTOR] = {
        "status":  "ok" if ok else "fail",
        "message": message,
    }
    url  = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}"
    body = json.dumps({"state_json": state}).encode()
    req  = urllib.request.Request(url, data=body, method="PATCH", headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    })
    with urllib.request.urlopen(req, timeout=15): pass


def _get_creds() -> dict:
    state = _get_state()
    cred  = state.get("connectors", {}).get(CONNECTOR, {})
    return {"user": cred.get("user", ""), "pass": cred.get("pass", "")}


# ── Test OSPHARM ───────────────────────────────────────────────────────────────

def test_ospharm(creds: dict):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_context().new_page()

        # Aller sur le site directement — il redirige vers le formulaire OAuth
        page.goto(OSPHARM_URL, wait_until="networkidle", timeout=30_000)

        try:
            page.locator("input[type='email'],input[name='username'],input[name='email']").first.fill(creds["user"], timeout=15_000)
            page.locator("input[type='password'],input[name='password']").first.fill(creds["pass"], timeout=5_000)
            page.locator("button[type='submit'],input[type='submit']").first.click(timeout=5_000)
            try:
                page.wait_for_url("*datastat.ospharm.org*", timeout=25_000)
            except PWTimeout:
                pass
        except PWTimeout as e:
            browser.close()
            raise RuntimeError(f"Timeout formulaire : {e}")

        ok  = "datastat.ospharm.org" in page.url and "accounts" not in page.url
        url = page.url
        browser.close()

    if not ok:
        raise RuntimeError(f"Identifiants OSPHARM incorrects (URL finale : {url})")


# ── Test DIGIPHARMACIE ─────────────────────────────────────────────────────────

def test_digipharmacie(creds: dict):
    from camoufox.sync_api import Camoufox

    with Camoufox(headless=True, geoip=True) as browser:
        page = browser.new_page()
        page.goto("https://app.digipharmacie.fr/login/", timeout=60_000)

        try:
            page.wait_for_selector("input[type='email']", timeout=40_000)
        except Exception:
            raise RuntimeError("Formulaire de login DIGIPHARMACIE introuvable (Cloudflare ?)")

        page.locator("input[type='email']").first.fill(creds["user"])
        page.locator("input[type='password']").first.fill(creds["pass"])
        page.locator("input[type='password']").first.press("Enter")

        try:
            page.wait_for_url("**/dashboard**", timeout=20_000)
        except Exception:
            try:
                page.wait_for_function(
                    "() => !window.location.pathname.includes('/login')",
                    timeout=15_000,
                )
            except Exception:
                pass

        ok  = "/login" not in page.url
        url = page.url
        page.close()

    if not ok:
        raise RuntimeError(f"Identifiants DIGIPHARMACIE incorrects (URL finale : {url})")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"🔍  Test connexion {CONNECTOR} pour user_id={USER_ID}")
    creds = _get_creds()

    if not creds["user"] or not creds["pass"]:
        _write_result(False, "Identifiants vides")
        sys.exit(1)

    try:
        if CONNECTOR == "ospharm":
            test_ospharm(creds)
        elif CONNECTOR == "digipharmacie":
            test_digipharmacie(creds)
        else:
            raise ValueError(f"Connecteur inconnu : {CONNECTOR}")

        print(f"✅  Connexion {CONNECTOR} réussie")
        _write_result(True, "Connexion réussie")

    except Exception as e:
        print(f"❌  {e}")
        _write_result(False, str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
