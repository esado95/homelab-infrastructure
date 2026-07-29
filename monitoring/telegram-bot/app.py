# -*- coding: utf-8 -*-
"""
Homelab Supervision — pupitre Telegram du serveur.

À quoi il sert :
  * Consulter l'état du homelab depuis le téléphone, sans ouvrir de session SSH :
    serveur, disques, conteneurs, GPU, lectures en cours, demandes, sauvegardes,
    disponibilité des services.
  * Une veille (« watchdog ») compare chaque minute la liste des conteneurs actifs
    et prévient quand l'un d'eux disparaît, puis quand il revient.

Deux principes que je me suis fixés :
  * **Lecture seule.** Le bot ne fait qu'interroger Prometheus et quelques API ;
    il ne redémarre rien, ne supprime rien. Aucune commande d'action n'existe.
  * **Pas de `docker.sock`.** Le socket Docker donnerait un accès équivalent à root
    sur l'hôte. Le bot passe par les métriques de cAdvisor exposées par Prometheus
    (voir le README : moindre privilège).

Un seul chat Telegram est autorisé (`ALLOWED_CHAT`) : tout message venant d'ailleurs
est ignoré sans réponse.
"""

import asyncio
import os
import re
import time

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

# ---------------------------------------------------------------------------
# Configuration (variables d'environnement, voir .env.example)
# ---------------------------------------------------------------------------
TOKEN = os.environ["BOT_TOKEN"]                 # jeton donné par @BotFather
ALLOWED = int(os.environ["ALLOWED_CHAT"])       # seul chat autorisé

# Les services sont joignables par leur nom de conteneur sur le réseau Docker.
PROM = os.environ.get("PROM_URL", "http://prometheus:9090")
JF = os.environ.get("JF_URL", "http://jellyfin:8096")
JF_TOKEN = os.environ.get("JF_TOKEN", "")
GPU = os.environ.get("GPU_URL", "http://nvidia-exporter:9835/metrics")
JS = os.environ.get("JS_URL", "http://jellyseerr:5055")
JS_TOKEN = os.environ.get("JS_TOKEN", "")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backups")   # monté en lecture seule
SERVER_NAME = os.environ.get("SERVER_NAME", "homelab")  # simple étiquette d'affichage

# À améliorer : décalage horaire figé (heure d'été française) au lieu d'un vrai fuseau.
TZ_OFFSET = 2 * 3600


# ---------------------------------------------------------------- interface --

MENU = [
    ("srv", "📊 Serveur"), ("disk", "💾 Disques"),
    ("dock", "🐳 Conteneurs"), ("gpu", "🎮 Carte graphique"),
    ("jf", "🎬 En lecture"), ("req", "🎯 Demandes"),
    ("backup", "💽 Sauvegardes"), ("net", "🌐 Services"),
]


def menu_kb():
    """Clavier du menu : les rubriques deux par deux, puis « Tout vérifier »."""
    rows, row = [], []
    for key, label in MENU:
        row.append(InlineKeyboardButton(label, callback_data=key))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔎 Tout vérifier", callback_data="all")])
    return InlineKeyboardMarkup(rows)


def nav_kb(section):
    """Clavier affiché sous une rubrique : retour au menu ou rafraîchissement."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Menu", callback_data="menu"),
        InlineKeyboardButton("🔄 Actualiser", callback_data=section),
    ]])


MENU_TEXT = "🎛 <b>Homelab Supervision</b>\n\nChoisis une rubrique :"


def now_str():
    t = time.time() + TZ_OFFSET
    return time.strftime("%H:%M:%S", time.gmtime(t))


def stamp(text):
    """Ajoute l'heure de mise à jour — utile quand on rafraîchit plusieurs fois."""
    return f"{text}\n\n<i>màj {now_str()}</i>"


def bar(pct):
    """Petite jauge en caractères : ▰▰▰▱▱▱▱▱▱▱"""
    filled = min(10, max(0, round(pct / 10)))
    return "▰" * filled + "▱" * (10 - filled)


def dot(ok):
    return "🟢" if ok else "🔴"


def dur(sec):
    """Durée lisible : « 3 j 4 h 12 min »."""
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    out = []
    if d:
        out.append(f"{d} j")
    if h or d:
        out.append(f"{h} h")
    out.append(f"{m} min")
    return " ".join(out)


