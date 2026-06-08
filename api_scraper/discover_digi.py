"""
discover_digi.py — Capture la structure brute de l'API Digipharmacie.
Stocke dans Supabase : state_json.digi_discover = {sample, fields, doc_types}
Ne télécharge aucun PDF.
"""

import asyncio
import json
import os
import urllib.request

SUPA_URL    = "https://api.break-pharma.fr"
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
USER_ID     = os.environ.get("USER_ID", "")
PROXY_URL   = os.environ.get("PROXY_URL", "")
BASE_URL    = "https://app.digipharmacie.fr"
PAGE_SIZE   = 50


# ── Supabase ───────────────────────────────────────────────────────────────────

def _get_state() -> dict:
    key = SERVICE_KEY
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}&select=state_json&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": key, "Authorization": f"Bearer {key}",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    return rows[0]["state_json"] if rows else {}


def _save_discover(result: dict):
    state = _get_state()
    state["digi_discover"] = result
    url  = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}"
    body = json.dumps({"state_json": state}).encode()
    req  = urllib.request.Request(url, data=body, method="PATCH", headers={
        "apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    })
    with urllib.request.urlopen(req, timeout=15): pass


def _get_creds() -> dict:
    key = SERVICE_KEY
    url = f"{SUPA_URL}/rest/v1/user_state?user_id=eq.{USER_ID}&select=connectors&limit=1"
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    conns = (rows[0].get("connectors") or {}) if rows else {}
    cred  = conns.get("digipharmacie", {})
    return {"user": cred.get("user", ""), "pass": cred.get("pass", "")}


# ── Analyse ────────────────────────────────────────────────────────────────────

def _summarize_doc(doc: dict) -> dict:
    """Résumé d'un document : tous les champs avec leur type et valeur tronquée."""
    return {
        k: {"type": type(v).__name__, "value": str(v)[:120]}
        for k, v in doc.items()
    }


def _classify_hint(doc: dict) -> str:
    """Essaie de deviner le type de document depuis ses champs."""
    total = doc.get("total") or doc.get("amount") or 0
    try:
        total = float(str(total).replace(",", "."))
    except Exception:
        total = 0

    doc_type = str(doc.get("document_type") or doc.get("type") or "").lower()
    provider = str(doc.get("provider_ref") or doc.get("provider_name") or "").lower()
    client   = str(doc.get("client_ref") or doc.get("client_name") or "").lower()

    if doc_type:
        return doc_type
    if total < 0:
        return "avoir/credit (total négatif)"
    if "pharmacie" in provider or "montmagny" in provider:
        return "sortant (pharmacie=fournisseur)"
    if client and "pharmacie" in client:
        return "entrant (pharmacie=client)"
    return "?"


# ── Scraper découverte ──────────────────────────────────────────────────────────

async def _discover_async(creds: dict) -> dict:
    from camoufox.async_api import AsyncCamoufox

    proxy_cfg = None
    if PROXY_URL:
        import urllib.parse as _up
        _p = _up.urlparse(PROXY_URL)
        proxy_cfg = {
            "server":   f"{_p.scheme}://{_p.hostname}:{_p.port}",
            "username": _p.username or "",
            "password": _p.password or "",
        }

    # ── Phase 1 : curl_cffi login (fast path — bypasses Cloudflare CSRF cache issue) ──
    session_cookies: dict = {}
    try:
        from curl_cffi import requests as cffi_requests
        proxy_kw = {"proxy": PROXY_URL} if PROXY_URL else {}
        session = cffi_requests.Session(impersonate="chrome124")
        page_hdrs = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9",
        }
        r = session.get(f"{BASE_URL}/login/", headers=page_hdrs, timeout=25,
                        allow_redirects=True, **proxy_kw)
        csrf = session.cookies.get("csrftoken", "")
        print(f"  curl_cffi GET /login/ → {r.status_code}  csrf_len={len(csrf)}")
        if csrf:
            api_hdrs = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-CSRFToken": csrf,
                "Referer": f"{BASE_URL}/login/",
                "Origin": BASE_URL,
            }
            for ep in ["/api/v1/auth/login/", "/api/auth/login/", "/api/v1/token/", "/api/token/"]:
                try:
                    rp = session.post(f"{BASE_URL}{ep}",
                                      json={"email": creds["user"], "password": creds["pass"]},
                                      headers=api_hdrs, timeout=15,
                                      allow_redirects=False, **proxy_kw)
                    print(f"    {ep} → {rp.status_code}")
                    if rp.status_code == 200:
                        session_cookies = dict(session.cookies)
                        print(f"  curl_cffi login OK via {ep}")
                        break
                    if rp.status_code in (400, 401):
                        raise RuntimeError("Identifiants DIGIPHARMACIE incorrects")
                except RuntimeError:
                    raise
                except Exception:
                    continue
            if not session_cookies:
                # Form POST fallback
                rp = session.post(f"{BASE_URL}/login/",
                                  data={"email": creds["user"], "password": creds["pass"],
                                        "csrfmiddlewaretoken": csrf},
                                  headers={"Content-Type": "application/x-www-form-urlencoded",
                                           "Referer": f"{BASE_URL}/login/"},
                                  timeout=15, allow_redirects=True, **proxy_kw)
                print(f"  curl_cffi form POST → {rp.status_code}  url={rp.url}")
                if "/login" not in rp.url:
                    session_cookies = dict(session.cookies)
                    print(f"  curl_cffi form login OK")
    except RuntimeError:
        raise
    except Exception as ce:
        print(f"  curl_cffi échoué ({ce}) — fallback camoufox…")

    async with AsyncCamoufox(headless=True, geoip=False) as browser:
        ctx  = await browser.new_context(**({"proxy": proxy_cfg} if proxy_cfg else {}))

        # Injecter les cookies de session curl_cffi si disponibles
        if session_cookies:
            await ctx.add_cookies([
                {"name": k, "value": v, "domain": "app.digipharmacie.fr", "path": "/",
                 "sameSite": "Lax"}
                for k, v in session_cookies.items()
            ])
            print(f"  Session cookies injectés: {list(session_cookies.keys())}")

        page = await ctx.new_page()
        page.on("pageerror", lambda e: None)
        page.set_default_timeout(120_000)

        if not session_cookies:
            # Pas de session curl_cffi — login via camoufox (même code que test_connector.py)
            print("  Login via camoufox…")
            await page.goto(f"{BASE_URL}/login/", timeout=60_000)
            title = await page.title()
            print(f"  Titre après goto: {title!r}")
            _cf_kw = ("just a moment", "checking", "verifying", "cloudflare")
            if any(k in title.lower() for k in _cf_kw):
                print("  Cloudflare challenge — attente résolution…")
                await page.wait_for_function(
                    "() => !['just a moment','checking','verifying','cloudflare']"
                    ".some(k => document.title.toLowerCase().includes(k))",
                    polling=2000,
                )
                title = await page.title()
                print(f"  Challenge résolu. Titre: {title!r}")
            # Remplir le formulaire — intercepter les requêtes réseau pour diagnostiquer
            login_responses: list[dict] = []
            async def _capture_login(response):
                url_r = response.url
                if any(k in url_r for k in ("login", "auth", "token", "session")):
                    try:
                        body_t = await response.text()
                    except Exception:
                        body_t = ""
                    login_responses.append({
                        "url": url_r, "status": response.status,
                        "method": response.request.method, "body": body_t[:400],
                    })
            page.on("response", _capture_login)

            sel = ("input[type='email'], input[name='email'], "
                   "input[name='username'], input[type='text']")
            await page.wait_for_selector(sel, timeout=60_000)
            print(f"  Formulaire trouvé. URL: {page.url}")

            # Lire les inputs présents pour vérifier qu'on cible le bon
            inputs_info = await page.evaluate(
                "() => Array.from(document.querySelectorAll('input'))"
                ".map(i=>({type:i.type,name:i.name,id:i.id,placeholder:i.placeholder}))"
            )
            print(f"  Inputs présents: {inputs_info}")

            # Cibler l'email et le mot de passe avec fill() (dispatche input + change)
            await page.locator(sel).first.fill(creds["user"])
            await page.locator("input[type='password']").first.fill(creds["pass"])

            # Vérifier les valeurs dans les champs
            email_val = await page.locator(sel).first.input_value()
            pass_len_val = len(await page.locator("input[type='password']").first.input_value())
            print(f"  Champs après fill: email={email_val[:6]}*** ({len(email_val)}c)  pass_len={pass_len_val}")

            # Essai direct de l'endpoint réel /auth/login/ depuis le contexte navigateur
            direct_res = await page.evaluate("""async ({email, password}) => {
                const r = await fetch('/auth/login/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({email, password}),
                    credentials: 'include',
                });
                const body = await r.text();
                return {status: r.status, body: body.slice(0, 300)};
            }""", {"email": creds["user"], "password": creds["pass"]})
            print(f"  Direct /auth/login/ → {direct_res}")

            if direct_res.get("status") == 200:
                print("  Login JS direct réussi !")
                page.remove_listener("response", _capture_login)
                # Session créée — naviguer maintenant vers /factures/
            else:
                # Cliquer sur le bouton de soumission (une seule fois)
                try:
                    await page.locator("button[type='submit'], input[type='submit']").first.click(timeout=3_000)
                except Exception:
                    await page.keyboard.press("Enter")

                # Attendre la navigation ou le timeout
                await page.wait_for_timeout(10_000)
                page.remove_listener("response", _capture_login)

                print(f"  Réponses login: {login_responses}")
                print(f"  URL après submit: {page.url}  title: {await page.title()!r}")
                try:
                    err_txt = await page.evaluate("""() => {
                        const el = document.querySelector('[class*="error"],[class*="alert"],[class*="Error"],[class*="Alert"]');
                        return el ? el.textContent.trim().slice(0, 300) : '';
                    }""")
                    if err_txt:
                        print(f"  Message erreur: {err_txt!r}")
                except Exception:
                    pass

                if "/login" in page.url:
                    raise RuntimeError(f"Login échoué — URL: {page.url}")

        print(f"  Connecté — URL: {page.url}  navigation /factures/…")

        # Intercepter la première réponse API
        raw_pages: list[dict] = []
        captured  = asyncio.Event()

        async def on_response(response):
            if captured.is_set():
                return
            url = response.url
            if ("invoice" in url or "facture" in url.lower()) and response.status == 200:
                try:
                    body = await response.json()
                    if isinstance(body, dict) and ("results" in body or "count" in body):
                        raw_pages.append(body)
                        captured.set()
                except Exception:
                    pass

        page.on("response", on_response)
        await page.goto(f"{BASE_URL}/factures/", wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(5000)
        page.off("response", on_response)

        # Si pas capturé via event, appel direct
        if not raw_pages:
            print("  Tentative fetch direct…")
            _ctx_cks = await ctx.cookies()
            csrf2 = next((c["value"] for c in _ctx_cks if c["name"] == "csrftoken"), "")
            if not csrf2:
                csrf2 = await page.evaluate(
                    "() => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || ''"
                )
            result = await page.evaluate("""async ([url, csrf]) => {
                const r = await fetch(url, {
                    credentials:'include',
                    headers:{'Accept':'application/json','X-CSRFToken':csrf,'X-Requested-With':'XMLHttpRequest'}
                });
                return {status: r.status, text: await r.text()};
            }""", [
                f"{BASE_URL}/api/v1/invoices/?ordering=-billing_date&page_size={PAGE_SIZE}&page=1",
                csrf2,
            ])
            if result["status"] == 200:
                raw_pages.append(json.loads(result["text"]))

        await page.close()

    if not raw_pages:
        return {"error": "Aucune réponse API capturée"}

    data    = raw_pages[0]
    docs    = data.get("results", data if isinstance(data, list) else [])
    total   = data.get("count", len(docs))

    print(f"  {total} documents au total, analyse des {len(docs)} premiers…")

    # Collecter tous les champs distincts
    all_fields: dict[str, set] = {}
    for doc in docs:
        for k, v in doc.items():
            all_fields.setdefault(k, set()).add(type(v).__name__)

    # Classifier les documents
    classified: list[dict] = []
    for doc in docs:
        classified.append({
            "hint":       _classify_hint(doc),
            "summary":    _summarize_doc(doc),
        })

    # Grouper par type de hint
    type_counts: dict[str, int] = {}
    for c in classified:
        t = c["hint"]
        type_counts[t] = type_counts.get(t, 0) + 1

    return {
        "total_documents": total,
        "sample_count":    len(docs),
        "fields":          {k: list(v) for k, v in all_fields.items()},
        "type_distribution": type_counts,
        "sample": classified[:20],  # 20 premiers documents résumés
    }


def main():
    print(f"🔍  Découverte API Digipharmacie pour user_id={USER_ID}")
    creds = _get_creds()
    if not creds["user"]:
        print("❌  Credentials manquants")
        return

    result = asyncio.run(_discover_async(creds))
    _save_discover(result)

    print(f"✅  Sauvegardé dans state_json.digi_discover")
    print(f"   Total docs : {result.get('total_documents', '?')}")
    print(f"   Champs     : {list(result.get('fields', {}).keys())}")
    print(f"   Types      : {result.get('type_distribution', {})}")


if __name__ == "__main__":
    main()
