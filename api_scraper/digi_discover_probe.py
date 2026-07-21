#!/usr/bin/env python3
"""
digi_discover_probe.py — Découverte de l'API factures Digipharmacie via camoufox
(écran virtuel + proxy résidentiel). Se logue et dumpe la STRUCTURE d'une facture
(champs, référence du PDF) pour concevoir le scraper. Ne télécharge aucun PDF.

Usage (VPS, tunnel SOCKS actif sur 127.0.0.1:1080) :
    VIRTUAL=1 PROXY=socks5://127.0.0.1:1080 \
    DIGI_USER='email' DIGI_PASS='motdepasse' python3 digi_discover_probe.py
"""
import asyncio
import json
import os
import sys

PROXY = os.environ.get("PROXY", "")
USER  = os.environ.get("DIGI_USER", "")
PASS  = os.environ.get("DIGI_PASS", "")
BASE  = "https://app.digipharmacie.fr"
if not USER or not PASS:
    print("!! DIGI_USER / DIGI_PASS requis"); sys.exit(2)


async def main():
    from camoufox.async_api import AsyncCamoufox
    kw = {"headless": "virtual" if os.environ.get("VIRTUAL") == "1" else True}
    if PROXY:
        kw["proxy"] = {"server": PROXY}
        kw["geoip"] = True
    async with AsyncCamoufox(**kw) as browser:
        page = await browser.new_page()
        page.set_default_timeout(90_000)

        print("→ /login/ (franchissement Cloudflare)…")
        await page.goto(f"{BASE}/login/", wait_until="domcontentloaded", timeout=90_000)
        for _ in range(10):
            t = (await page.title() or "").lower()
            if not any(k in t for k in ("just a moment", "un instant", "moment", "checking", "verifying")):
                break
            await page.wait_for_timeout(3_000)
        print(f"  titre après CF : {await page.title()!r}")

        sel = "input[type='email'], input[name='email'], input[name='username'], input[type='text']"
        await page.wait_for_selector(sel, timeout=60_000)
        await page.locator(sel).first.fill(USER)
        await page.locator("input[type='password']").first.fill(PASS)
        # Login direct via l'endpoint (comme discover_digi), repli sur le bouton.
        # On teste plusieurs endpoints de login connus. Un login AJAX ne fait PAS
        # naviguer la page (elle reste sur /login/) → on ne se fie PAS à l'URL :
        # le seul juge de paix est l'appel /api/v1/invoices/ plus bas.
        login_ok = False
        token = ""
        for ep in ("/auth/login/", "/api/v1/auth/login/", "/api/auth/login/", "/login/"):
            res = await page.evaluate("""async ([ep, email, password]) => {
                try {
                    const r = await fetch(ep, {method:'POST',
                        headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},
                        body: JSON.stringify({email, password}), credentials:'include'});
                    return {status: r.status, body: (await r.text()).slice(0,300)};
                } catch (e) { return {status: -1, body: String(e)}; }
            }""", [ep, USER, PASS])
            print(f"  POST {ep} → {res['status']}  {res['body'][:160]!r}")
            if res["status"] in (200, 201, 204):
                login_ok = True
                # Auth par token (dj-rest-auth) : la réponse contient {"key": "..."}.
                try:
                    token = (json.loads(res["body"]) or {}).get("key", "") or \
                            (json.loads(res["body"]) or {}).get("token", "")
                except Exception:
                    token = ""
                if token:
                    print(f"  ✓ token récupéré : {token[:12]}…")
                break
        # Repli formulaire classique (submit) si aucun endpoint JSON n'a répondu 200.
        if not login_ok:
            try:
                await page.locator("button[type='submit'], input[type='submit']").first.click(timeout=3_000)
                await page.wait_for_timeout(8_000)
                print(f"  (repli submit form) URL={page.url}")
            except Exception as e:
                print(f"  (repli submit form impossible : {e})")
        csrf = next((c["value"] for c in await page.context.cookies() if c["name"] == "csrftoken"), "")

        # ── DÉCOUVERTE PAR CAPTURE RÉSEAU ────────────────────────────────────
        # /api/v1/invoices/ renvoie le HTML de la SPA (route fourre-tout) : ce
        # n'est pas la vraie route. On écoute donc TOUTES les requêtes /api que
        # l'app émet en naviguant, pour révéler les vrais endpoints JSON.
        seen = {}  # url -> {method, status, ctype}
        def _on_resp(resp):
            try:
                u = resp.url
                if "/api" in u and "digipharmacie" in u:
                    ct = (resp.headers or {}).get("content-type", "")
                    seen[u.split("?")[0] + ("?…" if "?" in u else "")] = {
                        "method": resp.request.method, "status": resp.status, "ctype": ct[:40]}
            except Exception:
                pass
        page.on("response", _on_resp)

        print("\n→ Chargement de l'app (capture des appels /api)…")
        # Injecter le token pour que la SPA se croie authentifiée (localStorage courant).
        try:
            await page.evaluate("""(tok) => {
                try { localStorage.setItem('token', tok);
                      localStorage.setItem('key', tok);
                      localStorage.setItem('auth_token', tok); } catch(e){}
            }""", token)
        except Exception:
            pass
        for path in ("/", "/factures", "/invoices", "/dashboard", "/documents"):
            try:
                await page.goto(f"{BASE}{path}", wait_until="networkidle", timeout=45_000)
                await page.wait_for_timeout(2_500)
            except Exception as e:
                print(f"  (nav {path} : {e})")

        print("\n===== Endpoints /api observés =====")
        inv_url = ""
        for u, meta in sorted(seen.items()):
            mark = "  ← PISTE FACTURES" if any(k in u.lower() for k in (
                "invoice", "facture", "document", "bill")) else ""
            print(f"  [{meta['status']}] {meta['method']:4s} {u}  ({meta['ctype']}){mark}")
            if mark and "json" in meta["ctype"] and not inv_url:
                inv_url = u.split("?")[0]
        if not seen:
            print("  (aucun appel /api capturé — la SPA n'a peut-être pas chargé)")

        # Si on a repéré une piste factures, on la rejoue avec le token et on dumpe.
        target = inv_url or f"{BASE}/api/v1/invoices/"
        print(f"\n→ Dump de : {target}")
        result = await page.evaluate("""async ([url, csrf, token]) => {
            const h = {'Accept':'application/json','X-CSRFToken':csrf,'X-Requested-With':'XMLHttpRequest'};
            if (token) h['Authorization'] = 'Token ' + token;
            const sep = url.includes('?') ? '&' : '?';
            const r = await fetch(url + sep + 'page_size=5', {credentials:'include', headers:h});
            return {status: r.status, text: await r.text()};
        }""", [target, csrf, token])
        print(f"  HTTP {result['status']}")
        try:
            data = json.loads(result["text"])
        except Exception:
            print("  ⚠️ non-JSON — aperçu :"); print(result["text"][:400]); return
        rows = data.get("results") if isinstance(data, dict) else data
        total = data.get("count", "?") if isinstance(data, dict) else (len(rows) if rows else "?")
        print(f"  {total} éléments. Clés réponse : {list(data.keys()) if isinstance(data, dict) else 'liste'}")
        if rows:
            print("\n--- CHAMPS du 1er élément ---")
            for k, v in rows[0].items():
                print(f"  {k:24s} = {json.dumps(v, ensure_ascii=False)[:90]}")
            print("\n--- champs piste PDF (url/pdf/file/document/path) ---")
            for k, v in rows[0].items():
                if any(t in k.lower() for t in ("url", "pdf", "file", "document", "path", "href")):
                    print(f"  {k} = {v}")
            print("\n===== JSON COMPLET 1er élément =====")
            print(json.dumps(rows[0], ensure_ascii=False, indent=2)[:2500])


if __name__ == "__main__":
    asyncio.run(main())