def gib(n):
    return f"{n / 2**30:.1f} Gio"


# ------------------------------------------------------------------ données --

async def prom(query):
    """Une requête PromQL instantanée vers Prometheus."""
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.get(f"{PROM}/api/v1/query", params={"query": query})
        return r.json()["data"]["result"]


def one(res, default=None):
    """Première valeur d'un résultat Prometheus, ou une valeur par défaut."""
    return float(res[0]["value"][1]) if res else default


async def running_names():
    """Conteneurs considérés actifs = vus par cAdvisor il y a moins de 2 minutes.

    C'est ce qui remplace `docker ps` : les métriques suffisent, pas besoin
    de donner le socket Docker au bot.
    """
    res = await prom('container_last_seen{name!=""}')
    now = time.time()
    return {r["metric"]["name"] for r in res if now - float(r["value"][1]) < 120}


async def sec_srv():
    """📊 Serveur : uptime, processeur, charge, mémoire, swap."""
    up = one(await prom("node_time_seconds - node_boot_time_seconds"))
    load1 = one(await prom("node_load1"))
    load5 = one(await prom("node_load5"))
    cores = one(await prom('count(node_cpu_seconds_total{mode="idle"})'), 1)
    cpu = one(await prom('100 - avg(rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100'))
    mem_used = one(await prom("node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes"))
    mem_total = one(await prom("node_memory_MemTotal_bytes"))
    mem_pct = 100 * mem_used / mem_total if mem_total else 0
    swap = one(await prom("100 * (1 - node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes)"), 0)
    charge = 100 * load1 / cores if cores else 0
    return (f"📊 <b>Serveur {SERVER_NAME}</b>\n\n"
            f"⏱ En ligne : {dur(up)}\n"
            f"🧠 Processeur : {cpu:.0f} %  {bar(cpu)}\n"
            f"⚙️ Charge : {load1:.2f} / {load5:.2f}  ({charge:.0f} % de {cores:.0f} cœurs)\n"
            f"💭 Mémoire : {gib(mem_used)} / {gib(mem_total)}  {bar(mem_pct)}\n"
            f"♻️ Swap : {swap:.0f} %")


# Points de montage suivis (adapter à ton serveur)
DISKS = [("/", "Système"), ("/srv/media-cache", "Médiathèque"), ("/srv/backup", "Sauvegardes")]


async def sec_disk():
    """💾 Disques : taux d'occupation et espace libre, avec alerte au-delà de 88 %."""
    lines = ["💾 <b>Disques</b>"]
    for mp, label in DISKS:
        avail = one(await prom(f'node_filesystem_avail_bytes{{mountpoint="{mp}",fstype!="tmpfs"}}'))
        size = one(await prom(f'node_filesystem_size_bytes{{mountpoint="{mp}",fstype!="tmpfs"}}'))
        if not size:
            continue
        used_pct = 100 * (1 - avail / size)
        warn = " ⚠️" if used_pct > 88 else ""
        lines.append(f"\n<b>{label}</b>{warn}\n{bar(used_pct)} {used_pct:.0f} %\n"
                     f"libre {gib(avail)} sur {gib(size)}")
    return "\n".join(lines)


# Conteneurs attendus : leur absence est signalée comme « service essentiel »
CORE = ["caddy", "keycloak", "keycloak-postgres", "oauth2-proxy", "jellyfin",
        "jellyseerr", "radarr", "sonarr", "prowlarr", "qbittorrent", "homarr",
        "dockge", "grafana", "prometheus", "homelab-bot"]


async def sec_dock():
    """🐳 Conteneurs : combien tournent, lesquels manquent."""
    names = await running_names()
    missing = [n for n in CORE if n not in names]
    lines = [f"🐳 <b>Conteneurs : {len(names)} actifs</b>"]
    if missing:
        lines.append("\n🔴 <b>Manquants :</b> " + ", ".join(missing))
    else:
        lines.append("\n🟢 Tous les services essentiels tournent")
    lines.append("\n" + " · ".join(sorted(names)))
    return "\n".join(lines)


