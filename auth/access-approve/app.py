# -*- coding: utf-8 -*-
"""
Homelab Approve — mini-service d'approbation des inscriptions.

À quoi il sert :
  * L'accès à Jellyfin est fermé par le rôle `media` (role-gating dans le plugin SSO).
  * Une nouvelle personne s'inscrit dans Keycloak, mais sans le rôle `media` → elle n'entre pas.
  * Ce service montre à l'admin les « en attente » et envoie un e-mail avec les liens
    « Approuver » / « Refuser ».
      - Approuver = attribuer le rôle `media` (l'accès s'ouvre).
      - Refuser   = désactiver le compte.
  * TOUTES les actions ne sont possibles qu'après la connexion de l'admin via Keycloak (login-gated).
    Même si un lien de l'e-mail fuit — sans ta connexion il est inutile.
"""

import os, hmac, hashlib, time, json, threading, smtplib, ssl, logging
from email.message import EmailMessage
from urllib.parse import urlencode

import requests
from flask import (Flask, Blueprint, session, redirect, request,
                   url_for, render_template_string, abort)
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix

log = logging.getLogger("access-approve")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# Configuration (depuis les variables d'environnement, voir .env)
# ---------------------------------------------------------------------------
def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"Variable d'environnement non définie : {name}")
    return v

PUBLIC_ISSUER    = env("OIDC_ISSUER", "https://auth.example.com/realms/homelab")  # connexion dans le navigateur
INTERNAL_KC      = env("KC_INTERNAL", "http://keycloak:8080")                       # server-to-server
REALM            = env("KC_REALM", "homelab")
CLIENT_ID        = env("OIDC_CLIENT_ID", "access-approve")
CLIENT_SECRET    = env("OIDC_CLIENT_SECRET", required=True)
BASE_URL         = env("BASE_URL", "https://example.com/_access-approve").rstrip("/")
URL_PREFIX       = env("URL_PREFIX", "/_access-approve")
ROLE_NAME        = env("ROLE_NAME", "media")
ADMIN_USERS      = [u.strip() for u in env("ADMIN_USERS", "admin").split(",") if u.strip()]
SIGN_KEY         = env("SIGN_KEY", required=True).encode()
FLASK_SECRET     = env("FLASK_SECRET", required=True)
LINK_TTL         = int(env("LINK_TTL_SECONDS", "604800"))   # durée de vie du lien, 7 jours
REQUIRE_VERIFIED = env("REQUIRE_EMAIL_VERIFIED", "true").lower() == "true"

# SMTP — facultatif. Sans lui, le notificateur reste silencieux (le service fonctionne quand même).
SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = int(env("SMTP_PORT", "587"))
SMTP_USER = env("SMTP_USER")
SMTP_PASS = env("SMTP_PASS")
SMTP_FROM = env("SMTP_FROM", SMTP_USER or "noreply@example.com")
SMTP_MODE = env("SMTP_MODE", "starttls")      # starttls | ssl | plain
NOTIFY_TO = env("NOTIFY_TO", "")
POLL_INTERVAL = int(env("POLL_INTERVAL_SECONDS", "120"))
STATE_FILE = env("STATE_FILE", "/data/notified.json")


# ---------------------------------------------------------------------------
# Client OIDC (connexion admin) + token admin (attribution du rôle)
# ---------------------------------------------------------------------------
oauth = OAuth()
oauth.register(
    name="kc",
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    server_metadata_url=f"{PUBLIC_ISSUER}/.well-known/openid-configuration",
    client_kwargs={"scope": "openid profile email"},
)

