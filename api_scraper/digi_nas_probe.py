#!/usr/bin/env python3
"""
digi_nas_probe.py — Sonde de connexion Digipharmacie depuis le NAS.

But : vérifier qu'une IP RÉSIDENTIELLE (le NAS Synology) passe Cloudflare et
peut interroger l'API Django de Digipharmacie SANS navigateur ni extension.
Ne télécharge AUCUN PDF, n'écrit rien en base — juste un diagnostic.

Identifiants lus dans l'environnement (jamais en dur, jamais dans le dépôt) :
    DIGI_USER=ton-email
    DIGI_PASS=ton-mot-de-passe

Usage sur le NAS :
    DIGI_USER='...' DIGI_PASS='...' python3 digi_nas_probe.py

Deux modes automatiques :
  • curl_cffi si installé (empreinte TLS Chrome — le plus fiable) ;
  • sinon repli urllib (stdlib, aucune dépendance) — souvent suffisant depuis
    une IP résidentielle où Cloudflare ne challenge pas les simples requêtes.
"""
import json
import os
import sys

DIGI_URL   = "https://app.digipharmacie.fr"
LOGIN_APIS = ["/api/v1/auth/login/", "/api/auth/login/", "/api/v1/token/", "/api/token/"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

USER = os.environ.get("DIGI_USER", "")
PASS = os.environ.get("DIGI_PASS", "")
if not USER or not PASS:
    print("!! Renseigne DIGI_USER et DIGI_PASS dans l'environnement.")
    sys.exit(2)


def _ok(msg):   print(f"  \033[32m✓\033[0m {msg}")
def _bad(msg):  print(f"  \033[31m✗\033[0m {msg}")
def _info(msg): print(f"    {msg}")


def _sample(data):
    """Affiche quelques factures du JSON renvoyé (structure DRF paginée)."""
    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list):
        _info(f"réponse inattendue : {str(data)[:200]}")
        return 0
    for r in results[:3]:
        f = r.get("supplier") or r.get("fournisseur") or r.get("issuer") or "?"
        d = r.get("billing_date") or r.get("date") or "?"
        m = r.get("amount_ht") or r.get("total_ht") or r.get("montant_ht") or r.get("amount") or "?"
        _info(f"• {str(f)[:24]:24s} {d}  {m}")
    return len(results)


# ── Mode 1 : curl_cffi (empreinte Chrome) ──────────────────────────────────────
def probe_curl():
    from curl_cffi import requests as cffi
    s = cffi.Session(impersonate="chrome124")
    hp = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "fr-FR,fr;q=0.9"}
    r = s.get(f"{DIGI_URL}/login/", headers=hp, timeout=25, allow_redirects=True)
    if r.status_code in (403, 503) or len(r.text) < 200:
        _bad(f"Cloudflare bloque (HTTP {r.status_code}, {len(r.text)} o)"); return False
    csrf = s.cookies.get("csrftoken", "")
    if not csrf:
        _bad("Pas de cookie csrftoken (challenge JS ?)"); return False
    _ok(f"GET /login/ OK — Cloudflare franchi, csrftoken obtenu")
    ah = {"Accept": "application/json", "Content-Type": "application/json",
          "X-CSRFToken": csrf, "Referer": f"{DIGI_URL}/login/", "Origin": DIGI_URL}
    logged = False
    for ep in LOGIN_APIS:
        try:
            r = s.post(f"{DIGI_URL}{ep}", json={"email": USER, "password": PASS},
                       headers=ah, timeout=15, allow_redirects=False)
        except Exception:
            continue
        if r.status_code == 200:
            _ok(f"login via {ep}"); logged = True; break
        if r.status_code in (400, 401):
            _bad("identifiants refusés (400/401)"); return False
    if not logged:
        r = s.post(f"{DIGI_URL}/login/",
                   data={"email": USER, "password": PASS, "csrfmiddlewaretoken": csrf},
                   headers={"Referer": f"{DIGI_URL}/login/", "Origin": DIGI_URL},
                   timeout=15, allow_redirects=True)
        if "/login" in getattr(r, "url", ""):
            _bad("login form échoué (identifiants ?)"); return False
        _ok("login via form POST")
    csrf = s.cookies.get("csrftoken", csrf)
    r = s.get(f"{DIGI_URL}/api/v1/invoices/?ordering=-billing_date&page_size=3&page=1",
              headers={"Accept": "application/json", "X-CSRFToken": csrf,
                       "X-Requested-With": "XMLHttpRequest"}, timeout=20)
    if r.status_code != 200:
        _bad(f"/api/v1/invoices/ → HTTP {r.status_code}"); return False
    data = r.json()
    total = data.get("count") if isinstance(data, dict) else len(data)
    _ok(f"API factures OK — {total} facture(s) au total")
    _sample(data)
    return True


