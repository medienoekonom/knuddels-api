# Knuddels-Bot

Python-Wrapper für die inoffizielle GraphQL-API von [knuddels.de](https://www.knuddels.de/) plus ein langlaufender Bot mit Browser-Dashboard. Postet rotierend Werbenachrichten in Channels, beobachtet den Posteingang, und kann optional Privatnachrichten an weibliche Profile im Channel schicken.

---

## Bevor du das nutzt: Ehrlichkeitsklausel

Die API ist **reverse-engineered** und nicht von Knuddels dokumentiert. Verhalten kann sich jederzeit ändern, Accounts können gesperrt werden, Knuddels könnte die Zugänge zumachen.

Der **PN-Modus** (massenhafte Privatnachrichten an weibliche Profile) ist ein Feature, das du **bewusst aktivieren musst**. Per Default ist es aus. Wenn du es nutzt: dein Bot wird unaufgeforderte Kontaktanfragen an Personen senden. Manche dieser Empfängerinnen haben in ihren Knuddels-Filtern explizit "keine Männer" oder "nicht in dieser Altersgruppe" eingestellt — der Bot bekommt von der API einen `CONTACT_FILTER: WRONG_GENDER`/`WRONG_AGE` zurück. Knuddels hat diesen Filter eingebaut, **um diese Art von Verhalten zu unterbinden**. Wenn du den Filter konsequent triggerst, kann es einen Shadowban geben.

Tools haben keine Moral, Menschen schon. Entscheide bewusst.

---

## Was kann der Bot?

- **Channel-Rotation**: round-robin durch mehrere Channel-Gruppen (z.B. `Matratzensport 1/2/3`, `Erotic`) und alle Sub-Instanzen davon, mit automatischem Re-Discover bei Wechsel der Userzahl
- **Anti-Wiederholungs-Sliding-Window**: gleiche Variante kommt nie zweimal kurz hintereinander
- **Verify nach Send**: prüft via `recentMessages`-Query ob die Nachricht tatsächlich im Channel landet
- **Posteingang-Watchdog**: pollt Konversationen, meldet neue Privatnachrichten von anderen
- **Antworten-Tracking**: erkennt wenn jemand auf eine Bot-PN antwortet → markiert sie im Dashboard und in `answered_users.json` für die Antwort-Rate-Statistik
- **PN-Modus** (optional): schickt im aktuellen Channel an jedes weibliche Profil eine PN, das wir noch nicht angeschrieben haben. Globale DB verhindert Doppelt-Anschreiben über alle Channels hinweg.
- **Templating**: `${nick}` im PN-Text wird durch den Empfänger-Nick ersetzt
- **Tageslimit**: konfigurierbarer Cap auf PNs pro Kalendertag (Schutz vor Shadowban)
- **Auto-Archive**: Conv direkt nach erfolgreicher PN archivieren — Knuddels poppt sie automatisch zurück in den Inbox-Tab sobald Antwort kommt
- **Browser-Dashboard**: 3-Spalten-Layout mit Live-Log, Editor für Varianten/PN-Vorlage/Timing/Channel-Auswahl, Pause/Resume + Restart
- **Pause/Resume**: kein Restart nötig wenn man nur kurz alle Outbound-Aktionen aussetzen will
- **Multi-Account**: mehrere Knuddels-Accounts speichern und per Dashboard-Klick wechseln. Jeder Account hat eigene Channels, Varianten, PN-Vorlage und DB (Credentials, Settings und Kontakt-DB sind account-isoliert in `accounts/<nick>/`)

---

## Quick Start

```bash
git clone https://github.com/henrydatei/knuddels-api.git
cd knuddels-api
pip install -r requirements.txt
python knuddels_bot.py
```

Beim ersten Start werden Knuddels-Nick und Passwort interaktiv abgefragt und in `.env` gespeichert (gitignored). Danach öffnet sich automatisch der Browser mit dem Dashboard auf <http://localhost:8080/dashboard.html>.

---

## Voraussetzungen

- **Python 3.10+** (wegen `Path.resolve()`-Verhalten, f-strings mit Self-Reference, walrus, `dict.__or__`)
- **Knuddels-Account** — der Bot ist eine Hülle um deinen normalen Account, kein Headless-Anonymizer
- **OS**: getestet auf Windows 10, sollte auch auf Linux/macOS laufen
- **Browser** für das Dashboard

Die einzigen Dependencies stehen in `requirements.txt`:

```
requests >= 2.28
dacite >= 1.8
python-decouple >= 3.8
tqdm >= 4.65
```

---

## Erster Start

```
============================================================
  Knuddels-Bot — Erster Start
============================================================

  Es wurde keine .env mit Zugangsdaten gefunden.
  Ich lege gleich .env im Bot-Verzeichnis an
  (per .gitignore vom Versionskontrollsystem ausgeschlossen).

  Falls du keinen Knuddels-Account hast: 
  https://www.knuddels.de/ → Registrieren

  Knuddels-Nick: _
  Passwort  (wird beim Tippen nicht angezeigt): _

  OK — .env angelegt für 'meinnick'. Login folgt gleich...
============================================================

[14:01:22] INIT eingeloggt als meinnick
[14:01:22] INIT Dashboard: http://localhost:8080/dashboard.html
[14:01:22] INIT Browser geöffnet: http://localhost:8080/dashboard.html
[14:01:22] DISC Channels (0): []
[14:01:22] INIT Loop läuft (Intervalle in der bot_config änderbar)
```

Bei falschem Passwort versucht der Wizard bis zu **3×** neu — beim Fehlschlag wird `.env` gelöscht und die Eingabe wiederholt sich.

Direkt nach dem Login startet der Dashboard-Server, der Bot ist aber im **Idle-Zustand**: ohne konfigurierte Channel-Gruppe gibt's nichts zu posten. Im Dashboard erscheint dann ein gelber Hinweis.

---

## Das Dashboard

Aufbau von oben nach unten:

### Header

Links: Bot-Name + aktuell eingeloggter Nick.

Rechts (von links nach rechts):
- **Status-Pulse** — grün wenn Bot reagiert, rot wenn nicht
- **Update-Zeit** — wann das Dashboard zuletzt frische Daten gezogen hat
- **Account wechseln** — öffnet ein Modal mit allen gespeicherten Accounts. Klick auf einen Account wechselt sofort (Re-Login + eigene Channel-/Varianten-/PN-Konfiguration laden). Über „Neuen Account hinzufügen" können weitere Knuddels-Accounts mit Nick + Passwort eingetragen werden.
- **Pausieren / Fortsetzen** — schaltet SPAM und PN aus (MSG-Watch + DISC laufen weiter, damit Antworten weiter erkannt werden)
- **Bot neu starten** — Soft-Restart: neue Session, Channel-Liste neu suchen, Inbox/Sent/PN/Event-Logs leeren. Die persistente PN-DB (`processed_users.json`, `answered_users.json`) bleibt.

### Stats-Leiste

| Feld | Bedeutung |
|---|---|
| **Channel** | Aktueller Channel in der Rotation |
| **Channels** | Anzahl gefundener Instanzen über alle Gruppen |
| **PN-Modus** | `aktiv` (grün) wenn PN-Vorlage gesetzt, sonst `aus` |
| **Queue** | Im PN-Modus: `bearbeitet / gesamt` für die aktuell laufende Channel-Queue |
| **Heute** | PNs heute (Kalendertag) — bei Tageslimit `aktuell / limit` |
| **Geantwortet** | Anzahl User die auf eine unserer PNs geantwortet haben |
| **Rate** | Antwortrate (`geantwortet / Gesamt-DB` in %) |
| **DB** | Gesamtgröße der „bereits angeschrieben"-DB |

### Drei-Spalten-Grid

**Posteingang** (links) — die letzten 6 eingehenden Privatnachrichten (eigene werden nicht angezeigt). Antworten auf Bot-PNs werden mit einem `↩ Antwort`-Badge markiert. Nicks + Zeitstempel + 2-Zeilen-Preview.

**Gesendet (Channel)** (mitte) — die letzten 6 Channel-Posts des Bots: Channel-Name, Variant-Nummer, Zeitstempel.

**PN-Versand** (rechts) — die letzten 10 PN-Versuche. Status-Pillen:
- `gesendet` (grün) — durchgegangen
- `gefiltert` (gelb) — `canReceiveMessages=false` oder `CONTACT_FILTER: WRONG_*`. Empfänger lässt uns explizit nicht durch.
- `Fehler` (rot) — echter Fehler (Netzwerk, GraphQL-Validation, etc.)

Ein `↩ antwortete`-Badge erscheint, sobald die angeschriebene Person später antwortet.

### Live-Log

Letzte ~50 Events (gefiltert auf das, was das Dashboard kennt). Farbcodierung nach Prefix: PN/MSG grün, SPAM/TEST blau, WARN/FAIL rot, INIT/DISC/EDIT grau, RESTART gelb.

### Channels

- **Chips** der aktuell ausgewählten Channel-Gruppen mit `×`-Button zum Entfernen.
- **Suchfeld**: tippe los, Live-Suche via Knuddels' eigene `channelGroups(prefix:...)`-Query. Ergebnisse sortiert nach Online-Userzahl. Klick fügt die Gruppe hinzu.
- Sub-Channels (z.B. `Matratzensport 2`, `… 3`) werden automatisch mit-discovered. Nur der **Gruppenname** wird gespeichert.

Bei Speicherung wird sofort ein neuer `do_discover()`-Lauf getriggert — kein 10-Minuten-Warten.

### Channel-Varianten

Beliebig viele Texte (Minimum 2 — sonst gibt's nichts zum Rotieren). Pro Variante:
- **Test-Button** — schickt diese Variante **einmal sofort** in den aktuellen Channel, ohne die Sliding-Window-History oder die Rotation zu beeinflussen. Praktisch um neue Texte gleich zu probieren.
- **×-Entfernen** — disabled wenn nur noch 2 übrig sind.
- **Textarea** mit dem Variant-Text. Gelber Border zeigt unbestätigte Änderungen.

**„+ Variante hinzufügen"** unten fügt eine neue leere hinzu. **Speichern** schreibt nach `messages.json` und wirkt ab dem nächsten SPAM-Tick.

### PN-Vorlage

Eine Textarea. Inhalt wird beim PN-Send an die Empfängerin geschickt. **Leer = PN-Modus aus.**

`${nick}` als Platzhalter wird beim Senden durch den Nick der Empfängerin ersetzt. Beispiel: `"Hey ${nick}, dein Profil ist mir aufgefallen…"`.

### Timing & Limits

Vier Number-Inputs:

| Feld | Default | Bereich | Bedeutung |
|---|---|---|---|
| Channel-Intervall | 120 s | 5–3600 | Wartezeit zwischen Channel-Posts |
| Posteingang-Intervall | 30 s | 5–3600 | Wartezeit zwischen Inbox-Polls |
| PN-Intervall | 30 s | 5–3600 | Wartezeit zwischen PN-Sends |
| Tageslimit PN | 0 | ≥0 | `0` = kein Limit; `>0` = max. PNs pro Kalendertag |

Werden in `bot_config.json` persistiert. Änderungen wirken sofort — `next_X`-Timestamps des Main-Loops werden auf die neuen Werte geclampt.

### PN-DB aufräumen

User-ID rein, **Entfernen** klicken → der User wird aus `processed_users.json` UND `answered_users.json` entfernt. Beim nächsten Mal in einem Channel könnte ihm wieder eine PN geschickt werden. Nützlich wenn du jemanden „freischalten" willst.

User-ID kriegst du z.B. aus den Log-Lines (`PN-Versand`-Spalte zeigt keinen User-ID an — aber du siehst sie im Live-Log: `[HH:MM:SS] PN   <nick>: ...`. Profile-URL in Knuddels enthält die ID auch.)

---

## CLI-Flags

```bash
python knuddels_bot.py [--port PORT] [--no-browser] [--config-dir DIR]
```

| Flag | Default | Effekt |
|---|---|---|
| `--port` | `8080` | Dashboard-Port (falls 8080 belegt) |
| `--no-browser` | aus | Browser nicht automatisch öffnen (z.B. headless) |
| `--config-dir` | Skript-Dir | Alle Persistenz-Files dort: `.env`, `*.json`. Nützlich für mehrere Bot-Instanzen. |

Beispiel: zwei parallele Bots für zwei Accounts auf verschiedenen Ports:

```bash
python knuddels_bot.py --port 8080 --config-dir ~/bots/account_a/
python knuddels_bot.py --port 8081 --config-dir ~/bots/account_b/
```

> **Tipp:** Für einfaches Account-Wechseln ohne zweiten Prozess reicht der **„Account wechseln"-Button** im Dashboard — der Bot merkt sich jeden Account automatisch in `accounts/<nick>/` und lädt beim Wechsel dessen eigene Konfiguration.

---

## Files im Bot-Verzeichnis

Alles per `.gitignore` ausgeschlossen.

### Pro Account (`accounts/<nick>/`)

| File | Inhalt |
|---|---|
| `.env` | `KNUDDELS_USERNAME` + `KNUDDELS_PASSWORD`. **Niemals committen.** |
| `messages.json` | Liste der Channel-Werbevarianten |
| `pn_text.json` | PN-Vorlage |
| `processed_users.json` | DB der angeschriebenen User-IDs (channel-übergreifend) |
| `answered_users.json` | User-IDs die auf eine PN geantwortet haben |
| `channel_groups.json` | Im Dashboard ausgewählte Channel-Gruppen |
| `bot_config.json` | Intervalle + Tageslimit |

### Im Wurzelverzeichnis

| File | Inhalt |
|---|---|
| `current_account.txt` | Nick des zuletzt aktiven Accounts (wird beim Start wiederhergestellt) |
| `state.json` | Dashboard-Snapshot (wird alle paar Sekunden überschrieben) |

Backup: `accounts/<nick>/` als Ganzes kopieren.

Reset: einzelne Files löschen → Bot fängt für diesen Aspekt frisch an. Z.B. `accounts/<nick>/processed_users.json` löschen → DB ist leer, jeder User wird wieder als „neu" behandelt.

---

## Bot-Verhalten im Detail

### Channel-Rotation

Der Bot kann pro Channel-Gruppe nur in einer Instanz gleichzeitig sein. `joinByName("Matratzensport 2")` kickt automatisch aus `Matratzensport 1`. Deswegen **Round-Robin** statt Broadcast.

**Ohne** PN-Modus rotiert der Bot bei jedem SPAM-Tick eins weiter. Bei 3 Channels und 2-Min-Intervall → 6 Min pro Channel.

**Mit** PN-Modus bleibt der Bot in einem Channel bis alle weiblichen, noch nicht angeschriebenen Profile durch sind — dann erst weiter zum nächsten. Während dieser Zeit postet er trotzdem alle 2 Minuten Werbung in den aktuellen Channel.

### Sliding-Window-Anti-Repeat

Adaptive Logik: bei N Varianten dürfen die letzten `min(4, N-1)` nicht wiederholt werden.

- N=2 → strikt alternierend (1, 2, 1, 2, …)
- N=3 → 3er-Zyklus mit zufälligem Pick aus jeweils einer Variante
- N≥5 → klassisches 4er-Sliding-Window mit Random-Choice

### PN-Modus (an/aus)

- **PN-Vorlage leer** → PN-Modus aus, Rotation läuft normal weiter, keine PNs
- **PN-Vorlage gesetzt** → PN-Modus an, Rotation pausiert pro Channel bis Queue durch ist

Pro Channel wird beim ersten Besuch die User-Liste geholt (`getChannel(id).users`), gefiltert auf `gender=FEMALE` und nicht in `processed_users`, das ergibt die Queue. Pro PN-Tick wird dann der erste aus der Queue genommen, sein Profil via `getUserMacroBox(id)` geholt (für `canReceiveMessages` + `conversationId`), und versendet:

1. Wenn `canReceiveMessages=false`: skip, in DB markieren, kein archive
2. Wenn `canReceiveMessages=true` aber Send-Fehler `CONTACT_FILTER`: gefiltert vom Empfänger, in DB markieren, kein archive (die Nachricht wurde serverseitig vor dem Conv-Anlegen abgewiesen)
3. Bei erfolgreichem Send: **`time.sleep(1.5)`** und dann `archiveConversation()`. Die Pause ist nötig wegen einer Race-Condition (siehe Known Limitations).

### Antworten-Tracking

Bei jedem MSG-Watchdog-Tick wird verglichen: ist der Sender der neuesten Nachricht in `processed_users` (= wir haben ihn angeschrieben), aber NICHT in `answered_users`? Wenn ja → **das hier ist eine Antwort**, ID wird in `answered_users.json` aufgenommen, im Dashboard als `↩ antwortete` markiert.

Wenn der Sender **noch nicht** in `processed_users` ist (= jemand schreibt uns ohne dass wir je was an sie geschickt hatten), wird sie in `processed_users` aufgenommen — damit wir sie nicht später aus Versehen anschreiben.

### Tageslimit

Per Default `0` = aus. Wenn gesetzt: Bot vergleicht `pn_count_today` (Kalendertag-Zählung, resettet um 0:00) gegen das Limit. Beim Erreichen loggt er einmalig `Tageslimit erreicht — PN-Versand pausiert bis morgen` und überspringt alle weiteren PN-Ticks bis zum nächsten Kalendertag.

Channel-Spam läuft trotzdem weiter — das Limit gilt nur für PNs.

---

## Troubleshooting

### Bot postet nichts

1. Hast du im Dashboard mind. eine Channel-Gruppe ausgewählt?
2. Hast du im Variants-Editor Inhalt? Default-Platzhalter zählt nicht (Knuddels filtert sie wahrscheinlich, sind unsinnig)
3. Ist der Bot pausiert? (Pause-Button checken)
4. Schau ins Live-Log oder Terminal — gibt's WARN/FAIL?

### „PN-Modus geht nicht"

- PN-Vorlage gespeichert? Leerer Text = aus
- Channel hat keine weiblichen User? (Stats-Leiste: Queue = 0/0)
- Tageslimit erreicht? (Stats-Leiste: Heute = X/Limit)

### Bot fliegt aus dem Channel

Knuddels kickt nach gewissen Verhaltensmustern. Bot ruft `join_by_name` vor jedem Send neu auf, aber wenn die API silent-kickt (kein API-Error trotz fehlendem Channel-Zugriff), ist das eine Knuddels-Eigenheit, die wir nur post-hoc erkennen (Verify schlägt fehl).

Workaround: Sliding-Window-History größer machen (Bot-Code anpassen) und/oder bessere Texte verwenden.

### Knuddels filtert meine Channel-Posts

Du wirst es daran erkennen, dass der Verify-Check „NICHT sichtbar" loggt obwohl du im Channel bist. Knuddels' Content-Filter ist eine Black Box. Dinge die hilfreich sein können:

- Längere Texte sind verdächtiger als kurze
- Wiederholte Pattern triggern den Filter
- Keywords wie „Fickfreundschaft", „PN", Maßangaben „19cm" sind oft Trigger
- Probiere kürzere Varianten mit zufälligen Smiley-Suffixes

### „Login-Loop" / 401

`.env` löschen, Bot neu starten — Wizard fragt Credentials neu. Wenn Knuddels den Account temporär gesperrt hat: erst in der App einloggen und prüfen, ob ein Captcha o.ä. fällig ist.

### Browser öffnet sich nicht (Linux headless)

`--no-browser` mit angeben. Dashboard kannst du per SSH-Tunnel von einem anderen Rechner aus erreichen:

```bash
ssh -L 8080:localhost:8080 server
# dann auf dem lokalen Rechner http://localhost:8080/dashboard.html
```

### Port 8080 belegt

```bash
python knuddels_bot.py --port 9000
```

---

## Architektur (Kurzversion)

Drei Layer:

1. **`knuddelsAPI.py`** + `classes/` — der Wrapper. Ein `KnuddelsAPI(user, pw)`-Aufruf macht Login (logincheck → createSessionToken → activateSessionToken), danach hat jede Methode dasselbe Muster: GraphQL-Request mit Bearer-Auth, Response via `dacite.from_dict` in eine Dataclass parsen.

2. **`knuddels_bot.py`** — der Bot. Ein einziger Prozess mit einer Session und vier Tasks in einer Event-Loop (SPAM/MSG/PN/DISC). Persistenz in `.json`-Files, Live-State in `state.json`. Eingebauter HTTP-Server (`http.server`, kein Flask) für Dashboard + REST-API.

3. **`dashboard.html`** — Vanilla HTML/CSS/JS, kein Build-Step. Pollt `state.json` alle 5s, hat Editor-Sektionen für die Konfigurations-Files, schickt POST-Requests an die `/api/...`-Endpoints.

Datenfluss:
```
Knuddels GraphQL ⇄ KnuddelsAPI (lib) ⇄ knuddels_bot.py ⇄ state.json ← dashboard.html (Polling 5s)
                                          ↑
                                          └─ accounts/<nick>/
                                               ├─ .env (Credentials)
                                               ├─ messages.json, pn_text.json
                                               ├─ channel_groups.json, bot_config.json
                                               └─ processed_users.json, answered_users.json
```

Für detailliertere Architektur (Auth-Flow, GraphQL-Patterns, polymorphic dispatch) siehe [CLAUDE.md](CLAUDE.md).

---

## Known Limitations

### Silent Failures der Knuddels-API

Knuddels' GraphQL antwortet bei vielen Fehler-Konditionen mit `error: null` — also „Erfolg" — obwohl der Side-Effect nicht passiert ist. Beispiele die im Code workarounded sind:

- **Content-Filter im Channel** drops unsere Posts still. Mitigation: separater `recentMessages`-Verify nach jedem Send.
- **Send-then-Archive Race**: archive direkt nach send wird vom delayed Send-Commit überschrieben (`visibility` springt von `ARCHIVED` zurück auf `VISIBLE`). Mitigation: `time.sleep(1.5)` zwischen send und archive.
- **Channel-Kick** wird nicht explizit gemeldet. Mitigation: `join_by_name` vor jedem Send.

### `recentMessages` zeigt nur 3 Einträge

In aktiven Channels (500+ User) verdrängen andere Posts den eigenen aus dem 3er-Fenster sofort. `is_my_message_visible` produziert dort false negatives — das ist eine Verify-Limitierung, kein echter Filter. Im Bot toleriert: false-negative-Logs sind keine Aktion erforderlich.

### Keine offizielle API

Knuddels könnte die Endpoints jederzeit ändern. Alle GraphQL-Queries sind aus dem offiziellen iOS-Client gesnifft. Das `sessionInfo` im Login hardcodet `platform: Native, osInfo: ios 26.2, deviceInfo: iPhone15,4, deviceIdentifier: <UUID>` — wenn das einmal gewechselt wird, muss man neu sniffen.

### Sessions invalidieren sich gegenseitig

Wenn man zwei Bot-Instanzen parallel mit demselben Account startet, kickt eine die andere serverseitig raus → bis zum nächsten Re-Login. Bei Bedarf einen zweiten Knuddels-Account anlegen.

### Kein Rate-Limiting

Der Wrapper hat keine Backoff-Logik. Recursive Pagination (`getMessagesForConversation`) kann viele Requests in Sekunden feuern. Bot-Intervalle sind konservativ konfiguriert, aber wenn du sie zu niedrig setzt → Shadowban-Risiko.

---

## Entwicklung

Keine Tests, kein Linter konfiguriert. Manueller Syntax-Check:

```bash
python -m py_compile knuddels_bot.py
```

Neue API-Methode hinzufügen:

1. Request aus dem offiziellen Client capturen (Charles, mitmproxy, etc.)
2. Methode in `KnuddelsAPI` nach dem Muster der anderen — GraphQL-Query inline halten
3. Wenn die Response polymorphic Content enthalten kann: `config=self.base_config` an `from_dict` mitgeben (Dispatch via `__typename`)
4. Wenn Response-Struktur neu: Dataclass unter `classes/` anlegen

Für eine ausführliche Hands-on-Architektur-Beschreibung mit allen Gotchas siehe [CLAUDE.md](CLAUDE.md) — die war ursprünglich für AI-Coding-Assistants gedacht, ist aber auch für Menschen lesbar und auf dem aktuellen Stand.

---

## Mitwirkende

Wrapper-Library basiert auf [henrydatei/knuddels-api](https://github.com/henrydatei/knuddels-api).

---

## Lizenz

Lizenz folgt der Upstream-Library. Wer den Bot nutzt, ist selbst dafür verantwortlich, sich an Knuddels' AGB und geltendes Recht zu halten.
