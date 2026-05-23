"""
Konsolidierter Knuddels-Bot — ein Prozess, eine Session, vier Tasks in einer
Event-Loop. Output ist stdout (eine Zeile pro Event, Dashboard erreichbar unter
http://localhost:<port>/dashboard.html).

Tasks (alle Intervalle live im Dashboard editierbar, persistiert in bot_config.json):
  - SPAM: zufällige Channel-Nachricht aus MESSAGES (adaptive Sliding-Window-History,
          mind. 2 Varianten erforderlich), `join_by_name` vor jedem Send, Verify danach.
          Rotation durch alle Channels aus den konfigurierten Gruppen.
  - MSG:  Konversationen pollen, neue Nachrichten von anderen melden, Antworten
          (Sender ist bereits in processed_users) extra markieren.
  - PN:   Wenn `pn_text` non-empty → an die nächste noch nicht angeschriebene
          weibliche Person im aktuellen Channel eine PN senden (mit `${nick}`-Templating).
          Rotation pausiert solange Queue nicht leer ist. Tageslimit konfigurierbar.
  - DISC: Channel-Liste neu suchen (Instanzen kommen/gehen mit Userzahl).

Persistenz (alle gitignored):
  - .env                  — Knuddels-Credentials (interaktiv beim ersten Start angelegt)
  - messages.json         — Channel-Werbevarianten (beliebig viele ≥ MIN_VARIANTS)
  - pn_text.json          — PN-Vorlage (leer = PN-Modus aus)
  - processed_users.json  — DB der angeschriebenen User-IDs (global, channel-übergreifend)
  - answered_users.json   — DB der User die geantwortet haben (für Antwort-Rate-Stat)
  - channel_groups.json   — vom User ausgewählte Channel-Gruppen
  - bot_config.json       — Intervalle + Tageslimit
  - state.json            — Dashboard-Snapshot
"""
import argparse
import getpass
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import random
import re
import webbrowser
import requests
from collections import deque
from datetime import datetime, timezone, date
from pathlib import Path
from knuddelsAPI import KnuddelsAPI
from classes.MessageContent import (
    ConversationTextMessageContent,
    ConversationQuotedMessageContent,
    ConversationImageMessageContent,
    ConversationSnapMessageContent,
    ConversationKnuddelTransferMessageContent,
)

sys.stdout.reconfigure(encoding="utf-8")

# === Globale Konstanten ===
KNUDDELS_GRAPHQL_URL = "https://api-de.knuddels.de/mono/graphql"

DEFAULT_CHANNEL_GROUP_NAMES = []  # leer = User wählt im Dashboard aus
HISTORY_SIZE = 4
DISCOVER_INTERVAL = 600  # alle 10 Min Channels neu suchen (kommen/gehen mit Userzahl)

# Defaults für die im Dashboard editierbaren Intervalle / Limits.
# Tatsächliche Werte liegen in bot_config (mit Persistenz in bot_config.json).
DEFAULT_BOT_CONFIG = {
    "spam_interval": 120,   # Sekunden zwischen Channel-Sends
    "msg_interval": 30,     # Sekunden zwischen Inbox-Polls (konservativ — `getConversations` paginiert rekursiv)
    "pn_interval": 30,      # Sekunden zwischen PN-Sends
    "daily_pn_limit": 0,    # 0 = kein Limit; >0 = max PN-Sends pro Kalendertag
}
MIN_INTERVAL_SEC = 5
MAX_INTERVAL_SEC = 3600

# Dashboard
DEFAULT_DASHBOARD_PORT = 8080
DASHBOARD_PORT = DEFAULT_DASHBOARD_PORT  # via CLI überschreibbar
DASHBOARD_LOG_SIZE = 6
PN_LOG_SIZE = 10
EVENT_LOG_SIZE = 200
EVENT_LOG_DASHBOARD_LIMIT = 80  # wieviele Events ans Dashboard ausliefern
WORKTREE_DIR = str(Path(__file__).parent)
DATA_DIR = Path(WORKTREE_DIR)  # default; via --config-dir überschreibbar
# Filepfade werden in init_paths() initialisiert (auch von apply_args neu gesetzt).
STATE_FILE = MESSAGES_FILE = PN_TEXT_FILE = PROCESSED_USERS_FILE = None
ANSWERED_USERS_FILE = CHANNEL_GROUPS_FILE = BOT_CONFIG_FILE = ENV_FILE = None
ACCOUNTS_DIR = CURRENT_ACCOUNT_FILE = None   # gesetzt in init_paths()
ACCOUNT_DIR = None                            # gesetzt in init_account_paths()


def init_paths(data_dir: Path):
    """Setzt alle Datei-Globals relativ zu data_dir. Wird in apply_args() und
    am Modul-Ende für den Default-Pfad aufgerufen."""
    global DATA_DIR, STATE_FILE, ACCOUNTS_DIR, CURRENT_ACCOUNT_FILE
    DATA_DIR = data_dir
    STATE_FILE = data_dir / "state.json"
    ACCOUNTS_DIR = data_dir / "accounts"
    CURRENT_ACCOUNT_FILE = data_dir / "current_account.txt"
    # Default: account-Files im Root (Backward-Compat vor erstem Login / Migration)
    init_account_paths(data_dir)


def init_account_paths(account_dir: Path):
    """Setzt alle account-spezifischen Dateipfade. Wird nach Login / Account-Wechsel
    auf das jeweilige accounts/<nick>/-Verzeichnis umgestellt."""
    global ACCOUNT_DIR, MESSAGES_FILE, PN_TEXT_FILE
    global PROCESSED_USERS_FILE, ANSWERED_USERS_FILE
    global CHANNEL_GROUPS_FILE, BOT_CONFIG_FILE, ENV_FILE
    ACCOUNT_DIR = account_dir
    MESSAGES_FILE = account_dir / "messages.json"
    PN_TEXT_FILE = account_dir / "pn_text.json"
    PROCESSED_USERS_FILE = account_dir / "processed_users.json"
    ANSWERED_USERS_FILE = account_dir / "answered_users.json"
    CHANNEL_GROUPS_FILE = account_dir / "channel_groups.json"
    BOT_CONFIG_FILE = account_dir / "bot_config.json"
    ENV_FILE = account_dir / ".env"


init_paths(DATA_DIR)