async def sec_gpu():
    """🎮 Carte graphique : métriques brutes de l'exportateur NVIDIA (format Prometheus)."""
    async with httpx.AsyncClient(timeout=8) as c:
        text = (await c.get(GPU)).text

    def metric(name):
        r = re.search(r"^" + name + r"(?:\{[^}]*\})? ([0-9.e+-]+)$", text, re.M)
        return float(r.group(1)) if r else None

    util = metric("nvidia_smi_utilization_gpu_ratio")
    used = metric("nvidia_smi_memory_used_bytes")
    total = metric("nvidia_smi_memory_total_bytes")
    temp = metric("nvidia_smi_temperature_gpu")
    power = metric("nvidia_smi_power_draw_watts")
    enc = metric("nvidia_smi_encoder_stats_session_count")
    if util is None and temp is None:
        return "🎮 <b>Carte graphique</b>\n\n⚠️ L'exportateur ne renvoie pas de métriques"
    lines = ["🎮 <b>Carte graphique</b>\n"]
    if util is not None:
        lines.append(f"⚡ Utilisation : {util*100:.0f} %  {bar(util*100)}")
    if used is not None and total:
        lines.append(f"🧠 Mémoire vidéo : {used/2**20:.0f} / {total/2**20:.0f} Mio")
    if temp is not None:
        chaud = " 🔥" if temp >= 80 else ""
        lines.append(f"🌡 Température : {temp:.0f} °C{chaud}")
    if power is not None:
        lines.append(f"🔌 Consommation : {power:.0f} W")
    lines.append(f"🎞 Transcodages actifs : {int(enc) if enc is not None else '—'}")
    return "\n".join(lines)


async def sec_jf():
    """🎬 En lecture : sessions Jellyfin en cours (qui, quoi, sur quel appareil)."""
    if not JF_TOKEN:
        return "🎬 <b>En lecture</b>\n\n⚠️ Clé API Jellyfin absente"
    async with httpx.AsyncClient(timeout=8) as c:
        sessions = (await c.get(f"{JF}/Sessions", params={"api_key": JF_TOKEN})).json()
    lines = []
    for s in sessions:
        item = s.get("NowPlayingItem")
        if not item:
            continue
        name = item.get("Name", "?")
        if item.get("SeriesName"):
            name = f'{item["SeriesName"]} — {name}'
        pos = s.get("PlayState", {}).get("PositionTicks") or 0
        total = item.get("RunTimeTicks") or 0
        pct = f" · {100*pos/total:.0f} %" if total else ""
        icon = "⏸" if s.get("PlayState", {}).get("IsPaused") else "▶️"
        # savoir si le GPU travaille ou si le flux part tel quel
        mode = "transcodage" if s.get("TranscodingInfo") else "lecture directe"
        device = s.get("DeviceName", "?")
        lines.append(f'{icon} <b>{s.get("UserName","?")}</b> — {name}{pct}\n'
                     f'    <i>{device} · {mode}</i>')
    if not lines:
        return "🎬 <b>En lecture</b>\n\nPersonne ne regarde rien 🍿"
    return f"🎬 <b>En lecture ({len(lines)})</b>\n\n" + "\n".join(lines)


# codes d'état renvoyés par l'API Jellyseerr
REQ_STATUS = {1: "⏳ en attente", 2: "✅ approuvé", 3: "🚫 refusé"}
MEDIA_STATUS = {3: "🔄 téléchargement", 4: "📀 partiel", 5: "🍿 disponible"}


