"""
test_connector.py — Teste uniquement le login sur OSPHARM ou DIGIPHARMACIE.
Écrit le résultat dans Supabase : state_json.conn_test.{connector}
"""

import json
import os
import sys
import urllib.request

SUPA_URL    = "https://api.break-pharma.fr"
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
USER_ID     = os.environ.get("USER_ID", "")
CONNECTOR   = os.environ.get("CONNECTOR", "")
PROXY_URL   = os.environ.get("PROXY_URL", "")   # http://user:pass@host:port — IP résidentielle

OSPHARM_URL = "https://datastat.ospharm.org/"
DIGI_URL    = "https://app.digipharmacie.fr"

# Endpoints JSON à tenter dans l'ordre
DIGI_LOGIN_APIS = [
    "/api/v1/auth/login/",
    "/api/auth/login/",
    "/api/v1/token/",
    "/api/token/",
]


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


def _get_connectors_col() -> dict:
    """Lit la colonne connectors (nouvelle architecture atomique)."""
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}&select=connectors&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    return (rows[0].get("connectors") or {}) if rows else {}


def _get_creds() -> dict:
    # Priorité 1 : env vars passées par le subprocess main.py
    env_user = os.environ.get("DIGI_USER", "")
    env_pass = os.environ.get("DIGI_PASS", "")
    if env_user and env_pass:
        return {"user": env_user, "pass": env_pass}

    # Priorité 2 : colonne connectors dédiée (nouvelle architecture)
    try:
        conns = _get_connectors_col()
        cred  = conns.get(CONNECTOR, {})
        if cred.get("user") and cred.get("pass"):
            return {"user": cred["user"], "pass": cred["pass"]}
    except Exception:
        pass

    # Priorité 3 : legacy state_json.connectors
    state = _get_state()
    cred  = state.get("connectors", {}).get(CONNECTOR, {})
    return {"user": cred.get("user", ""), "pass": cred.get("pass", "")}


def _mark_connected():
    """Appelle upsert_connector pour marquer connected=true après succès du test."""
    try:
        conns = _get_connectors_col()
        cred  = conns.get(CONNECTOR, {})
        if not cred.get("user"):
            return
        url  = f"{SUPA_URL}/rest/v1/rpc/upsert_connector"
        body = json.dumps({
            "p_user_id":   USER_ID,
            "p_connector": CONNECTOR,
            "p_login":     cred.get("user", ""),
            "p_pass":      cred.get("pass", ""),
            "p_connected": True,
        }).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15): pass
    except Exception as e:
        print(f"⚠️  _mark_connected: {e}")


# ── Test OSPHARM — POST /authorize (pas de navigateur) ─────────────────────────

OSPHARM_AUTH_URL = "https://accounts.dev.ospharm.org"
OSPHARM_REDIRECT = "https://datastat.ospharm.org/"
FSE_REDIRECT     = "https://fse.ospharm.org/"

_CHROMIUM_ARGS = [
    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
    "--disable-extensions", "--disable-background-networking",
    "--disable-sync", "--disable-translate", "--mute-audio",
    "--no-first-run", "--safebrowsing-disable-auto-update",
    "--js-flags=--max-old-space-size=128",
]