def atomic_json_write(path, data, indent=None):
    """Schreibt JSON atomar: erst nach <path>.tmp, dann os.replace (crash-sicher)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)

DEFAULT_PN_TEXT = ""  # leer = PN-Modus aus, Bot rotiert dann Channels normal

# Defaults, falls noch keine messages.json existiert. Werden direkt nach erstem Start
# im Dashboard überschrieben. Mindestens 2 Varianten erforderlich (sonst keine Rotation
# möglich), mehr ist beliebig.
MIN_VARIANTS = 2
DEFAULT_MESSAGES = [f"Channel-Nachricht Variante {i+1} — bitte über das Dashboard ersetzen." for i in range(MIN_VARIANTS)]
MESSAGES = list(DEFAULT_MESSAGES)  # zur Laufzeit überschreibbar (Dashboard-Editor, beliebige Länge ≥ MIN_VARIANTS)

# === Shared state ===
api = None
me_id = None
my_nick = None
spam_history = deque(maxlen=HISTORY_SIZE)  # zuletzt gesendete Varianten (für Anti-Wiederholung)
msgs_state = None        # None = noch kein initial-snapshot
channel_group_names = []  # konfigurierte „Mutter-Channel"-Namen (z.B. "Matratzensport")
channels = []             # list of (channel_id, channel_name) — discovered Instanzen
channel_rotation_idx = 0  # Round-Robin-Index
inbox_log = deque(maxlen=DASHBOARD_LOG_SIZE)  # last N incoming PNs
sent_log = deque(maxlen=DASHBOARD_LOG_SIZE)   # last N channel sends
event_log = deque(maxlen=EVENT_LOG_SIZE)      # alle Log-Lines für Dashboard-Pane
# RLock damit verschachtelte log()-Calls aus gelocktem Code keinen Deadlock geben.
state_lock = threading.RLock()       # synchronisiert deque-Mutationen UND -Snapshots
file_lock = threading.Lock()         # synchronisiert JSON-File-Writes (vermeidet Last-Writer-Wins)
_sent_seq = 0  # monotonic ID for sent entries
_evt_seq = 0   # monotonic ID for event entries

# Inter-Thread-Events: HTTP-Handler signalisiert, Main-Loop arbeitet ab
restart_event = threading.Event()       # /api/restart → do_soft_restart
discover_event = threading.Event()      # /api/channel-groups POST → do_discover ohne 10-Min-Warten
config_dirty_event = threading.Event()  # /api/bot-config POST → next_X neu berechnen
switch_account_event = threading.Event()  # /api/switch-account POST → do_switch_account
switch_account_target = {"nick": None}    # Ziel-Nick für den nächsten Account-Wechsel

# Bot-Config (laufzeitveränderlich via Dashboard /api/bot-config)
bot_config = dict(DEFAULT_BOT_CONFIG)
bot_paused = False
test_send_request = []   # Liste von Variant-Indices die beim nächsten Tick als Test gesendet werden sollen
test_send_lock = threading.Lock()

# Tagesbuchhaltung für PN-Limit (date-string → count)
pn_count_today = {"date": None, "count": 0}
_pn_limit_logged = False    # eingeschränkt: log "Limit erreicht" pro Tag nur einmal

# PN-Modul
pn_text = DEFAULT_PN_TEXT
processed_users = set()            # User-IDs die bereits angeschrieben wurden
answered_users = set()             # User-IDs die auf eine PN geantwortet haben
female_queue = []                  # noch zu kontaktierende User-IDs im aktuellen Channel
female_queue_channel = None        # Channel-ID, für die die Queue gilt
female_queue_total = 0             # initiale Größe der Queue beim letzten Populate
pn_sent_session = 0                # PN-Counter seit Bot-Start (resettet beim Restart)
pn_log = deque(maxlen=PN_LOG_SIZE) # letzte N PN-Versuche
_pn_seq = 0                        # monotonic ID


def ts():
    return datetime.now().strftime("%H:%M:%S")


def log(prefix, msg):
    global _evt_seq
    print(f"[{ts()}] {prefix:4s} {msg}", flush=True)
    with state_lock:
        _evt_seq += 1
        event_log.appendleft({
            "id": _evt_seq,
            "ts": datetime.now(timezone.utc).astimezone().isoformat(),
            "prefix": prefix,
            "msg": str(msg),
        })


def load_bot_config():
    """bot_config.json einlesen, Defaults bei fehlenden/ungültigen Werten."""
    global bot_config
    bot_config = dict(DEFAULT_BOT_CONFIG)
    if not BOT_CONFIG_FILE.exists():
        return
    try:
        with open(BOT_CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k, default in DEFAULT_BOT_CONFIG.items():
            v = data.get(k, default)
            if not isinstance(v, int) or v < 0:
                v = default
            if k.endswith("_interval") and v < MIN_INTERVAL_SEC:
                v = MIN_INTERVAL_SEC
            if k.endswith("_interval") and v > MAX_INTERVAL_SEC:
                v = MAX_INTERVAL_SEC
            bot_config[k] = v
    except Exception as e:
        log("WARN", f"bot_config.json laden fehlgeschlagen: {e}, nutze Defaults")


def save_bot_config_to_file(new_cfg):
    """Validiert + schreibt bot_config.json. new_cfg darf nur bekannte Keys haben."""
    global bot_config
    cleaned = dict(bot_config)
    for k in DEFAULT_BOT_CONFIG:
        if k not in new_cfg:
            continue
        v = new_cfg[k]
        if not isinstance(v, (int, float)):
            raise ValueError(f"{k}: muss Zahl sein")
        v = int(v)
        if v < 0:
            raise ValueError(f"{k}: darf nicht negativ sein")
        if k.endswith("_interval"):
            if v < MIN_INTERVAL_SEC or v > MAX_INTERVAL_SEC:
                raise ValueError(f"{k}: muss zwischen {MIN_INTERVAL_SEC} und {MAX_INTERVAL_SEC} Sekunden liegen")
        cleaned[k] = v
    with file_lock:
        atomic_json_write(BOT_CONFIG_FILE, cleaned, indent=2)
    bot_config = cleaned


def reset_pn_count_if_new_day():
    today = date.today().isoformat()
    if pn_count_today["date"] != today:
        pn_count_today["date"] = today
        pn_count_today["count"] = 0


def pn_limit_reached():
    """True wenn das Tageslimit gesetzt UND erreicht ist."""
    reset_pn_count_if_new_day()
    limit = bot_config.get("daily_pn_limit", 0)
    return limit > 0 and pn_count_today["count"] >= limit


def get_accounts_list():
    """Alle bekannten Account-Nicks (= Unterverzeichnisse von ACCOUNTS_DIR mit .env)."""
    if not ACCOUNTS_DIR or not ACCOUNTS_DIR.exists():
        return []
    return sorted(
        d.name for d in ACCOUNTS_DIR.iterdir()
        if d.is_dir() and (d / ".env").exists()
    )


def restore_last_account():
    """Beim Bot-Start: falls ein current_account.txt existiert und das Account-Verzeichnis
    vorhanden ist, Pfade direkt darauf umschalten (kein Wizard nötig)."""
    if not CURRENT_ACCOUNT_FILE or not CURRENT_ACCOUNT_FILE.exists():
        return False
    try:
        nick = CURRENT_ACCOUNT_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return False
    if not nick:
        return False
    account_dir = ACCOUNTS_DIR / nick
    if not account_dir.exists() or not (account_dir / ".env").exists():
        return False
    init_account_paths(account_dir)
    return True


def ensure_account_dir_for_current_user():
    """Nach dem Login: accounts/<nick>/-Verzeichnis anlegen, noch im Root liegende
    Dateien einmalig dorthin migrieren, Pfade umschalten, current_account.txt schreiben."""
    import shutil
    account_dir = ACCOUNTS_DIR / my_nick
    account_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("messages.json", "pn_text.json", "processed_users.json",
                  "answered_users.json", "channel_groups.json", "bot_config.json", ".env"):
        src = DATA_DIR / fname
        dst = account_dir / fname
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
    init_account_paths(account_dir)
    try:
        CURRENT_ACCOUNT_FILE.write_text(my_nick, encoding="utf-8")
    except Exception:
        pass


def do_switch_account(nick):
    """Account wechseln: Pfade umschalten, alle Configs neu laden, Soft-Restart."""
    account_dir = ACCOUNTS_DIR / nick
    if not account_dir.exists() or not (account_dir / ".env").exists():
        log("WARN", f"Account-Wechsel zu '{nick}' fehlgeschlagen: Verzeichnis fehlt")
        return
    log("SWITCH", f"Wechsle zu Account '{nick}'…")
    init_account_paths(account_dir)
    try:
        CURRENT_ACCOUNT_FILE.write_text(nick, encoding="utf-8")
    except Exception:
        pass
    load_bot_config()
    load_messages()
    load_pn_text()
    load_processed()
    load_answered()
    load_channel_groups()
    do_soft_restart()


def ensure_credentials():
    """Erster-Start-Wizard: wenn keine .env mit Knuddels-Login da ist, interaktiv abfragen.
    Schreibt .env (gitignored), danach läuft alles wie sonst über decouple/config()."""
    if ENV_FILE.exists():
        try:
            content = ENV_FILE.read_text(encoding="utf-8")
            if "KNUDDELS_USERNAME=" in content and "KNUDDELS_PASSWORD=" in content:
                return  # alles da
        except Exception:
            pass

    bar = "=" * 60
    print(bar)
    print("  Knuddels-Bot — Erster Start")
    print(bar)
    print()
    print("  Es wurde keine .env mit Zugangsdaten gefunden.")
    print(f"  Ich lege gleich {ENV_FILE.name} im Bot-Verzeichnis an")
    print("  (per .gitignore vom Versionskontrollsystem ausgeschlossen).")
    print()
    print("  Falls du keinen Knuddels-Account hast: ")
    print("  https://www.knuddels.de/ → Registrieren")
    print()

    try:
        user = input("  Knuddels-Nick: ").strip()
    except EOFError:
        print("  Abbruch (kein TTY für interaktive Eingabe).")
        sys.exit(1)
    if not user:
        print("  Abbruch — Nick darf nicht leer sein.")
        sys.exit(1)

    # Passwort NICHT strippen — Whitespace könnte Teil davon sein.
    pw = getpass.getpass("  Passwort  (wird beim Tippen nicht angezeigt): ")
    if not pw:
        print("  Abbruch — Passwort darf nicht leer sein.")
        sys.exit(1)

    # Sehr defensiv: User darf : / Backslash etc. enthalten. Wir schreiben raw ohne Quotes.
    # python-decouple respektiert das. Falls Passwort ein # enthält, könnte das in einigen
    # .env-Parsern als Kommentar interpretiert werden — wir warnen aber nicht extra.
    try:
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write(f"KNUDDELS_USERNAME={user}\n")
            f.write(f"KNUDDELS_PASSWORD={pw}\n")
    except Exception as e:
        print(f"  Fehler beim Schreiben von .env: {e}")
        sys.exit(1)

    # Auf Unix einigermaßen sicher (besitzerlesbar). Auf Windows ignoriert.
    try:
        os.chmod(ENV_FILE, 0o600)
    except Exception:
        pass

    print()
    print(f"  OK — .env angelegt für '{user}'. Login folgt gleich...")
    print(bar)
    print()


def read_env_creds():
    """Liest KNUDDELS_USERNAME / KNUDDELS_PASSWORD direkt aus .env (ohne decouple-Cache).
    decouple cached die erste Lesung — bei Re-Try nach falschen Creds brauchen wir frisch."""
    if not ENV_FILE.exists():
        return None, None
    user = pw = None
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if (len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'")):
                v = v[1:-1]
            if k == "KNUDDELS_USERNAME":
                user = v
            elif k == "KNUDDELS_PASSWORD":
                pw = v
    except Exception:
        return None, None
    return user, pw


def login():
    global api, me_id, my_nick
    user, pw = read_env_creds()
    if not user or not pw:
        raise RuntimeError("Zugangsdaten fehlen in .env")
    api = KnuddelsAPI(user, pw)
    me = api.getCurrentUserNick()
    me_id = me.id
    my_nick = me.nick


def login_with_retry(max_attempts=3):
    """Login mit interaktivem Re-Try bei Fehler (falsche Credentials, Netzwerk, etc.).
    Bei jedem Fehlversuch wird .env gelöscht und der Wizard erneut gestartet."""
    for attempt in range(1, max_attempts + 1):
        try:
            login()
            return
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"\n[FEHLER] Login fehlgeschlagen: {type(e).__name__}: {e}")
            if attempt >= max_attempts:
                print("Maximale Versuche erreicht — Abbruch.")
                sys.exit(1)
            print(f"\nVersuch {attempt+1}/{max_attempts}: Zugangsdaten neu eintragen (Strg+C zum Abbruch)\n")
            try:
                ENV_FILE.unlink()
            except Exception:
                pass
            ensure_credentials()


def write_state():
    """Atomar state.json schreiben (für Dashboard-Frontend)."""
    current_channel_name = None
    if channels and 0 <= channel_rotation_idx < len(channels):
        current_channel_name = channels[channel_rotation_idx][1]
    reset_pn_count_if_new_day()
    answer_rate = None
    if processed_users:
        answer_rate = round(100.0 * len(answered_users) / len(processed_users), 1)
    # PN-Log + Inbox + Sent + Events snapshotten (deque-list() ist atomic in CPython
    # dank GIL — kein expliziter Lock nötig).
    with state_lock:
        pn_snap = list(pn_log)
        inbox_snap = list(inbox_log)
        sent_snap = list(sent_log)
        evt_snap = list(event_log)[:EVENT_LOG_DASHBOARD_LIMIT]
    pn_log_enriched = [{**e, "answered": bool(e.get("user_id") and e["user_id"] in answered_users)}
                       for e in pn_snap]

    payload = {
        "me": my_nick,
        "paused": bot_paused,
        "inbox": inbox_snap,
        "sent": sent_snap,
        "pn": pn_log_enriched,
        "stats": {
            "pn_enabled": pn_enabled(),
            "pn_total_db": len(processed_users),
            "pn_answered": len(answered_users),
            "answer_rate": answer_rate,
            "pn_session": pn_sent_session,
            "pn_today_count": pn_count_today["count"],
            "pn_today_limit": bot_config.get("daily_pn_limit", 0),
            "current_channel": current_channel_name,
            "channel_count": len(channels),
            "pn_queue_remaining": len(female_queue) if female_queue_channel == (channels[channel_rotation_idx][0] if channels else None) else None,
            "pn_queue_total": female_queue_total,
        },
        "config": dict(bot_config),
        "events": evt_snap,
    }
    try:
        with file_lock:
            atomic_json_write(STATE_FILE, payload)
    except Exception as e:
        log("WARN", f"State-Schreiben fehlgeschlagen: {e}")


def _handle_channel_search(handler, _body):
    """GET /api/channel-search?q=... — Sondersache wegen Query-Param."""
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(handler.path).query)
    prefix = (qs.get("q") or [""])[0].strip()
    if not prefix:
        handler._json(200, {"results": []})
        return
    try:
        handler._json(200, {"results": search_channel_groups(prefix)})
    except Exception as e:
        handler._json(500, {"error": str(e)})


def _handle_messages_post(handler, body):
    try:
        save_messages_to_file(body.get("messages", []))
        log("EDIT", f"{len(MESSAGES)} Varianten gespeichert via Dashboard")
        handler._json(200, {"ok": True})
    except Exception as e:
        handler._json(400, {"error": str(e)})


def _handle_pn_text_post(handler, body):
    try:
        save_pn_text_to_file(body.get("text", ""))
        log("EDIT", "PN-Vorlage gespeichert via Dashboard")
        handler._json(200, {"ok": True})
    except Exception as e:
        handler._json(400, {"error": str(e)})


def _handle_channel_groups_post(handler, body):
    try:
        save_channel_groups_to_file(body.get("names", []))
        log("EDIT", f"Channel-Gruppen gespeichert: {channel_group_names}")
        discover_event.set()   # Main-Loop arbeitet do_discover ab (R2)
        handler._json(200, {"ok": True})
    except Exception as e:
        handler._json(400, {"error": str(e)})


def _handle_restart_post(handler, _body):
    restart_event.set()
    log("EDIT", "Restart angefordert via Dashboard")
    handler._json(200, {"ok": True})


def _handle_pause_post(handler, _body):
    global bot_paused
    bot_paused = True
    log("EDIT", "Bot pausiert via Dashboard")
    write_state()
    handler._json(200, {"ok": True, "paused": True})


def _handle_resume_post(handler, _body):
    global bot_paused
    bot_paused = False
    log("EDIT", "Bot fortgesetzt via Dashboard")
    write_state()
    handler._json(200, {"ok": True, "paused": False})


def _handle_bot_config_post(handler, body):
    try:
        save_bot_config_to_file(body)
        log("EDIT", f"Bot-Config gespeichert: {bot_config}")
        config_dirty_event.set()
        handler._json(200, {"ok": True, "config": dict(bot_config)})
    except Exception as e:
        handler._json(400, {"error": str(e)})


def _handle_forget_user_post(handler, body):
    user_id = str(body.get("user_id", "")).strip()
    if not user_id:
        handler._json(400, {"error": "user_id fehlt"})
        return
    removed = forget_user(user_id)
    log("EDIT", f"forget-user {user_id} — {'entfernt' if removed else 'war nicht in DB'}")
    handler._json(200, {"ok": True, "removed": removed})


def _handle_test_send_post(handler, body):
    idx = body.get("variant")
    if not isinstance(idx, int) or idx < 0 or idx >= len(MESSAGES):
        handler._json(400, {"error": f"variant Index 0..{len(MESSAGES)-1} erwartet"})
        return
    with test_send_lock:
        test_send_request.append(idx)
    log("EDIT", f"Test-Send Variante {idx+1} angefordert")
    handler._json(200, {"ok": True})


def _handle_accounts_get(handler, _body):
    handler._json(200, {"accounts": get_accounts_list(), "current": my_nick})


def _handle_switch_account_post(handler, body):
    nick = str(body.get("nick", "")).strip()
    if not nick:
        handler._json(400, {"error": "nick fehlt"})
        return
    account_dir = ACCOUNTS_DIR / nick
    if not account_dir.exists() or not (account_dir / ".env").exists():
        handler._json(404, {"error": f"Account '{nick}' nicht gefunden"})
        return
    if nick == my_nick:
        handler._json(200, {"ok": True, "nick": nick, "note": "bereits aktiv"})
        return
    log("EDIT", f"Account-Wechsel zu '{nick}' angefordert via Dashboard")
    switch_account_target["nick"] = nick
    switch_account_event.set()
    handler._json(200, {"ok": True, "nick": nick})


def _handle_add_account_post(handler, body):
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()
    if not username or not password:
        handler._json(400, {"error": "username und password erforderlich"})
        return
    try:
        tmp_api = KnuddelsAPI(username, password)
        tmp_me = tmp_api.getCurrentUserNick()
        nick = tmp_me.nick
    except Exception as e:
        handler._json(400, {"error": f"Login fehlgeschlagen: {e}"})
        return
    account_dir = ACCOUNTS_DIR / nick
    account_dir.mkdir(parents=True, exist_ok=True)
    env_path = account_dir / ".env"
    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(f"KNUDDELS_USERNAME={username}\n")
            f.write(f"KNUDDELS_PASSWORD={password}\n")
        try:
            import os as _os
            _os.chmod(env_path, 0o600)
        except Exception:
            pass
    except Exception as e:
        handler._json(500, {"error": f".env schreiben fehlgeschlagen: {e}"})
        return
    log("EDIT", f"Neuer Account '{nick}' angelegt — wechsle…")
    switch_account_target["nick"] = nick
    switch_account_event.set()
    handler._json(200, {"ok": True, "nick": nick})


# Route-Maps. Keys sind exakte Pfade, Values sind handler-Funktionen (handler, body) → None.
# Pfade mit Query-Params (z.B. /api/channel-search?q=...) werden über prefix-match unten gehandhabt.
_GET_ROUTES = {
    "/api/messages":        lambda h, _b: h._json(200, {"messages": list(MESSAGES)}),
    "/api/pn-text":         lambda h, _b: h._json(200, {"text": pn_text}),
    "/api/channel-groups":  lambda h, _b: h._json(200, {"names": list(channel_group_names)}),
    "/api/bot-config":      lambda h, _b: h._json(200, dict(bot_config)),
    "/api/accounts":        _handle_accounts_get,
}
_GET_PREFIX_ROUTES = [
    ("/api/channel-search", _handle_channel_search),
]
_POST_ROUTES = {
    "/api/messages":        _handle_messages_post,
    "/api/pn-text":         _handle_pn_text_post,
    "/api/channel-groups":  _handle_channel_groups_post,
    "/api/bot-config":      _handle_bot_config_post,
    "/api/restart":         _handle_restart_post,
    "/api/pause":           _handle_pause_post,
    "/api/resume":          _handle_resume_post,
    "/api/forget-user":     _handle_forget_user_post,
    "/api/test-send":       _handle_test_send_post,
    "/api/switch-account":  _handle_switch_account_post,
    "/api/add-account":     _handle_add_account_post,
}


def start_dashboard_server():
    """HTTP-Server in Daemon-Thread für Dashboard + API."""

    class DashboardHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=WORKTREE_DIR, **kwargs)

        def log_message(self, format, *args):
            pass  # Request-Logs unterdrücken

        def _json(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            h = _GET_ROUTES.get(self.path)
            if h:
                h(self, None)
                return
            for prefix, handler in _GET_PREFIX_ROUTES:
                if self.path.startswith(prefix):
                    handler(self, None)
                    return
            return super().do_GET()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception as e:
                self._json(400, {"error": f"invalid JSON: {e}"})
                return
            h = _POST_ROUTES.get(self.path)
            if h:
                h(self, body)
                return
            self._json(404, {"error": "unknown endpoint"})

    httpd = socketserver.ThreadingTCPServer(("", DASHBOARD_PORT), DashboardHandler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    log("INIT", f"Dashboard: http://localhost:{DASHBOARD_PORT}/dashboard.html")


def describe(content):
    """Polymorphic Content (dataclass-Instanz vom Wrapper) → kurzer Anzeige-String."""
    if content is None:
        return "[?]"
    if isinstance(content, ConversationTextMessageContent):
        return content.formattedText or ""
    if isinstance(content, ConversationQuotedMessageContent):
        return f"[Zitat] {content.formattedText or ''}"
    if isinstance(content, ConversationImageMessageContent):
        return "[Bild]"
    if isinstance(content, ConversationSnapMessageContent):
        return "[Snap]"
    if isinstance(content, ConversationKnuddelTransferMessageContent):
        return f"[Knuddel-Transfer: {content.knuddelAmount}]"
    return f"[{type(content).__name__}]"


def describe_raw(content):
    """Wie describe(), aber für die raw GraphQL-Dicts vom Watchdog (D2-Fix:
    do_watch_msgs umgeht den rekursiv paginierenden Wrapper)."""
    if not content:
        return "[?]"
    t = content.get("__typename")
    if t == "ConversationTextMessageContent":
        return content.get("formattedText") or ""
    if t == "ConversationQuotedMessageContent":
        return f"[Zitat] {content.get('formattedText') or ''}"
    if t == "ConversationImageMessageContent":
        return "[Bild]"
    if t == "ConversationSnapMessageContent":
        return "[Snap]"
    if t == "ConversationKnuddelTransferMessageContent":
        return f"[Knuddel-Transfer: {content.get('knuddelAmount')}]"
    return f"[{t or '?'}]"


# Schlanke single-page Variante von MessengerOverview — nur die Felder die der
# Watchdog braucht (kein Profil-Foto, keine voll aufgeblasenen Participants, etc.).
# Verzichtet auf die Rekursion des Wrappers; die ALLERNEUESTEN Konversationen kommen
# eh in Page 1, weil sortiert nach letzter-Aktivität-DESC. Antworten lassen alte Convs
# nach oben bubblen → kein Verlust ggü. der vollständigen Pagination.
_WATCH_QUERY = """
query MsgWatchdog {
  messenger {
    conversations(limit: 50, filterByState: ALL) {
      conversations {
        id
        otherParticipants { id nick }
        latestConversationMessage {
          id
          sender { id }
          content {
            __typename
            ... on ConversationTextMessageContent { formattedText }
            ... on ConversationQuotedMessageContent { formattedText }
            ... on ConversationImageMessageContent { sensitiveContentClassification }
            ... on ConversationSnapMessageContent { sensitiveContentClassification }
            ... on ConversationKnuddelTransferMessageContent { knuddelAmount }
          }
        }
      }
    }
  }
}
"""


def fetch_recent_conversations():
    """Single-page Recent-Conversations Abfrage (für D2: vermeidet die rekursive
    Pagination des Wrappers, der bei jedem Tick alle ~4 Pages holt)."""
    headers = {"authorization": "Bearer "+api.sessionToken, "content-type": "application/json"}
    r = requests.post(KNUDDELS_GRAPHQL_URL,
        data=json.dumps({"operationName":"MsgWatchdog","variables":{},"query":_WATCH_QUERY}),
        headers=headers)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL: {body['errors']}")
    return (body.get("data") or {}).get("messenger", {}).get("conversations", {}).get("conversations") or []


def is_my_message_visible(channel_id, idx):
    """True, wenn der eingeloggte User irgendwo in den (nur 3!) recentMessages auftaucht.
    Bei aktiven Channels (z.B. 500+ User) verdrängen andere Posts unsere zu schnell,
    dann gibt's false negatives — ist Verify-Limitierung, kein echter Filter."""
    headers = {"authorization": "Bearer "+api.sessionToken, "content-type": "application/json"}
    query = """query GetRecent($id: ID!) {
      channel { channel(id: $id) { recentMessages {
        __typename
        ... on ChannelMsgPublic { id sender { id } }
      } } }
    }"""
    params = {"operationName": "GetRecent", "variables": {"id": channel_id}, "query": query}
    req = requests.post(KNUDDELS_GRAPHQL_URL, data=json.dumps(params), headers=headers)
    req.raise_for_status()
    body = req.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    # Knuddels gibt `data.channel.channel = null` zurück, wenn wir nicht (mehr) im
    # Channel sind. Defensive: dann ist eben nichts sichtbar (kein TypeError-Crash).
    ch = body["data"]["channel"]["channel"]
    if ch is None:
        return False
    msgs = ch.get("recentMessages") or []
    for m in msgs:
        if (m.get("sender") or {}).get("id") == me_id:
            return True
    return False