async def sec_req():
    """🎯 Demandes : les dernières demandes de contenus faites dans Jellyseerr."""
    if not JS_TOKEN:
        return "🎯 <b>Demandes</b>\n\n⚠️ Clé API Jellyseerr absente"
    h = {"X-Api-Key": JS_TOKEN}
    lines = []
    total_req = 0
    async with httpx.AsyncClient(timeout=10) as c:
        data = (await c.get(f"{JS}/api/v1/request",
                            params={"take": 6, "sort": "added", "sortDirection": "desc"},
                            headers=h)).json()
        total_req = data.get("pageInfo", {}).get("results", 0)
        for q in data.get("results", []):
            media = q.get("media", {})
            kind = "movie" if media.get("mediaType", "movie") == "movie" else "tv"
            title = "?"
            try:
                # l'API des demandes ne renvoie pas le titre : on le récupère par tmdbId
                d = (await c.get(f"{JS}/api/v1/{kind}/{media.get('tmdbId')}", headers=h)).json()
                title = d.get("title") or d.get("name") or "?"
            except Exception:
                pass
            who = (q.get("requestedBy") or {}).get("displayName", "?")
            st = MEDIA_STATUS.get(media.get("status")) or REQ_STATUS.get(q.get("status"), "")
            icon = "🎞" if kind == "movie" else "📺"
            lines.append(f"{icon} <b>{title}</b>\n    <i>{who} · {st}</i>")
    if not lines:
        return "🎯 <b>Demandes</b>\n\nAucune demande"
    return f"🎯 <b>Dernières demandes</b> (total {total_req})\n\n" + "\n".join(lines)


async def sec_backup():
    """💽 Sauvegardes : fraîcheur et volume des archives (dossier monté en lecture seule)."""
    try:
        files = sorted(
            (f for f in os.listdir(BACKUP_DIR) if f.endswith(".tar.gz")),
            reverse=True)
    except FileNotFoundError:
        return "💽 <b>Sauvegardes</b>\n\n⚠️ Dossier de sauvegardes non monté"
    if not files:
        return "💽 <b>Sauvegardes</b>\n\n🔴 Aucune archive trouvée"
    lines = ["💽 <b>Sauvegardes ciblées</b>\n"]
    newest = os.path.join(BACKUP_DIR, files[0])
    age_h = (time.time() - os.path.getmtime(newest)) / 3600
    icon = "🟢" if age_h < 30 else "🔴"   # une sauvegarde quotidienne : au-delà de 30 h, c'est anormal
    lines.append(f"{icon} Dernière : <b>{files[0]}</b>")
    lines.append(f"    {os.path.getsize(newest)/2**20:.0f} Mio · il y a {age_h:.0f} h")
    lines.append(f"\n📦 Archives conservées : {len(files)}")
    total = sum(os.path.getsize(os.path.join(BACKUP_DIR, f)) for f in files)
    lines.append(f"📊 Volume total : {total/2**20:.0f} Mio")
    lines.append("\n<i>Quotidien 3 h 30 · copie vers l'hyperviseur 4 h 15</i>")
    return "\n".join(lines)


# Adresses publiques à tester (remplacer example.com par ton domaine)
SITES = [
    ("https://example.com", "Site / Jellyfin"),
    ("https://auth.example.com/realms/homelab/.well-known/openid-configuration", "Keycloak"),
    ("https://admin.example.com", "Portail"),
    ("https://requests.example.com", "Demandes"),
]


async def check_site(url, label):
    """Une requête HTTP + le temps de réponse. Une redirection compte comme un succès."""
    try:
        t0 = time.time()
        # verify=False : le bot sort et revient par le proxy depuis le réseau interne,
        # le certificat n'y correspond pas toujours. À améliorer (test depuis l'extérieur).
        async with httpx.AsyncClient(timeout=8, verify=False, follow_redirects=False) as c:
            r = await c.get(url)
        ms = (time.time() - t0) * 1000
        ok = r.status_code < 400 or r.status_code in (301, 302, 307, 308)
        return f"{dot(ok)} {label} — {r.status_code} · {ms:.0f} ms"
    except Exception as e:
        return f"🔴 {label} — {type(e).__name__}"


async def sec_net():
    """🌐 Services : les sites publics répondent-ils, et en combien de temps."""
    results = await asyncio.gather(*(check_site(u, l) for u, l in SITES))
    return "🌐 <b>Disponibilité des services</b>\n\n" + "\n".join(results)


async def sec_all():
    """🔎 Tout vérifier : les rubriques principales en un seul message."""
    parts = await asyncio.gather(sec_srv(), sec_disk(), sec_dock(), sec_net(),
                                 return_exceptions=True)
    out = []
    for p in parts:
        # une rubrique en panne ne doit pas faire échouer tout le message
        out.append(p if isinstance(p, str) else f"⚠️ {type(p).__name__}")
    return "\n\n———\n\n".join(out)


