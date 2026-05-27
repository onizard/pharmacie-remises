"""
test_connector.py — Teste uniquement le login sur OSPHARM ou DIGIPHARMACIE.
Écrit le résultat dans Supabase : state_json.conn_test.{connector}
"""

import asyncio
import json
import os
import sys
import urllib.request

SUPA_URL    = "https://fmterazwesiwpwjpkyqi.supabase.co"
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


# ── Test OSPHARM ───────────────────────────────────────────────────────────────

def test_ospharm(creds: dict):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_context().new_page()

        page.goto(OSPHARM_URL, wait_until="networkidle", timeout=30_000)

        if "datastat.ospharm.org" in page.url and "login" not in page.url and "accounts" not in page.url:
            browser.close()
            return

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

        ok = "datastat.ospharm.org" in page.url and "accounts" not in page.url and "login" not in page.url
        browser.close()

    if not ok:
        raise RuntimeError("Identifiants OSPHARM incorrects")


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


# ── Test DIGIPHARMACIE — fallback camoufox ────────────────────────────────────

async def _test_digipharmacie_async(creds: dict):
    # Chemin rapide : curl_cffi sans navigateur (~5-10s)
    try:
        test_digi_curl(creds)
        print("✅  curl_cffi login réussi")
        return
    except RuntimeError:
        raise  # mauvais credentials — ne pas aller plus loin
    except Exception as curl_err:
        print(f"⚠️  curl_cffi échoué ({curl_err}) — fallback camoufox…")

    # Fallback : navigateur camoufox (gère les challenges JS Cloudflare)
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        raise RuntimeError(
            "Cloudflare bloque les IPs de ce serveur. "
            "Configurez un runner self-hosted sur votre machine pour contourner ce blocage."
        )

    if PROXY_URL:
        import urllib.parse as _up
        _p = _up.urlparse(PROXY_URL)
        proxy_cfg = {
            "server":   f"{_p.scheme}://{_p.hostname}:{_p.port}",
            "username": _p.username or "",
            "password": _p.password or "",
        }
    else:
        proxy_cfg = None

    async with AsyncCamoufox(headless=True, geoip=False) as browser:
        ctx  = await browser.new_context(**({"proxy": proxy_cfg} if proxy_cfg else {}))
        page = await ctx.new_page()

        # Supprimer les pageerror Cloudflare qui crashent le handler Playwright interne
        page.on("pageerror", lambda exc: None)

        try:
            await page.goto(f"{DIGI_URL}/login/", timeout=60_000)
        except Exception:
            raise RuntimeError(
                "DIGIPHARMACIE inaccessible depuis ce serveur "
                "(Cloudflare bloque les IPs Render). "
                "Contactez le support si l'erreur persiste."
            )

        title = await page.title()
        print(f"  Page title after goto: {title!r}  URL: {page.url}")

        # Attendre que le challenge Cloudflare se résolve (titre != "Just a moment...")
        _cf_titles = ("just a moment", "checking", "verifying", "cloudflare")
        if any(k in title.lower() for k in _cf_titles):
            print("  Cloudflare challenge détecté — attente résolution (90s max)…")
            try:
                await page.wait_for_function(
                    "() => !['just a moment','checking','verifying','cloudflare']"
                    ".some(k => document.title.toLowerCase().includes(k))",
                    timeout=90_000, polling=2000,
                )
                title = await page.title()
                print(f"  Challenge résolu. Title: {title!r}")
            except Exception:
                raise RuntimeError(
                    f"Cloudflare challenge non résolu après 90s (URL: {page.url})"
                )

        # Tenter le login via l'API depuis le browser context (cookie CF clearance déjà présent)
        _js_login = """async ({email, password}) => {
            const endpoints = [
                '/api/v1/auth/login/',
                '/api/auth/login/',
                '/api/v1/token/',
                '/api/token/',
            ];
            const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
            for (const ep of endpoints) {
                try {
                    const r = await fetch(ep, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': csrf,
                        },
                        body: JSON.stringify({email, password}),
                        credentials: 'include',
                    });
                    if (r.status === 200) return {ok: true, status: r.status, ep};
                    if (r.status === 400 || r.status === 401)
                        return {ok: false, bad_creds: true, status: r.status, ep};
                } catch(e) {}
            }
            return {ok: false, bad_creds: false, status: 0};
        }"""
        try:
            res = await page.evaluate(_js_login, {"email": creds["user"], "password": creds["pass"]})
            print(f"  API login result: {res}")
            if res.get("ok"):
                return  # succès
            if res.get("bad_creds"):
                raise RuntimeError("Identifiants DIGIPHARMACIE incorrects")
        except RuntimeError:
            raise
        except Exception as js_err:
            print(f"  API evaluate échoué ({js_err}) — fallback formulaire…")

        # Fallback : remplir le formulaire HTML
        _email_sel = (
            "input[type='email'], input[name='email'], "
            "input[name='username'], input[type='text']"
        )
        try:
            await page.wait_for_selector(_email_sel, timeout=20_000)
        except Exception:
            try:
                inputs_info = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('input'))"
                    ".map(i=>({type:i.type,name:i.name,id:i.id}))"
                )
                print(f"  [debug] inputs: {inputs_info}")
                snippet = (await page.content())[:1500]
                print(f"  [debug] content: {snippet}")
            except Exception:
                pass
            raise RuntimeError(
                f"Formulaire de login introuvable (URL: {page.url} — Cloudflare ?)"
            )

        print(f"  Formulaire trouvé. URL: {page.url}")
        await page.locator(_email_sel).first.fill(creds["user"])
        await page.locator("input[type='password']").first.fill(creds["pass"])
        await page.locator("input[type='password']").first.press("Enter")

        try:
            await page.wait_for_url("**/dashboard**", timeout=20_000)
        except Exception:
            try:
                await page.wait_for_function(
                    "() => !window.location.pathname.includes('/login')",
                    timeout=15_000,
                )
            except Exception:
                pass

        url = page.url
        ok  = "/login" not in url

    if not ok:
        raise RuntimeError(f"Identifiants DIGIPHARMACIE incorrects (URL finale : {url})")


def test_digipharmacie(creds: dict):
    asyncio.run(_test_digipharmacie_async(creds))


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
        _mark_connected()

    except Exception as e:
        print(f"❌  {e}")
        _write_result(False, str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