def load_messages():
    """messages.json beim Start laden, Default-Fallback. Akzeptiert beliebige Länge ≥ MIN_VARIANTS."""
    global MESSAGES
    if MESSAGES_FILE.exists():
        try:
            with open(MESSAGES_FILE, encoding="utf-8") as f:
                data = json.load(f)
            msgs = data.get("messages", [])
            if (isinstance(msgs, list) and len(msgs) >= MIN_VARIANTS
                    and all(isinstance(m, str) and m.strip() for m in msgs)):
                MESSAGES = list(msgs)
                log("INIT", f"{len(MESSAGES)} Varianten aus messages.json geladen")
                return
            log("WARN", f"messages.json ungültig (≥{MIN_VARIANTS} nicht-leere Strings nötig), nutze Defaults")
        except Exception as e:
            log("WARN", f"messages.json laden fehlgeschlagen: {e}, nutze Defaults")
    MESSAGES = list(DEFAULT_MESSAGES)


def save_messages_to_file(msgs):
    """messages.json schreiben + in-memory MESSAGES updaten."""
    global MESSAGES
    if not isinstance(msgs, list):
        raise ValueError("erwarte Liste")
    if len(msgs) < MIN_VARIANTS:
        raise ValueError(f"mindestens {MIN_VARIANTS} Varianten nötig")
    if not all(isinstance(m, str) and m.strip() for m in msgs):
        raise ValueError("leere Variante nicht erlaubt")
    with file_lock:
        atomic_json_write(MESSAGES_FILE, {"messages": msgs}, indent=2)
    MESSAGES = list(msgs)