SECTIONS = {"srv": sec_srv, "disk": sec_disk, "dock": sec_dock, "gpu": sec_gpu,
            "jf": sec_jf, "req": sec_req, "backup": sec_backup, "net": sec_net,
            "all": sec_all}


# ---------------------------------------------------------------- Telegram --

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/start et /menu — affichent le pupitre."""
    if update.effective_chat.id != ALLOWED:
        return          # silence total pour les inconnus
    await update.message.reply_text(MENU_TEXT, reply_markup=menu_kb(),
                                    parse_mode=ParseMode.HTML)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Un bouton a été pressé : on remplace le message par la rubrique demandée."""
    q = update.callback_query
    if update.effective_chat.id != ALLOWED:
        await q.answer()
        return
    data = q.data
    await q.answer("Actualisation…" if data != "menu" else None)
    try:
        if data == "menu":
            await q.edit_message_text(MENU_TEXT, reply_markup=menu_kb(),
                                      parse_mode=ParseMode.HTML)
            return
        fn = SECTIONS.get(data)
        if not fn:
            return
        try:
            text = stamp(await fn())
        except httpx.HTTPError as e:
            text = f"⚠️ Service injoignable : {type(e).__name__}"
        except Exception as e:
            text = f"⚠️ Erreur : {type(e).__name__}"
        await q.edit_message_text(text, reply_markup=nav_kb(data),
                                  parse_mode=ParseMode.HTML)
    except BadRequest as e:
        # « message is not modified » arrive si rien n'a changé depuis le dernier relevé
        if "not modified" not in str(e).lower():
            raise


# Un conteneur doit avoir été vu ce nombre de relevés d'affilée (~minutes)
# avant d'être surveillé : cela écarte les conteneurs jetables (docker run --rm)
# qui vivent quelques secondes et déclencheraient de fausses alertes.
STABLE_AFTER = 10


async def watchdog(app: Application):
    """Veille : alerte si un conteneur DURABLE disparaît (2 relevés) puis à son retour."""
    stable = {}      # nom -> relevés consécutifs où le conteneur est présent
    watched = set()  # conteneurs jugés durables, donc surveillés
    gone = {}        # nom -> relevés consécutifs d'absence
    while True:
        try:
            now = await running_names()

            # les services essentiels sont surveillés d'emblée,
            # les autres seulement après STABLE_AFTER relevés consécutifs
            for n in now:
                stable[n] = stable.get(n, 0) + 1
                if n in CORE or stable[n] >= STABLE_AFTER:
                    watched.add(n)

            for n in sorted(watched - now):
                gone[n] = gone.get(n, 0) + 1
                if gone[n] == 2:
                    critique = " ⚠️ <b>service essentiel</b>" if n in CORE else ""
                    await app.bot.send_message(
                        ALLOWED,
                        f"🔴 Conteneur <b>{n}</b> hors ligne (~2 min){critique}",
                        parse_mode=ParseMode.HTML)

            for n in sorted(now):
                if gone.get(n, 0) >= 2:
                    await app.bot.send_message(
                        ALLOWED, f"🟢 Conteneur <b>{n}</b> de nouveau actif",
                        parse_mode=ParseMode.HTML)
                gone.pop(n, None)

            # oublier les conteneurs jetables : disparus avant d'être surveillés
            for n in [k for k in stable if k not in now and k not in watched]:
                stable.pop(n, None)
        except Exception:
            pass
        await asyncio.sleep(60)


async def post_init(app: Application):
    await app.bot.set_my_commands([
        ("menu", "Ouvrir le pupitre"),
        ("etat", "État rapide du serveur"),
    ])
    app.create_task(watchdog(app))


async def etat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/etat — raccourci vers la rubrique Serveur, sans passer par le menu."""
    if update.effective_chat.id != ALLOWED:
        return
    try:
        text = stamp(await sec_srv())
    except Exception as e:
        text = f"⚠️ Erreur : {type(e).__name__}"
    await update.message.reply_text(text, reply_markup=nav_kb("srv"),
                                    parse_mode=ParseMode.HTML)


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "menu"], start))
    app.add_handler(CommandHandler("etat", etat))
    app.add_handler(CallbackQueryHandler(on_button))
    # Long polling : le bot appelle Telegram, aucun port n'est ouvert sur le serveur.
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
