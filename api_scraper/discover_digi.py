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

        print("  Login…")
        await page.goto(f"{BASE_URL}/login/", timeout=60_000)

        # Attendre challenge Cloudflare
        title = await page.title()
        if "just a moment" in title.lower():
            print("  Cloudflare challenge — attente…")
            await page.wait_for_function(
                "() => !document.title.toLowerCase().includes('just a moment')",
                timeout=90_000, polling=2000,
            )

        # Login via API JS (cookie CF déjà présent)
        csrf = await page.evaluate(
            "() => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || ''"
        )
        res = await page.evaluate("""async ({email, password, csrf}) => {
            const r = await fetch('/api/v1/auth/login/', {
                method: 'POST',
                headers: {'Content-Type':'application/json','X-CSRFToken':csrf},
                body: JSON.stringify({email, password}),
                credentials: 'include',
            });
            return {status: r.status};
        }""", {"email": creds["user"], "password": creds["pass"], "csrf": csrf})

        if res["status"] not in (200, 204):
            # Fallback form login
            sel = "input[type='email'],input[name='email'],input[type='text']"
            await page.wait_for_selector(sel, timeout=30_000)
            await page.locator(sel).first.fill(creds["user"])
            await page.locator("input[type='password']").first.fill(creds["pass"])
            await page.locator("input[type='password']").first.press("Enter")
            await page.wait_for_function(
                "() => !window.location.pathname.includes('/login')", timeout=30_000
            )

        print("  Connecté — navigation /factures/…")

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