def admin_token():
    """Token du compte de service (client_credentials) pour l'Admin REST API."""
    r = requests.post(
        f"{INTERNAL_KC}/realms/{REALM}/protocol/openid-connect/token",
        data={"grant_type": "client_credentials",
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]

def kc_get(path, **params):
    r = requests.get(f"{INTERNAL_KC}/admin/realms/{REALM}{path}",
                     headers={"Authorization": f"Bearer {admin_token()}"},
                     params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def get_user(uid):
    return kc_get(f"/users/{uid}")

def role_repr():
    return kc_get(f"/roles/{ROLE_NAME}")

def user_has_role(uid):
    roles = kc_get(f"/users/{uid}/role-mappings/realm")
    return any(x["name"] == ROLE_NAME for x in roles)

def grant_role(uid):
    role = role_repr()
    r = requests.post(
        f"{INTERNAL_KC}/admin/realms/{REALM}/users/{uid}/role-mappings/realm",
        headers={"Authorization": f"Bearer {admin_token()}", "Content-Type": "application/json"},
        json=[{"id": role["id"], "name": role["name"]}], timeout=15)
    r.raise_for_status()

def disable_user(uid):
    r = requests.put(
        f"{INTERNAL_KC}/admin/realms/{REALM}/users/{uid}",
        headers={"Authorization": f"Bearer {admin_token()}", "Content-Type": "application/json"},
        json={"enabled": False}, timeout=15)
    r.raise_for_status()

def list_pending():
    """En attente = activés, non service-account, non admins, (optionnellement) e-mail vérifié, sans le rôle media."""
    pending = []
    for u in kc_get("/users", max=1000, briefRepresentation="false"):
        if not u.get("enabled"):                       continue
        un = u.get("username", "")
        if un.startswith("service-account-"):          continue
        if un in ADMIN_USERS:                           continue
        if REQUIRE_VERIFIED and not u.get("emailVerified"): continue
        if user_has_role(u["id"]):                      continue
        pending.append(u)
    return pending


# ---------------------------------------------------------------------------
# Signature des liens (HMAC) — pour qu'un lien ne puisse pas être falsifié / l'utilisateur substitué
# ---------------------------------------------------------------------------
def sign(action, uid, exp):
    return hmac.new(SIGN_KEY, f"{action}:{uid}:{exp}".encode(), hashlib.sha256).hexdigest()

def make_link(action, uid):
    exp = int(time.time()) + LINK_TTL
    return f"{BASE_URL}/{action}?" + urlencode({"u": uid, "exp": exp, "sig": sign(action, uid, exp)})

def valid_link(action, uid, exp, sig):
    if not (uid and exp and sig):
        return False
    try:
        exp = int(exp)
    except ValueError:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(sig, sign(action, uid, exp))


# ---------------------------------------------------------------------------
# Routes (tout sous le préfixe /_access-approve)
# ---------------------------------------------------------------------------
bp = Blueprint("bp", __name__, url_prefix=URL_PREFIX)

def current_user():
    return session.get("user")

def require_admin():
    """Renvoie redirect/abort si non connecté ou non admin ; sinon None."""
    u = current_user()
    if not u:
        session["next"] = request.url
        return redirect(url_for("bp.login"))
    if u.get("preferred_username") not in ADMIN_USERS:
        abort(403)
    return None

@bp.route("/login")
def login():
    return oauth.kc.authorize_redirect(BASE_URL + "/oauth2/callback")

@bp.route("/oauth2/callback")
def callback():
    token = oauth.kc.authorize_access_token()
    info = token.get("userinfo") or oauth.kc.userinfo(token=token)
    session["user"] = {"preferred_username": info.get("preferred_username"),
                       "name": info.get("name"), "email": info.get("email")}
    return redirect(session.pop("next", None) or url_for("bp.index"))

@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("bp.index"))

@bp.route("/")
def index():
    if (r := require_admin()) is not None:
        return r
    return render_template_string(PAGE_INDEX, pending=list_pending(),
                                  make_link=make_link, user=current_user())

@bp.route("/approve")
def approve():
    if (r := require_admin()) is not None:
        return r
    uid, exp, sig = request.args.get("u"), request.args.get("exp"), request.args.get("sig")
    if not valid_link("approve", uid, exp, sig):
        return render_template_string(PAGE_RESULT, ok=False, msg="Lien invalide ou expiré."), 400
    try:
        u = get_user(uid)
        grant_role(uid)
    except Exception as e:
        log.exception("approve failed")
        return render_template_string(PAGE_RESULT, ok=False, msg=f"Erreur : {e}"), 500
    return render_template_string(PAGE_RESULT, ok=True,
                                  msg=f"Utilisateur « {u.get('username')} » approuvé — accès ouvert.")

@bp.route("/deny")
def deny():
    if (r := require_admin()) is not None:
        return r
    uid, exp, sig = request.args.get("u"), request.args.get("exp"), request.args.get("sig")
    if not valid_link("deny", uid, exp, sig):
        return render_template_string(PAGE_RESULT, ok=False, msg="Lien invalide ou expiré."), 400
    try:
        u = get_user(uid)
        disable_user(uid)
    except Exception as e:
        log.exception("deny failed")
        return render_template_string(PAGE_RESULT, ok=False, msg=f"Erreur : {e}"), 500
    return render_template_string(PAGE_RESULT, ok=True,
                                  msg=f"Utilisateur « {u.get('username')} » refusé (compte désactivé).")

@bp.route("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Notificateur : recherche périodiquement les nouveaux en attente et envoie un e-mail
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_state(s):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(sorted(s), f)
    except Exception:
        log.exception("save state failed")

def send_email(subject, html, to):
    if not (SMTP_HOST and to):
        log.info("SMTP non configuré ou pas de destinataire — e-mail ignoré (%s)", subject)
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, SMTP_FROM, to
    msg.set_content("Ouvrez cet e-mail en mode HTML.")
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    if SMTP_MODE == "ssl":
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as s:
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            if SMTP_MODE == "starttls":
                s.starttls(context=ctx)
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

def notifier_loop(app):
    log.info("Notificateur démarré (intervalle %ss, destinataire : %s)", POLL_INTERVAL, NOTIFY_TO or "non défini")
    while True:
        try:
            with app.app_context():           # render_template_string nécessite le contexte d'application
                notified = load_state()
                for u in list_pending():
                    if u["id"] in notified:
                        continue
                    html = render_template_string(MAIL_HTML, u=u,
                                approve=make_link("approve", u["id"]),
                                deny=make_link("deny", u["id"]))
                    send_email(f"Homelab — nouvelle demande : {u.get('username')}", html, NOTIFY_TO)
                    notified.add(u["id"])
                    log.info("Notification pour l'utilisateur %s", u.get("username"))
                save_state(notified)
        except Exception:
            log.exception("notifier loop error")
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Templates (simples, dans le style Homelab)
# ---------------------------------------------------------------------------
_BASE_CSS = """
  body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0e0e12;color:#e8e8ee;margin:0;padding:2rem;}
  .card{max-width:640px;margin:0 auto;background:#17171f;border:1px solid #2a2a35;border-radius:14px;padding:1.5rem 1.75rem;}
  h1{font-size:1.4rem;font-weight:600;margin:0 0 1rem;} .muted{color:#9a9aa8;}
  .u{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 0;border-top:1px solid #23232d;}
  a.btn{display:inline-block;text-decoration:none;font-weight:600;border-radius:8px;padding:8px 14px;font-size:.9rem;}
  .ok{background:#1d9e75;color:#04342c;} .no{background:#3a2230;color:#f0a0b0;border:1px solid #5a2030;}
  .gold{color:#e8c87a;}
"""

PAGE_INDEX = """<!doctype html><meta charset="utf-8"><title>Homelab — approbation</title>
<style>""" + _BASE_CSS + """</style>
<div class="card">
  <h1>🎬 Homelab — <span class="gold">approbation des accès</span></h1>
  <p class="muted">Connecté en tant que <b>{{ user.preferred_username }}</b>. En attente d'approbation : {{ pending|length }}.</p>
  {% if not pending %}<p class="muted">Personne n'attend d'approbation pour le moment. 👌</p>{% endif %}
  {% for u in pending %}
    <div class="u">
      <div><b>{{ u.username }}</b><br><span class="muted">{{ u.email or '—' }}</span></div>
      <div>
        <a class="btn ok" href="{{ make_link('approve', u.id) }}">Approuver</a>
        <a class="btn no" href="{{ make_link('deny', u.id) }}">Refuser</a>
      </div>
    </div>
  {% endfor %}
</div>"""

PAGE_RESULT = """<!doctype html><meta charset="utf-8"><title>Homelab</title>
<style>""" + _BASE_CSS + """</style>
<div class="card">
  <h1>{{ '✅' if ok else '⚠️' }} Homelab</h1>
  <p>{{ msg }}</p>
  <p><a class="btn ok" href="{{ url_for('bp.index') }}">Vers la liste des demandes</a></p>
</div>"""

MAIL_HTML = """<div style="font-family:system-ui,sans-serif;background:#0e0e12;color:#e8e8ee;padding:24px;border-radius:12px;max-width:520px;">
  <h2 style="color:#e8c87a;margin:0 0 8px;">🎬 Homelab — nouvelle demande</h2>
  <p style="color:#b8b8c4;">Un utilisateur souhaite obtenir l'accès :</p>
  <p style="font-size:1.1rem;"><b>{{ u.username }}</b><br><span style="color:#9a9aa8;">{{ u.email or '—' }}</span></p>
  <p style="margin-top:18px;">
    <a href="{{ approve }}" style="background:#1d9e75;color:#04342c;text-decoration:none;font-weight:700;padding:10px 18px;border-radius:8px;">✅ Approuver</a>
    &nbsp;&nbsp;
    <a href="{{ deny }}" style="background:#3a2230;color:#f0a0b0;text-decoration:none;font-weight:700;padding:10px 18px;border-radius:8px;">⛔ Refuser</a>
  </p>
  <p style="color:#6a6a78;font-size:.8rem;margin-top:18px;">Le lien est valable un temps limité et ne fonctionnera qu'après ta connexion.</p>
</div>"""


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
def create_app():
    app = Flask(__name__)
    app.secret_key = FLASK_SECRET
    app.config.update(
        SESSION_COOKIE_NAME="access_approve_session",
        SESSION_COOKIE_PATH=URL_PREFIX,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PREFERRED_URL_SCHEME="https",
    )
    # derrière le reverse proxy Caddy : on fait confiance à X-Forwarded-* (schéma/hôte/préfixe)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    oauth.init_app(app)
    app.register_blueprint(bp)
    if env("ENABLE_NOTIFIER", "true").lower() == "true":
        threading.Thread(target=notifier_loop, args=(app,), daemon=True).start()
    return app

app = create_app()
