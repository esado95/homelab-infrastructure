# -*- coding: utf-8 -*-
"""
Homelab Approve — мини-сервис одобрения регистраций.

Зачем он нужен:
  * Доступ в Jellyfin закрыт ролью `media` (role-gating в SSO-плагине).
  * Новый человек регистрируется в Keycloak, но роли `media` у него нет → внутрь не пускают.
  * Этот сервис показывает админу «ожидающих» и шлёт письмо со ссылками
    «Одобрить» / «Отклонить».
      - Одобрить  = выдать роль `media` (доступ открывается).
      - Отклонить = отключить аккаунт.
  * ВСЕ действия — только после входа админа через Keycloak (login-gated).
    Даже если ссылка из письма утечёт — без твоего входа она бесполезна.
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
# Конфигурация (из переменных окружения, см. .env)
# ---------------------------------------------------------------------------
def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return v

PUBLIC_ISSUER    = env("OIDC_ISSUER", "https://auth.example.com/realms/homelab")  # вход в браузере
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
LINK_TTL         = int(env("LINK_TTL_SECONDS", "604800"))   # срок жизни ссылки, 7 дней
REQUIRE_VERIFIED = env("REQUIRE_EMAIL_VERIFIED", "true").lower() == "true"

# SMTP — необязательно. Без него уведомитель просто молчит (сервис всё равно работает).
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
# OIDC-клиент (вход админа) + админский токен (выдача роли)
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
    """Токен сервисного аккаунта (client_credentials) для Admin REST API."""
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
    """Ожидающие = включены, не сервисные, не админы, (по желанию) с подтверждённым email, без роли media."""
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
# Подпись ссылок (HMAC) — чтобы ссылку нельзя было подделать/подменить пользователя
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
# Маршруты (всё под префиксом /_access-approve)
# ---------------------------------------------------------------------------
bp = Blueprint("bp", __name__, url_prefix=URL_PREFIX)

def current_user():
    return session.get("user")

def require_admin():
    """Возвращает redirect/abort, если не залогинен или не админ; иначе None."""
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
        return render_template_string(PAGE_RESULT, ok=False, msg="Ссылка недействительна или истекла."), 400
    try:
        u = get_user(uid)
        grant_role(uid)
    except Exception as e:
        log.exception("approve failed")
        return render_template_string(PAGE_RESULT, ok=False, msg=f"Ошибка: {e}"), 500
    return render_template_string(PAGE_RESULT, ok=True,
                                  msg=f"Пользователь «{u.get('username')}» одобрен — доступ открыт.")

@bp.route("/deny")
def deny():
    if (r := require_admin()) is not None:
        return r
    uid, exp, sig = request.args.get("u"), request.args.get("exp"), request.args.get("sig")
    if not valid_link("deny", uid, exp, sig):
        return render_template_string(PAGE_RESULT, ok=False, msg="Ссылка недействительна или истекла."), 400
    try:
        u = get_user(uid)
        disable_user(uid)
    except Exception as e:
        log.exception("deny failed")
        return render_template_string(PAGE_RESULT, ok=False, msg=f"Ошибка: {e}"), 500
    return render_template_string(PAGE_RESULT, ok=True,
                                  msg=f"Пользователь «{u.get('username')}» отклонён (аккаунт отключён).")

@bp.route("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Уведомитель: периодически ищет новых ожидающих и шлёт письмо
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
        log.info("SMTP не настроен или нет получателя — письмо пропущено (%s)", subject)
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, SMTP_FROM, to
    msg.set_content("Откройте письмо в HTML-режиме.")
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
    log.info("Уведомитель запущен (интервал %ss, получатель: %s)", POLL_INTERVAL, NOTIFY_TO or "не задан")
    while True:
        try:
            with app.app_context():           # render_template_string требует контекст приложения
                notified = load_state()
                for u in list_pending():
                    if u["id"] in notified:
                        continue
                    html = render_template_string(MAIL_HTML, u=u,
                                approve=make_link("approve", u["id"]),
                                deny=make_link("deny", u["id"]))
                    send_email(f"Homelab — новая заявка: {u.get('username')}", html, NOTIFY_TO)
                    notified.add(u["id"])
                    log.info("Уведомление по пользователю %s", u.get("username"))
                save_state(notified)
        except Exception:
            log.exception("notifier loop error")
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Шаблоны (простые, в стиле Homelab)
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

PAGE_INDEX = """<!doctype html><meta charset="utf-8"><title>Homelab — одобрение</title>
<style>""" + _BASE_CSS + """</style>
<div class="card">
  <h1>🎬 Homelab — <span class="gold">одобрение доступа</span></h1>
  <p class="muted">Вошёл как <b>{{ user.preferred_username }}</b>. Ожидают одобрения: {{ pending|length }}.</p>
  {% if not pending %}<p class="muted">Сейчас никто не ждёт одобрения. 👌</p>{% endif %}
  {% for u in pending %}
    <div class="u">
      <div><b>{{ u.username }}</b><br><span class="muted">{{ u.email or '—' }}</span></div>
      <div>
        <a class="btn ok" href="{{ make_link('approve', u.id) }}">Одобрить</a>
        <a class="btn no" href="{{ make_link('deny', u.id) }}">Отклонить</a>
      </div>
    </div>
  {% endfor %}
</div>"""

PAGE_RESULT = """<!doctype html><meta charset="utf-8"><title>Homelab</title>
<style>""" + _BASE_CSS + """</style>
<div class="card">
  <h1>{{ '✅' if ok else '⚠️' }} Homelab</h1>
  <p>{{ msg }}</p>
  <p><a class="btn ok" href="{{ url_for('bp.index') }}">К списку заявок</a></p>
</div>"""

MAIL_HTML = """<div style="font-family:system-ui,sans-serif;background:#0e0e12;color:#e8e8ee;padding:24px;border-radius:12px;max-width:520px;">
  <h2 style="color:#e8c87a;margin:0 0 8px;">🎬 Homelab — новая заявка</h2>
  <p style="color:#b8b8c4;">Пользователь хочет получить доступ:</p>
  <p style="font-size:1.1rem;"><b>{{ u.username }}</b><br><span style="color:#9a9aa8;">{{ u.email or '—' }}</span></p>
  <p style="margin-top:18px;">
    <a href="{{ approve }}" style="background:#1d9e75;color:#04342c;text-decoration:none;font-weight:700;padding:10px 18px;border-radius:8px;">✅ Одобрить</a>
    &nbsp;&nbsp;
    <a href="{{ deny }}" style="background:#3a2230;color:#f0a0b0;text-decoration:none;font-weight:700;padding:10px 18px;border-radius:8px;">⛔ Отклонить</a>
  </p>
  <p style="color:#6a6a78;font-size:.8rem;margin-top:18px;">Ссылка действует ограниченное время и сработает только после твоего входа.</p>
</div>"""


# ---------------------------------------------------------------------------
# Приложение
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
    # за реверс-прокси Caddy: доверяем X-Forwarded-* (схема/хост/префикс)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    oauth.init_app(app)
    app.register_blueprint(bp)
    if env("ENABLE_NOTIFIER", "true").lower() == "true":
        threading.Thread(target=notifier_loop, args=(app,), daemon=True).start()
    return app

app = create_app()