def _ospharm_playwright_login(username: str, password: str, success_domain: str, form_redirect: str):
    """Login via Playwright Chromium — navigation directe vers l'app (pas de client_id hardcodé)."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        ctx  = browser.new_context(java_script_enabled=True, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        final_url = ""
        try:
            # Naviguer vers l'app — Keycloak redirige automatiquement vers sa page de login
            page.goto(form_redirect, wait_until="domcontentloaded", timeout=30_000)
            # Attendre la page de login Keycloak (accounts.*.ospharm.org)
            try:
                page.wait_for_url("**ospharm.org**", timeout=10_000)
            except PWTimeout:
                pass
            page.locator("input[name='username'],input[type='email'],input[type='text']").first.fill(username, timeout=10_000)
            page.locator("input[type='password']").first.fill(password, timeout=5_000)
            page.locator("button[type='submit'],input[type='submit'],input[type='password']").last.press("Enter")
            try:
                page.wait_for_url(f"**{success_domain}**", timeout=25_000)
            except PWTimeout:
                pass
            final_url = page.url
            ok = success_domain in final_url and OSPHARM_AUTH_URL not in final_url
        finally:
            browser.close()
    if not ok:
        raise RuntimeError(f"Identifiants incorrects (URL finale : {final_url[:80]})")


def test_ospharm(creds: dict):
    _ospharm_playwright_login(creds["user"], creds["pass"], "datastat.ospharm.org", OSPHARM_REDIRECT)


# ── Test CONCENTRATEUR (OSPHARM FSE / Resopharma) ─────────────────────────────

def test_concentrateur(creds: dict):
    _ospharm_playwright_login(creds["user"], creds["pass"], "fse.ospharm.org", FSE_REDIRECT)


# ── Test DIGIPHARMACIE — chemin rapide curl_cffi ───────────────────────────────

def test_digi_curl(creds: dict):
    """
    Teste les credentials via curl_cffi (TLS Chrome impersonation).
    - Retourne normalement si succès.
    - RuntimeError si credentials incorrects.
    - Exception (non RuntimeError) si Cloudflare bloque — le caller bascule sur camoufox.
    """
    from curl_cffi import requests as cffi_requests

    # curl_cffi prend proxy= (singulier, str) par requête — pas proxies= dict
    proxy_kw = {"proxy": PROXY_URL} if PROXY_URL else {}
    session = cffi_requests.Session(impersonate="chrome124")
    page_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
    }

    # Étape 1 — charger la page de login pour obtenir le cookie CSRF
    try:
        r = session.get(f"{DIGI_URL}/login/", headers=page_headers, timeout=25,
                        allow_redirects=True, **proxy_kw)
    except Exception as e:
        raise Exception(f"curl_cffi GET /login/ : {e}")

    if r.status_code in (403, 503) or len(r.text) < 200:
        raise Exception(f"Cloudflare bloque curl_cffi (HTTP {r.status_code}, {len(r.text)} octets)")

    csrf = session.cookies.get("csrftoken", "")
    if not csrf:
        raise Exception("Pas de cookie csrftoken — Cloudflare ou challenge JS requis")

    api_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-CSRFToken": csrf,
        "Referer": f"{DIGI_URL}/login/",
        "Origin": DIGI_URL,
        "Accept-Language": "fr-FR,fr;q=0.9",
    }

    # Étape 2 — essayer les endpoints JSON connus
    for endpoint in DIGI_LOGIN_APIS:
        try:
            r = session.post(
                f"{DIGI_URL}{endpoint}",
                json={"email": creds["user"], "password": creds["pass"]},
                headers=api_headers,
                timeout=15,
                allow_redirects=False,
                **proxy_kw,
            )
            if r.status_code == 200:
                return  # succès
            if r.status_code in (400, 401):
                raise RuntimeError("Identifiants DIGIPHARMACIE incorrects")
            # 404 / 405 → mauvais endpoint, on essaie le suivant
        except RuntimeError:
            raise
        except Exception:
            continue

    # Étape 3 — fallback form POST sur /login/
    try:
        r = session.post(
            f"{DIGI_URL}/login/",
            data={
                "email": creds["user"],
                "password": creds["pass"],
                "csrfmiddlewaretoken": csrf,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{DIGI_URL}/login/",
                "Origin": DIGI_URL,
            },
            timeout=15,
            allow_redirects=True,
            **proxy_kw,
        )
        if "/login" not in r.url:
            return  # redirigé vers le dashboard → succès
        raise RuntimeError("Identifiants DIGIPHARMACIE incorrects")
    except RuntimeError:
        raise
    except Exception as e:
        raise Exception(f"curl_cffi form POST: {e}")


# ── Test DIGIPHARMACIE — fallback Playwright Chromium ────────────────────────

def test_digipharmacie(creds: dict):
    # Fast path : curl_cffi sans navigateur (~5-10s)
    try:
        test_digi_curl(creds)
        print("✅  curl_cffi login réussi")
        return
    except RuntimeError:
        raise  # mauvais credentials — ne pas aller plus loin
    except Exception as curl_err:
        print(f"⚠️  curl_cffi échoué ({curl_err}) — fallback Playwright Chromium…")

    # Fallback : Playwright Chromium (Firefox/camoufox non disponible sur ce runner)
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    _email_sel = (
        "input[type='email'], input[name='email'], "
        "input[name='username'], input[type='text']"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        ctx  = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        final_url = ""
        ok = False
        try:
            page.goto(f"{DIGI_URL}/login/", wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_selector(_email_sel, timeout=20_000)
            except PWTimeout:
                inputs_info = page.evaluate(
                    "() => Array.from(document.querySelectorAll('input'))"
                    ".map(i=>({type:i.type,name:i.name,id:i.id}))"
                )
                print(f"  [debug] inputs: {inputs_info}")
                raise RuntimeError(
                    f"Formulaire de login introuvable (URL: {page.url} — Cloudflare ?)"
                )
            print(f"  Formulaire trouvé. URL: {page.url}")
            page.locator(_email_sel).first.fill(creds["user"])
            page.locator("input[type='password']").first.fill(creds["pass"])
            page.locator("input[type='password']").first.press("Enter")
            try:
                page.wait_for_url("**/dashboard**", timeout=20_000)
            except PWTimeout:
                try:
                    page.wait_for_function(
                        "() => !window.location.pathname.includes('/login')",
                        timeout=15_000,
                    )
                except PWTimeout:
                    pass
            final_url = page.url
            ok = "/login" not in final_url
        finally:
            browser.close()

    if not ok:
        raise RuntimeError(f"Identifiants DIGIPHARMACIE incorrects (URL finale : {final_url[:80]})")


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
        elif CONNECTOR == "concentrateur":
            test_concentrateur(creds)
        elif CONNECTOR == "digipharmacie":
            test_digipharmacie(creds)
        else:
            raise ValueError(f"Connecteur inconnu : {CONNECTOR}")

        print(f"✅  Connexion {CONNECTOR} réussie")
        _write_result(True, "Connexion réussie")
        _mark_connected()

    except Exception as e:
        print(f"❌  {e}")
        _write_result(False, str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