def pn_enabled():
    """True, wenn das PN-Modul aktiv ist (nicht-leerer Text)."""
    return bool(pn_text and pn_text.strip())


def load_pn_text():
    global pn_text
    if PN_TEXT_FILE.exists():
        try:
            with open(PN_TEXT_FILE, encoding="utf-8") as f:
                data = json.load(f)
            t = data.get("text", "")
            if isinstance(t, str):
                pn_text = t
                if pn_enabled():
                    log("INIT", "PN-Modus aktiv (pn_text.json geladen)")
                else:
                    log("INIT", "PN-Modus aus (pn_text.json ist leer)")
                return
        except Exception as e:
            log("WARN", f"pn_text.json laden fehlgeschlagen: {e}")
    pn_text = DEFAULT_PN_TEXT
    log("INIT", "PN-Modus aus (kein pn_text.json)")


def save_pn_text_to_file(text):
    """Leerer String = PN-Modus aus. Nicht-leer = aktiv."""
    global pn_text, female_queue, female_queue_channel, female_queue_total
    if not isinstance(text, str):
        raise ValueError("PN-Text muss String sein")
    was_enabled = pn_enabled()
    with file_lock:
        atomic_json_write(PN_TEXT_FILE, {"text": text}, indent=2)
    pn_text = text
    now_enabled = pn_enabled()
    # Bei Modus-Wechsel: Queue resetten, damit nächster Tick sauber neu populiert
    # oder beim Ausschalten nichts mehr ansteht
    if was_enabled != now_enabled:
        female_queue = []
        female_queue_channel = None
        female_queue_total = 0
        if now_enabled:
            log("PN", "Modus eingeschaltet")
        else:
            log("PN", "Modus ausgeschaltet — Bot rotiert Channels wieder normal")