# ── Mode 2 : urllib (stdlib, aucune dépendance) ────────────────────────────────
def probe_urllib():
    import urllib.request, urllib.parse, http.cookiejar
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    def _get(url, headers=None):
        req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
        return opener.open(req, timeout=25)
    def _post(url, body, headers, form=False):
        data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"User-Agent": UA, **headers})
        return opener.open(req, timeout=20)
    try:
        resp = _get(f"{DIGI_URL}/login/")
        html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        _bad(f"GET /login/ : {e}"); return False
    if len(html) < 200:
        _bad("réponse trop courte — probable challenge Cloudflare"); return False
    csrf = next((c.value for c in cj if c.name == "csrftoken"), "")
    if not csrf:
        _bad("Pas de cookie csrftoken (Cloudflare/JS ?)"); return False
    _ok("GET /login/ OK — csrftoken obtenu")
    ah = {"Accept": "application/json", "Content-Type": "application/json",
          "X-CSRFToken": csrf, "Referer": f"{DIGI_URL}/login/", "Origin": DIGI_URL}
    logged = False
    for ep in LOGIN_APIS:
        try:
            r = _post(f"{DIGI_URL}{ep}", {"email": USER, "password": PASS}, ah)
            if r.status == 200:
                _ok(f"login via {ep}"); logged = True; break
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):
                _bad("identifiants refusés (400/401)"); return False
        except Exception:
            continue
    if not logged:
        try:
            r = _post(f"{DIGI_URL}/login/",
                      {"email": USER, "password": PASS, "csrfmiddlewaretoken": csrf},
                      {"Referer": f"{DIGI_URL}/login/", "Origin": DIGI_URL}, form=True)
            if "/login" in r.geturl():
                _bad("login form échoué (identifiants ?)"); return False
            _ok("login via form POST")
        except Exception as e:
            _bad(f"login form : {e}"); return False
    csrf = next((c.value for c in cj if c.name == "csrftoken"), csrf)
    try:
        r = _get(f"{DIGI_URL}/api/v1/invoices/?ordering=-billing_date&page_size=3&page=1",
                 {"Accept": "application/json", "X-CSRFToken": csrf,
                  "X-Requested-With": "XMLHttpRequest"})
        data = json.loads(r.read())
    except Exception as e:
        _bad(f"/api/v1/invoices/ : {e}"); return False
    total = data.get("count") if isinstance(data, dict) else len(data)
    _ok(f"API factures OK — {total} facture(s) au total")
    _sample(data)
    return True


def main():
    print(f"→ Sonde Digipharmacie depuis {os.uname().nodename} (utilisateur {USER[:3]}***)")
    try:
        import curl_cffi  # noqa
        print("  Mode : curl_cffi (empreinte Chrome)")
        ok = probe_curl()
    except ImportError:
        print("  Mode : urllib (stdlib — curl_cffi non installé)")
        ok = probe_urllib()
    print()
    if ok:
        print("\033[32m🎉  SUCCÈS — l'IP du NAS passe Cloudflare et l'API répond.\033[0m")
        print("    → on peut automatiser la récupération complète des factures.")
    else:
        print("\033[31m❌  ÉCHEC — voir la ligne ✗ ci-dessus.\033[0m")
        print("    Si c'est Cloudflare : installe curl_cffi (pip3 install curl_cffi) et relance.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
