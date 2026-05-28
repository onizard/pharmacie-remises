"""
discover_digi.py — Capture la structure brute de l'API Digipharmacie.
Stocke dans Supabase : state_json.digi_discover = {sample, fields, doc_types}
Ne télécharge aucun PDF.
"""

import asyncio
import json
import os
import urllib.request

SUPA_URL    = "https://fmterazwesiwpwjpkyqi.supabase.co"
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

    async with AsyncCamoufox(headless=True, geoip=False) as browser:
        ctx  = await browser.new_context(**({"proxy": proxy_cfg} if proxy_cfg else {}))
        page = await ctx.new_page()
        page.on("pageerror", lambda e: None)
        page.set_default_timeout(120_000)  # override default 30s partout

        print("  Login…")
        await page.goto(f"{BASE_URL}/login/", timeout=60_000)

        title = await page.title()
        print(f"  Titre après goto: {title!r}")

        # Attendre résolution challenge Cloudflare
        _cf_kw = ("just a moment", "checking", "verifying", "cloudflare")
        if any(k in title.lower() for k in _cf_kw):
            print("  Cloudflare challenge — attente résolution…")
            await page.wait_for_function(
                "() => !['just a moment','checking','verifying','cloudflare']"
                ".some(k => document.title.toLowerCase().includes(k))",
                polling=2000,  # timeout géré par set_default_timeout (120s)
            )
            title = await page.title()
            print(f"  Challenge résolu. Titre: {title!r}")
            # Attendre que la page soit complètement chargée (cookies Django inclus)
            await page.wait_for_load_state("networkidle")

        # Récupérer le CSRF token — via context (bypass HttpOnly) puis document.cookie
        ctx_cookies = await ctx.cookies()
        csrf = next((c["value"] for c in ctx_cookies if c["name"] == "csrftoken"), "")
        if not csrf:
            csrf = await page.evaluate(
                "() => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || ''"
            )
        all_cookie_names = [c["name"] for c in ctx_cookies]
        print(f"  Creds: user={creds['user'][:4]}*** len={len(creds['user'])}  pass_len={len(creds['pass'])}  csrf_len={len(csrf)}  all_cookies={all_cookie_names}")

        res = await page.evaluate("""async ({email, password, csrf}) => {
            const endpoints = [
                '/api/v1/auth/login/',
                '/api/auth/login/',
                '/api/v1/token/',
                '/api/token/',
            ];
            const results = [];
            for (const ep of endpoints) {
                try {
                    const r = await fetch(ep, {
                        method: 'POST',
                        headers: {'Content-Type':'application/json','X-CSRFToken':csrf},
                        body: JSON.stringify({email, password}),
                        credentials: 'include',
                    });
                    results.push({ep, status: r.status});
                    if (r.status === 200 || r.status === 204) return {status: r.status, ep, results};
                    if (r.status === 400 || r.status === 401) return {status: r.status, bad_creds: true, ep, results};
                } catch(e) { results.push({ep, error: String(e)}); }
            }
            return {status: 0, results};
        }""", {"email": creds["user"], "password": creds["pass"], "csrf": csrf})

        print(f"  API login: {res}")
        if res.get("bad_creds"):
            raise RuntimeError("Identifiants incorrects")

        if res.get("status", 0) not in (200, 204):
            # Fallback : POST form-encodé via fetch (CSRF depuis context cookies)
            ctx_cookies2 = await ctx.cookies()
            csrf2 = next((c["value"] for c in ctx_cookies2 if c["name"] == "csrftoken"), csrf)
            form_res = await page.evaluate("""async ({email, password, csrf}) => {
                const body = new URLSearchParams({
                    email, password, csrfmiddlewaretoken: csrf
                }).toString();
                const r = await fetch('/login/', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-CSRFToken': csrf,
                        'Referer': window.location.href,
                    },
                    body,
                    credentials: 'include',
                    redirect: 'follow',
                });
                return {status: r.status, url: r.url};
            }""", {"email": creds["user"], "password": creds["pass"], "csrf": csrf2})
            print(f"  Form POST result: {form_res}")

            # Naviguer vers la page retournée si succès
            if form_res.get("status") == 200 and "/login" not in form_res.get("url", "/login"):
                await page.goto(form_res["url"], wait_until="networkidle", timeout=30_000)
            elif form_res.get("status") == 200:
                await page.reload(wait_until="networkidle", timeout=30_000)

            cur_url = page.url
            print(f"  URL après form POST: {cur_url}")
            if "/login" in cur_url:
                # Dernier essai : remplir le formulaire HTML avec csrfmiddlewaretoken injecté
                sel = "input[type='email'], input[name='email'], input[name='username'], input[type='text']"
                try:
                    await page.wait_for_selector(sel, timeout=10_000)
                    # Injecter le CSRF token dans le formulaire si pas déjà présent
                    if csrf2:
                        await page.evaluate("""(csrf) => {
                            let inp = document.querySelector('input[name="csrfmiddlewaretoken"]');
                            if (!inp) {
                                inp = document.createElement('input');
                                inp.type = 'hidden'; inp.name = 'csrfmiddlewaretoken';
                                const form = document.querySelector('form');
                                if (form) form.appendChild(inp);
                            }
                            inp.value = csrf;
                        }""", csrf2)
                    await page.locator(sel).first.fill(creds["user"])
                    await page.locator("input[type='password']").first.fill(creds["pass"])
                    await page.locator("input[type='password']").first.press("Enter")
                    await page.wait_for_function(
                        "() => !window.location.pathname.includes('/login')",
                        timeout=30_000,
                    )
                except Exception:
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