def load_channel_groups():
    """channel_groups.json laden — User-Auswahl der zu beackernden Channel-Gruppen."""
    global channel_group_names
    if CHANNEL_GROUPS_FILE.exists():
        try:
            with open(CHANNEL_GROUPS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            names = data.get("names", [])
            if isinstance(names, list) and all(isinstance(n, str) for n in names):
                channel_group_names = [n for n in names if n.strip()]
                log("INIT", f"{len(channel_group_names)} Channel-Gruppen: {channel_group_names}")
                return
        except Exception as e:
            log("WARN", f"channel_groups.json laden fehlgeschlagen: {e}")
    channel_group_names = list(DEFAULT_CHANNEL_GROUP_NAMES)
    if not channel_group_names:
        log("INIT", "Keine Channel-Gruppen konfiguriert — bitte über Dashboard auswählen")


def save_channel_groups_to_file(names):
    """Save + in-memory update. Dedupe + trim."""
    global channel_group_names
    if not isinstance(names, list):
        raise ValueError("erwarte Liste von Strings")
    seen = set()
    clean = []
    for n in names:
        if not isinstance(n, str):
            raise ValueError("Listen-Element ist kein String")
        n = n.strip()
        if n and n not in seen:
            seen.add(n)
            clean.append(n)
    with file_lock:
        atomic_json_write(CHANNEL_GROUPS_FILE, {"names": clean}, indent=2)
    channel_group_names = clean


_PARENT_SUFFIX_RE = re.compile(r"^(.+?)\s+\d+$")


def _parent_name(name):
    """'Flirt 2' -> 'Flirt'; 'Flirt' -> 'Flirt'; 'Flirten' -> 'Flirten'."""
    m = _PARENT_SUFFIX_RE.match(name)
    return m.group(1) if m else name


def search_channel_groups(prefix):
    """Knuddels-Suche nach Channel-Gruppen mit gegebenem Prefix.
    Returns list of {name, instances, users}.

    Knuddels liefert bei manchen Gruppen die Sub-Instanzen als SEPARATE channelGroups
    zurück (z.B. „Flirt 2", „Flirt 3" sind eigene Groups, nicht Channels unter „Flirt").
    Wir consolidaten alle Treffer auf den Parent-Namen (= Name ohne trailing „ <Zahl>"),
    summieren Instanzen + Online-User und liefern eine deduplizierte Liste — so sieht der
    User nur den Mutterkanal und der Bot rotiert intern durch alle Sub-Instanzen.

    HINWEIS: prefix muss als `String!` deklariert sein (Knuddels' Argument ist non-null).
    Vorher hatten wir `String` (nullable) und Knuddels lehnte mit VariableTypeMismatch ab."""
    headers = {"authorization": "Bearer "+api.sessionToken, "content-type": "application/json"}
    query = """query SearchGroups($prefix: String!) {
      channel { channelGroups(prefix: $prefix) {
        id name channels { id onlineUserCount }
      } }
    }"""
    r = requests.post(KNUDDELS_GRAPHQL_URL,
        data=json.dumps({"operationName":"SearchGroups", "variables":{"prefix": prefix}, "query": query}),
        headers=headers)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL: {body['errors']}")
    groups = body["data"]["channel"]["channelGroups"] or []
    parents = {}  # parent -> {"instances": int, "users": int}
    for g in groups:
        parent = _parent_name(g["name"])
        chs = g.get("channels") or []
        entry = parents.setdefault(parent, {"instances": 0, "users": 0})
        entry["instances"] += len(chs)
        entry["users"] += sum((c.get("onlineUserCount") or 0) for c in chs)
    out = [{"name": k, "instances": v["instances"], "users": v["users"]} for k, v in parents.items()]
    # populärste zuerst
    out.sort(key=lambda x: x["users"], reverse=True)
    return out


def load_processed():
    global processed_users
    if PROCESSED_USERS_FILE.exists():
        try:
            with open(PROCESSED_USERS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            ids = data.get("ids", [])
            if isinstance(ids, list):
                processed_users = set(str(i) for i in ids)
                log("INIT", f"PN-DB: {len(processed_users)} bereits kontaktierte User geladen")
                return
        except Exception as e:
            log("WARN", f"processed_users.json laden fehlgeschlagen: {e}")
    processed_users = set()


def mark_processed(user_id):
    """User-ID in DB aufnehmen + atomar auf Disk schreiben."""
    processed_users.add(str(user_id))
    try:
        with file_lock:
            atomic_json_write(PROCESSED_USERS_FILE, {"ids": list(processed_users)})
    except Exception as e:
        log("WARN", f"PN-DB schreiben fehlgeschlagen: {e}")


def load_answered():
    global answered_users
    if ANSWERED_USERS_FILE.exists():
        try:
            with open(ANSWERED_USERS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            ids = data.get("ids", [])
            if isinstance(ids, list):
                answered_users = set(str(i) for i in ids)
                log("INIT", f"Antworten-DB: {len(answered_users)} bekannte Antworten")
                return
        except Exception as e:
            log("WARN", f"answered_users.json laden fehlgeschlagen: {e}")
    answered_users = set()


def mark_answered(user_id):
    answered_users.add(str(user_id))
    try:
        with file_lock:
            atomic_json_write(ANSWERED_USERS_FILE, {"ids": list(answered_users)})
    except Exception as e:
        log("WARN", f"Antworten-DB schreiben fehlgeschlagen: {e}")


def forget_user(user_id):
    """User aus processed_users + answered_users entfernen, beide Files updaten."""
    sid = str(user_id)
    removed_processed = sid in processed_users
    removed_answered = sid in answered_users
    processed_users.discard(sid)
    answered_users.discard(sid)
    with file_lock:
        if removed_processed:
            atomic_json_write(PROCESSED_USERS_FILE, {"ids": list(processed_users)})
        if removed_answered:
            atomic_json_write(ANSWERED_USERS_FILE, {"ids": list(answered_users)})
    return removed_processed or removed_answered


def render_pn_text(template, nick):
    """${nick} im Template durch echten Nick ersetzen."""
    return (template or "").replace("${nick}", nick or "")


def pn_log_add(nick, status, detail=None, user_id=None):
    """status: 'ok' | 'filter' | 'error'. user_id optional — wird für Antwort-Linking gebraucht."""
    global _pn_seq
    with state_lock:
        _pn_seq += 1
        pn_log.appendleft({
            "id": _pn_seq,
            "ts": datetime.now(timezone.utc).astimezone().isoformat(),
            "nick": nick,
            "user_id": str(user_id) if user_id is not None else None,
            "status": status,
            "detail": detail,
        })


def send_pn(conversation_id, text):
    """PN senden mit Error-Check (Wrapper ignoriert error-Feld stillschweigend)."""
    headers = {"authorization": "Bearer "+api.sessionToken, "content-type": "application/json"}
    query = ("mutation MessengerSendMessage($id: ID!, $text: String!) "
             "{ messenger { sendMessage(conversationId: $id, text: $text) "
             "{ error { type filterReason __typename } __typename } __typename } }")
    params = {"operationName": "MessengerSendMessage",
              "variables": {"id": conversation_id, "text": text}, "query": query}
    r = requests.post(KNUDDELS_GRAPHQL_URL,
                      data=json.dumps(params), headers=headers)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL: {body['errors']}")
    result = body["data"]["messenger"]["sendMessage"]
    if result.get("error"):
        e = result["error"]
        raise RuntimeError(f"{e.get('type')}: {e.get('filterReason') or ''}".strip(": "))


def join_by_name(name):
    """Versuch, Channel namens `name` zu joinen. Returns (channel_id, error_type)."""
    headers = {"authorization": "Bearer "+api.sessionToken, "content-type": "application/json"}
    query = """mutation JoinByName($name: String!, $confirmed: Boolean) {
      channel { joinByName(name: $name, confirmed: $confirmed) {
        channel { id name __typename }
        error { type freetext __typename }
        __typename
      } }
    }"""
    params = {"operationName": "JoinByName", "variables": {"name": name, "confirmed": True}, "query": query}
    r = requests.post(KNUDDELS_GRAPHQL_URL, data=json.dumps(params), headers=headers)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        return None, f"GraphQL: {body['errors']}"
    result = body["data"]["channel"]["joinByName"]
    if result.get("error"):
        return None, result["error"].get("type") or "unknown_error"
    if result.get("channel"):
        return result["channel"]["id"], None
    return None, "no channel returned"


def do_discover():
    """Für jede konfigurierte Parent-Gruppe alle Channel-Instanzen finden — egal ob sie
    in Knuddels als eine Gruppe mit mehreren Channels (z.B. „Matratzensport") oder als
    mehrere sibling-Groups (z.B. „Flirt", „Flirt 2", „Flirt 3") modelliert sind.

    Vorgehen: `channelGroups(prefix=name)` returniert alle Groups, deren Name mit dem
    Parent-Namen beginnt; wir akzeptieren nur die, deren Name == parent ODER
    parent + „ <Zahl>" ist — sonst würden „Flirten", „Flirtcafé" usw. mit reinrutschen."""
    global channels
    if not channel_group_names:
        if channels:
            log("DISC", "Keine Channel-Gruppen mehr konfiguriert — Channel-Liste geleert")
        channels = []
        return

    headers = {"authorization": "Bearer "+api.sessionToken, "content-type": "application/json"}
    query = """query G($prefix: String!) {
      channel { channelGroups(prefix: $prefix) { name channels { id name } } }
    }"""
    found = []
    seen_ids = set()
    for parent in channel_group_names:
        pat = re.compile(r'^' + re.escape(parent) + r'(?:\s+\d+)?$')
        try:
            r = requests.post(KNUDDELS_GRAPHQL_URL,
                data=json.dumps({"operationName":"G", "variables":{"prefix": parent}, "query": query}),
                headers=headers)
            r.raise_for_status()
            body = r.json()
            if body.get("errors"):
                log("DISC", f"Gruppe '{parent}' fehler: {body['errors'][0].get('message','')[:80]}")
                continue
            groups = (body.get("data") or {}).get("channel", {}).get("channelGroups") or []
            matched = 0
            for g in groups:
                if not pat.match(g.get("name") or ""):
                    continue
                matched += 1
                for ch in g.get("channels") or []:
                    cid = ch.get("id")
                    if cid is None or cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    found.append((cid, ch.get("name") or ""))
            if matched == 0:
                log("DISC", f"Gruppe '{parent}' nicht gefunden")
        except Exception as e:
            log("DISC", f"Gruppe '{parent}' Fehler: {e}")
    found.sort(key=lambda x: x[0])
    if [c[0] for c in found] != [c[0] for c in channels]:
        log("DISC", f"Channels ({len(found)}): {[c[1] for c in found]}")
    channels = found


def do_spam():
    global channel_rotation_idx, female_queue, female_queue_channel
    if not channels:
        log("SPAM", "Keine Channels bekannt — skip")
        return

    # Aktuellen Channel anvisieren (Rotation passiert nur, wenn PN-Queue erschöpft)
    # → siehe Ende dieser Funktion
    ch_id = ch_name = None
    for offset in range(len(channels)):
        i = (channel_rotation_idx + offset) % len(channels)
        candidate_id, candidate_name = channels[i]
        joined_id, err = join_by_name(candidate_name)
        if joined_id is not None:
            ch_id = joined_id
            ch_name = candidate_name
            channel_rotation_idx = i  # ohne +1, advance erst wenn PN-Queue leer
            if joined_id != candidate_id:
                log("SPAM", f"Join '{candidate_name}' -> Sub-Channel {joined_id}")
            break
        log("SPAM", f"Join '{candidate_name}' fehlgeschlagen: {err}")

    if ch_id is None:
        log("SPAM", "Konnte in keinen Channel joinen — skip")
        return

    # Variante wählen (adaptives Sliding-Window): bei N Varianten dürfen die letzten
    # min(HISTORY_SIZE, N-1) nicht wiederholt werden. Garantiert dass es immer ≥1
    # Kandidaten gibt UND nie zwei gleiche hintereinander, auch bei N=2.
    n = len(MESSAGES)
    block_count = min(HISTORY_SIZE, max(0, n - 1))
    blocked = set(list(spam_history)[-block_count:]) if block_count else set()
    candidates = [i for i in range(n) if i not in blocked]
    idx = random.choice(candidates)

    try:
        api.sendMessageInChannel(ch_id, MESSAGES[idx])
    except Exception as e:
        log("SPAM", f"V{idx+1} -> '{ch_name}': Send-Fehler: {e}")
        return
    spam_history.append(idx)

    time.sleep(1)
    visible = None
    try:
        visible = is_my_message_visible(ch_id, idx)
    except Exception as e:
        log("SPAM", f"V{idx+1} -> '{ch_name}': Verify-Fehler: {e}")
    else:
        log("SPAM", f"V{idx+1} -> '{ch_name}': {'sichtbar' if visible else 'NICHT sichtbar'}")

    global _sent_seq
    with state_lock:
        _sent_seq += 1
        sent_log.appendleft({
            "id": _sent_seq,
            "ts": datetime.now(timezone.utc).astimezone().isoformat(),
            "channel": ch_name,
            "variant": idx + 1,
            "visible": visible,
        })

    # Channel-Rotation:
    # - PN-Modus AUS  → jeden Tick zum nächsten Channel rotieren
    # - PN-Modus AN   → erst rotieren wenn die Female-Queue für diesen Channel leer ist
    if not pn_enabled():
        channel_rotation_idx = (channel_rotation_idx + 1) % len(channels)
    elif female_queue_channel == ch_id and not female_queue:
        channel_rotation_idx = (channel_rotation_idx + 1) % len(channels)
        log("SPAM", f"PN-Queue für '{ch_name}' leer — Rotation zu '{channels[channel_rotation_idx][1]}'")
    write_state()


def do_pn():
    """Ein PN an die nächste weibliche, noch nicht angeschriebene Person im aktuellen Channel."""
    global female_queue, female_queue_channel, female_queue_total, pn_sent_session
    if not pn_enabled():
        return  # PN-Modus aus
    if not channels:
        return
    global _pn_limit_logged
    if pn_limit_reached():
        if not _pn_limit_logged:
            log("PN", f"Tageslimit erreicht ({bot_config['daily_pn_limit']}) — PN-Versand pausiert bis morgen")
            _pn_limit_logged = True
        return
    _pn_limit_logged = False

    cid, cname = channels[channel_rotation_idx]

    # Queue für anderen Channel? Reset + neu populieren.
    if female_queue_channel != cid:
        female_queue.clear()
        female_queue_channel = cid
        try:
            ch = api.getChannel(cid)
        except Exception as e:
            log("PN", f"Channel-Lookup für '{cname}' fehlgeschlagen: {e}")
            return
        users = ch.users or []
        eligible = [u.id for u in users if (u.gender or "").upper() == "FEMALE" and u.id not in processed_users]
        female_queue = eligible
        female_queue_total = len(eligible)
        female_total = sum(1 for u in users if (u.gender or "").upper() == "FEMALE")
        log("PN", f"Queue '{cname}': {len(eligible)}/{female_total} weiblich/unkontaktiert")

    if not female_queue:
        return  # nichts zu tun, Rotation läuft

    user_id = female_queue.pop(0)

    # User-Profil holen für canReceiveMessages + conversationId
    try:
        user = api.getUserMacroBox(user_id)
    except Exception as e:
        log("PN", f"Profile-Lookup {user_id}: {e}")
        pn_log_add(str(user_id), "error", f"Profile-Lookup: {e}", user_id=user_id)
        mark_processed(user_id)
        write_state()
        return

    nick = user.nick or user_id
    sent_ok = False
    if not user.canReceiveMessages:
        log("PN", f"{nick}: kann keine PN empfangen (Filter), skip")
        pn_log_add(nick, "filter", "Empfänger blockt PN", user_id=user_id)
    elif not user.conversationId:
        log("PN", f"{nick}: keine conversationId, skip")
        pn_log_add(nick, "filter", "keine Conversation", user_id=user_id)
    else:
        try:
            personalized = render_pn_text(pn_text, nick)
            send_pn(user.conversationId, personalized)
            sent_ok = True
            pn_sent_session += 1
            reset_pn_count_if_new_day()
            pn_count_today["count"] += 1
            pn_log_add(nick, "ok", user_id=user_id)
            log("PN", f"{nick}: angeschrieben ({female_queue_total - len(female_queue)}/{female_queue_total})")
        except Exception as e:
            msg = str(e)
            log("PN", f"{nick}: Senden fehlgeschlagen ({msg})")
            # CONTACT_FILTER (WRONG_GENDER / WRONG_AGE) ist ein weicher Filter — Empfänger lässt
            # uns nicht durch, kein echter Fehler. Anders einfärben.
            kind = "filter" if "CONTACT_FILTER" in msg or "FILTER" in msg.upper() else "error"
            pn_log_add(nick, kind, msg, user_id=user_id)

    # Archivieren NUR wenn der Send tatsächlich durchging. Race: ohne Pause überschreibt
    # Knuddels' nachträglicher Send-Commit den ARCHIVED-Status wieder auf VISIBLE — selbst
    # wenn archive sofort danach gerufen wird und error: null zurückgibt. 1-2s Pause reichen.
    if sent_ok:
        time.sleep(1.5)
        try:
            api.archiveConversation(user.conversationId)
            log("PN", f"{nick}: Conv archiviert")
        except Exception as e:
            log("PN", f"{nick}: Archivieren fehlgeschlagen ({e})")

    mark_processed(user_id)
    write_state()


def do_watch_msgs():
    global msgs_state
    # D2: non-rekursive Single-Page-Query statt api.getConversations() (das paginiert
    # alle Conversations bei jedem Tick rekursiv). Neueste Aktivität ist immer Page 1.
    convs = fetch_recent_conversations()
    new_state = {}
    for c in convs:
        latest = c.get("latestConversationMessage")
        if not latest:
            continue
        participants = c.get("otherParticipants") or []
        other = participants[0] if participants else None
        nick = (other or {}).get("nick", "?")
        other_id = (other or {}).get("id")
        sender_id = ((latest.get("sender") or {}).get("id"))
        new_state[c["id"]] = {
            "msg_id": latest["id"],
            "nick": nick,
            "content": latest.get("content"),
            "from_me": sender_id == me_id,
            "other_id": other_id,
        }

    if msgs_state is None:
        log("MSG", f"Initial-Snapshot: {len(new_state)} Konversationen")
    else:
        new_entries = []
        for cid, info in new_state.items():
            prev = msgs_state.get(cid)
            if prev is None:
                prefix = "NEUER CHAT"
            elif prev["msg_id"] != info["msg_id"]:
                prefix = "NEU"
            else:
                continue
            if info["from_me"]:
                continue
            text = describe_raw(info["content"])
            log("MSG", f"{prefix} {info['nick']}: {text[:200]}")
            other_id = info.get("other_id")
            was_already_processed = bool(other_id and other_id in processed_users)
            if other_id and not was_already_processed:
                # Erstkontakt von ihrer Seite — in PN-DB damit wir nicht doppelt anschreiben
                mark_processed(other_id)
                log("MSG", f"  → {info['nick']} in PN-DB aufgenommen")
            elif other_id and was_already_processed and other_id not in answered_users:
                # War schon angeschrieben → DAS hier ist eine Antwort
                mark_answered(other_id)
                log("MSG", f"  ↳ Antwort von angeschriebener Person: {info['nick']}")
            new_entries.append({
                "id": info["msg_id"],
                "nick": info["nick"],
                "ts": datetime.now(timezone.utc).astimezone().isoformat(),
                "preview": text[:300],
                "is_reply": other_id in answered_users if other_id else False,
            })
        with state_lock:
            for e in new_entries:
                inbox_log.appendleft(e)
        if new_entries:
            write_state()

    msgs_state = new_state


# Diese Exception-Typen lösen Re-Login + Retry aus (Session/Netzwerk-Fehler).
# Andere (ValueError, KeyError, ...) sind Bugs in unserem Code — kein Re-Login.
_RETRY_EXC_TYPES = (
    requests.HTTPError,
    requests.ConnectionError,
    requests.Timeout,
    RuntimeError,    # u.a. GraphQL-Errors aus unseren Wrappern
    TypeError,       # z.B. None-Subscripting bei Library-Bugs gegen kaputtes API-Response
)


def safe(name, fn):
    """Task ausführen; bei Netzwerk-/Session-Fehler einmal Re-Login + Retry."""
    try:
        fn()
        return True
    except _RETRY_EXC_TYPES as e:
        log("WARN", f"{name}: {type(e).__name__}: {e} — versuche Re-Login")
    except Exception as e:
        # Andere Exceptions sind Bugs, nicht Session-Probleme — kein Retry.
        log("WARN", f"{name}: {type(e).__name__}: {e} (kein Re-Login bei diesem Fehlertyp)")
        return False
    try:
        login()
        log("WARN", "Re-Login OK — retry")
    except Exception as e2:
        log("WARN", f"Re-Login fehlgeschlagen: {e2}")
        return False
    try:
        fn()
        return True
    except Exception as e3:
        log("WARN", f"{name} nach Re-Login fehlgeschlagen: {e3}")
        return False


def execute_test_send(idx):
    """Test-Send: sofortiger einmaliger Send einer Variante in den aktuell rotierten Channel.
    Beeinflusst die Sliding-Window-History oder die Rotation NICHT — dient nur dem manuellen Probieren."""
    if not channels:
        log("TEST", "Keine Channels — abgebrochen")
        return
    if not (0 <= idx < len(MESSAGES)):
        log("TEST", f"Ungültiger Variant-Index {idx}")
        return
    ch_id, ch_name = channels[channel_rotation_idx]
    joined_id, err = join_by_name(ch_name)
    if joined_id is None:
        log("TEST", f"Join '{ch_name}' fehlgeschlagen: {err}")
        return
    try:
        api.sendMessageInChannel(joined_id, MESSAGES[idx])
        log("TEST", f"V{idx+1} → '{ch_name}' (Test, ohne Rotation/History)")
    except Exception as e:
        log("TEST", f"V{idx+1} → '{ch_name}' fail: {e}")


def do_soft_restart():
    """Soft-Restart: neue Session, Discovery, ALLE Runtime-Logs leeren, States resetten.
    PN-Datenbank (processed_users, answered_users) bleibt — die ist persistent gedacht."""
    global msgs_state, channel_rotation_idx, female_queue, female_queue_channel, female_queue_total
    login()
    log("RESTART", f"neu eingeloggt als {my_nick}")
    with state_lock:
        inbox_log.clear()
        sent_log.clear()
        pn_log.clear()
        event_log.clear()
    spam_history.clear()  # spam_history wird nur im Main-Thread benutzt — kein Lock nötig
    msgs_state = None
    channel_rotation_idx = 0
    female_queue = []
    female_queue_channel = None
    female_queue_total = 0
    do_discover()
    write_state()


def main(open_browser=True):
    # Letzten aktiven Account wiederherstellen (wenn current_account.txt existiert)
    restored = restore_last_account()
    if not restored:
        ensure_credentials()
    load_bot_config()
    load_messages()
    load_pn_text()
    load_processed()
    load_answered()
    load_channel_groups()
    login_with_retry()
    log("INIT", f"eingeloggt als {my_nick}")
    # Account-Verzeichnis anlegen/migrieren (nur beim ersten Mal nötig)
    ensure_account_dir_for_current_user()
    # Nach Migration Configs neu laden (Pfade zeigen jetzt auf accounts/<nick>/)
    load_bot_config()
    load_messages()
    load_pn_text()
    load_processed()
    load_answered()
    load_channel_groups()
    write_state()
    start_dashboard_server()
    if open_browser:
        url = f"http://localhost:{DASHBOARD_PORT}/dashboard.html"
        # In Daemon-Thread, weil webbrowser.open auf manchen Systemen (Linux ohne
        # DISPLAY, fehlende default-Apps) den aufrufenden Thread sekundenlang blockt.
        def _open():
            try:
                webbrowser.open(url)
                log("INIT", f"Browser geöffnet: {url}")
            except Exception as e:
                log("INIT", f"Browser konnte nicht automatisch geöffnet werden: {e}")
        threading.Thread(target=_open, daemon=True).start()
    safe("DISC", do_discover)
    log("INIT", "Loop läuft (Intervalle in der bot_config änderbar)")

    now = time.time()
    next_spam = now
    next_msgs = now
    next_pn = now + bot_config["pn_interval"]
    next_disc = now + DISCOVER_INTERVAL

    while True:
        # Account-Wechsel?
        if switch_account_event.is_set():
            switch_account_event.clear()
            target = switch_account_target.get("nick")
            if target:
                safe("SWITCH", lambda t=target: do_switch_account(t))
            now = time.time()
            next_spam = now
            next_msgs = now
            next_pn = now + bot_config["pn_interval"]
            next_disc = now + DISCOVER_INTERVAL
            continue

        # Restart?
        if restart_event.is_set():
            safe("RESTART", do_soft_restart)
            restart_event.clear()
            now = time.time()
            next_spam = now
            next_msgs = now
            next_pn = now + bot_config["pn_interval"]
            next_disc = now + DISCOVER_INTERVAL

        # Channel-Gruppen geändert → discover + rotation-idx reset
        if discover_event.is_set():
            discover_event.clear()
            global channel_rotation_idx
            channel_rotation_idx = 0
            safe("DISC", do_discover)
            now = time.time()
            next_disc = now + DISCOVER_INTERVAL

        # Bot-Config geändert → next_X auf neue Intervalle clampen
        if config_dirty_event.is_set():
            config_dirty_event.clear()
            now = time.time()
            next_spam = min(next_spam, now + bot_config["spam_interval"])
            next_msgs = min(next_msgs, now + bot_config["msg_interval"])
            next_pn = min(next_pn, now + bot_config["pn_interval"])

        # Test-Send aus Dashboard?
        with test_send_lock:
            pending_tests = list(test_send_request)
            test_send_request.clear()
        for idx in pending_tests:
            safe("TEST", lambda i=idx: execute_test_send(i))

        now = time.time()
        next_due = min(next_spam, next_msgs, next_pn, next_disc)
        if next_due > now:
            # Wake-up auf restart_event ODER discover_event ODER config_dirty_event
            # (alle drei nutzen restart_event.wait erstmal nicht — wir pollen alle 1s)
            time.sleep(min(next_due - now, 1.0))
            continue

        # Pausierte Bot: SPAM + PN überspringen, MSG und DISC laufen weiter
        if now >= next_spam:
            if not bot_paused:
                safe("SPAM", do_spam)
            next_spam = now + bot_config["spam_interval"]
        if now >= next_msgs:
            safe("MSG", do_watch_msgs)
            next_msgs = now + bot_config["msg_interval"]
        if now >= next_pn:
            if not bot_paused:
                safe("PN", do_pn)
            next_pn = now + bot_config["pn_interval"]
        if now >= next_disc:
            safe("DISC", do_discover)
            next_disc = now + DISCOVER_INTERVAL


def parse_args():
    p = argparse.ArgumentParser(description="Knuddels-Channel-Bot mit Live-Dashboard.")
    p.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT,
                   help=f"Dashboard-Port (default: {DEFAULT_DASHBOARD_PORT})")
    p.add_argument("--no-browser", action="store_true",
                   help="Browser nicht automatisch öffnen")
    p.add_argument("--config-dir", type=str, default=None,
                   help="Verzeichnis für .env und alle *.json-Files (default: Verzeichnis dieses Skripts)")
    return p.parse_args()


def apply_args(args):
    global DASHBOARD_PORT
    DASHBOARD_PORT = args.port
    if args.config_dir:
        custom = Path(args.config_dir).resolve()
        custom.mkdir(parents=True, exist_ok=True)
        init_paths(custom)   # ein zentraler Punkt — kein File mehr vergessen


if __name__ == "__main__":
    try:
        _args = parse_args()
        apply_args(_args)
        main(open_browser=not _args.no_browser)
    except KeyboardInterrupt:
        log("STOP", "abgebrochen")
