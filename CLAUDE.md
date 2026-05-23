# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python wrapper around the unofficial GraphQL API of knuddels.de (German chat site). Single-class library (`KnuddelsAPI` in [knuddelsAPI.py](knuddelsAPI.py)) plus a `classes/` package of dataclasses that mirror the GraphQL response shapes. Auth is interactive (username + password) — no API key exists. The work is incomplete per commit history: not every endpoint is wrapped, and parsing for some response types is still being filled in.

## Commands

There is no build, lint, or test setup — this is a plain Python package run directly.

```powershell
# Run the example (requires .env with KNUDDELS_USERNAME and KNUDDELS_PASSWORD)
python count_messages.py
```

Dependencies are not pinned anywhere; install ad hoc:

```powershell
pip install requests dacite python-decouple tqdm
```

(`tqdm` and `python-decouple` are only needed by the example, not the library itself.)

## Architecture

### Auth flow (`KnuddelsAPI.__post_init__` → `login`)

Three sequential calls — all three must succeed before any other method works:

1. `logincheck()` — POST form data to `https://www.knuddels.de/logincheck.html`, returns a JWT.
2. `createSessionToken(jwt)` — GraphQL `CreateSessionToken` mutation, returns a session token. The `sessionInfo` payload (clientVersion, platform iOS, device iPhone15,4, deviceIdentifier UUID) is hardcoded — it identifies this client to Knuddels and should not be changed casually.
3. `activateSessionToken(sessionToken)` — GraphQL `ActivateSession`, no return value but required.

All subsequent calls send `Authorization: Bearer <sessionToken>` to `https://api-de.knuddels.de/mono/graphql`.

### Per-method pattern

Every method on `KnuddelsAPI` is the same boilerplate:

```python
headers = {"authorization": "Bearer "+self.sessionToken, "content-type": "application/json"}
params  = {"operationName": "...", "variables": {...}, "query": "...GraphQL query string..."}
req = requests.post('https://api-de.knuddels.de/mono/graphql', data=json.dumps(params), headers=headers)
req.raise_for_status()
return from_dict(data_class=SomeClass, data=req.json()['data'][...][...])
```

The `query` strings are full GraphQL documents copied from intercepted client traffic (note the `__typename` selections — those are load-bearing for polymorphic parsing, see below). When adding a new endpoint, the easiest path is to capture the request from the official client and paste the query verbatim.

### Dataclass parsing (`dacite`)

Responses are deserialized with `dacite.from_dict(data_class=X, data=...)`. The dataclasses in `classes/` use `dataclasses.field(init=False, default=None)` heavily so GraphQL can omit fields without breaking parsing. When you add a new GraphQL field to a query, you generally need to add a matching field to the dataclass with `init=False, default=None`.

### Polymorphic message content (`__typename` discriminator)

`ConversationMessageContent` is a GraphQL union — the concrete type comes back in the `__typename` field. `KnuddelsAPI.parse_message_content` is registered as a `dacite` type hook on the `MessageContent` Union (see `__post_init__` building `self.base_config`). It dispatches on `__typename` to the matching dataclass in [classes/MessageContent.py](classes/MessageContent.py).

**Important:** when adding support for a new message content type, both pieces must be updated:
1. Add the dataclass to [classes/MessageContent.py](classes/MessageContent.py) and to the `MessageContent` Union at the bottom of that file.
2. Add the mapping entry inside `parse_message_content` in [knuddelsAPI.py](knuddelsAPI.py). Unknown `__typename` values print a dump and return `None` — that's how you'll notice missing coverage.

The `base_config` is only passed to `from_dict` calls that may contain polymorphic content (notably `getConversations` and `getMessagesForConversation`). Plain calls that return a single concrete type don't need it.

### Pagination via recursion

`getConversations` and `getMessagesForConversation` recurse on `hasMore`, passing the oldest item's timestamp/id as the `before` cursor. `getMessagesForConversation` has a hard `recursionDepth` cap of 10 (so ~500 messages max per conversation by default) — bump this if you need deeper history.

## Adding a new API method

1. Capture the GraphQL request from the official client (or guess from existing patterns).
2. Add a method to `KnuddelsAPI` following the boilerplate above. Keep the GraphQL query string inline — that's the convention in this file.
3. If the response shape isn't already covered, add a new dataclass under `classes/` and import it at the top of [knuddelsAPI.py](knuddelsAPI.py).
4. If the response can contain `ConversationMessageContent`, pass `config=self.base_config` to `from_dict` so polymorphic dispatch fires.

## Gotchas

- All GraphQL queries hardcode iOS client metadata. The Knuddels backend may behave differently if you change `platform`/`osInfo`/`deviceInfo` — leave them alone unless deliberately experimenting.
- No rate limiting or backoff is implemented. Recursive paginators (`getConversations`, `getMessagesForConversation`) can fire many requests fast.
- Session tokens expire; there is no auto-refresh. A long-running script will eventually 401 — the caller has to re-instantiate `KnuddelsAPI`.
- `.env` is gitignored. The example uses `python-decouple`'s `config()` to load it; if you write a new script, follow the same pattern rather than reading env vars directly.

---

## Bot project (`knuddels_bot.py` + `dashboard.html`)

A long-running bot built on top of the wrapper, plus a browser dashboard for live monitoring and configuration. Single process, single session, one event loop. Started with `python knuddels_bot.py`; dashboard at <http://localhost:8080/dashboard.html>.

### Tasks in the event loop

| Task | Interval | Function | What it does |
|------|----------|----------|--------------|
| SPAM | 120s | `do_spam` | join current channel by name (with `confirmed=true`, Ü18-Channels brauchen das), send next channel-ad variant, verify, advance rotation when PN-queue done |
| MSG | 30s | `do_watch_msgs` | poll conversations, append new incoming messages (not from-me) to `inbox_log` |
| PN | 30s | `do_pn` | if `pn_text` non-empty: send PN to next eligible female user in current channel, archives the resulting conversation right after to keep inbox tidy (Knuddels un-archives auf eingehende Antwort) |
| DISC | 600s | `do_discover` | für jede konfigurierte Parent-Gruppe `channelGroups(prefix=name)` anfragen und alle Treffer akzeptieren, deren Name == parent ODER parent + „ <Zahl>" ist. Damit fangen wir beide Knuddels-Modellierungen ab: eine Gruppe mit vielen Channels (z.B. „Matratzensport") UND mehrere sibling-Groups (z.B. „Flirt", „Flirt 2", „Flirt 3"). Liste wird im Dashboard via Such-UI gepflegt; die Suche zeigt nur den Parent — Sub-Instanzen werden in `search_channel_groups()` collapsed. Bei leerer Liste idlet der Bot still. |

Plus dashboard endpoints (HTTP-Thread):
- `GET  /state.json`          — Snapshot von inbox/sent/pn/events + stats + bot_paused + config
- `GET  /api/messages`        — aktuelle Channel-Varianten
- `POST /api/messages`        — Save edit-Liste (Länge ≥ MIN_VARIANTS) → `messages.json` + in-memory
- `GET  /api/pn-text`         — aktuelle PN-Vorlage (leer = PN-Modus aus); unterstützt `${nick}` als Platzhalter
- `POST /api/pn-text`         — Save PN-Vorlage → `pn_text.json`
- `GET  /api/channel-groups`  — aktuelle Channel-Auswahl
- `POST /api/channel-groups`  — Save Auswahl → `channel_groups.json` + sofort `do_discover()`
- `GET  /api/channel-search?q=<prefix>` — Knuddels-Suche via `channelGroups(prefix)`
- `GET  /api/bot-config`      — Intervalle + daily_pn_limit
- `POST /api/bot-config`      — Save Intervalle/Limit → `bot_config.json` (validiert 5..3600s)
- `POST /api/pause`           — Bot pausieren (SPAM + PN aus, MSG + DISC laufen weiter)
- `POST /api/resume`          — Bot fortsetzen
- `POST /api/forget-user`     — Body `{"user_id": "..."}`: User aus processed + answered DB entfernen
- `POST /api/test-send`       — Body `{"variant": <idx>}`: sofortiger Test-Send einer Variante in den aktuell rotierten Channel (kein History/Rotation-Effekt)
- `POST /api/restart`         — set `restart_event`; main loop does `do_soft_restart` (re-login, clear runtime state, keep PN-DB)

### Persistence files

| File | Content |
|------|---------|
| `messages.json`         | Liste der Channel-Werbevarianten (beliebig viele, min. `MIN_VARIANTS`=2; editable im Dashboard mit Add/Remove pro Variante) |
| `pn_text.json`          | Single PN template; empty string ⇒ PN-mode disabled |
| `processed_users.json`  | `{"ids": [...]}` — global, channel-übergreifende DB der bereits angeschriebenen User. Atomar (tmp + `os.replace`). Wird beim Bot-Restart nicht geleert (das wäre der Sinn). |
| `channel_groups.json`   | `{"names": [...]}` — vom User im Dashboard ausgewählte Channel-Gruppen. Leer ⇒ Bot idlet. |
| `bot_config.json`       | Intervalle (spam/msg/pn) + daily_pn_limit. Werte zwischen `MIN_INTERVAL_SEC` und `MAX_INTERVAL_SEC`. |
| `answered_users.json`   | `{"ids": [...]}` — User-IDs die auf eine unserer PNs geantwortet haben (für Antwort-Rate-Stat). |
| `state.json`            | Dashboard-Snapshot. Wird bei jedem state-relevanten Event atomar neu geschrieben. |
| `.env`                  | `KNUDDELS_USERNAME` und `KNUDDELS_PASSWORD` (gitignored) |

### Key invariants & gotchas

- **Knuddels kickt nach Idle.** Jedes `do_spam` ruft `join_by_name` mit `confirmed: true` auf, sonst landen die Posts im Off (Channel-Membership ist serverseitig flüchtig). Beim ersten Kick haben wir das durch `'NoneType' object is not subscriptable` in `getChannel` bemerkt — die Wrapper-Funktion crasht weil GraphQL bei nicht-Mitgliedschaft `data.channel.channel = null` liefert (siehe Memory `graphql_silent_errors.md`).
- **`recentMessages` zeigt nur 3 Nachrichten.** In aktiven Channels (500+ User) verdrängen andere Posts den eigenen aus dem Verify-Fenster sofort. `is_my_message_visible` produziert dort false negatives — keine echte Filter-Erkennung, nur eine Verify-Limitierung.
- **Knuddels lässt einen pro Channel-Gruppe nur in einer Instanz gleichzeitig.** `joinByName("Matratzensport 2")` kickt automatisch aus `Matratzensport 1`. Deswegen rotiert der Bot, statt zu broadcasten — und die Rotation pausiert bei aktivem PN-Modus auf einem Channel, bis dessen weibliche Queue durch ist.
- **Adaptive Sliding-Window-History** über die letzten Spam-Sends verhindert direkte Wiederholungen. Block-Count = `min(HISTORY_SIZE, len(MESSAGES)-1)`, so dass es immer ≥1 Kandidat gibt UND nie zwei gleiche hintereinander, auch bei nur 2 Varianten (dann strikt alternierend). Bei mehr Varianten greift das normale 4er-Window.
- **`error: null` ist nicht „durchgekommen".** Knuddels filtert Werbe-Content stillschweigend (gleicher Pattern wie bei archiveConversation, siehe Memory). `SendMessageMutationError`-Enum hat nur `INTERNAL_ERROR` und `CHANNEL_NOT_FOUND` — alles andere (Inhaltsfilter, Shadowban, Channel-Kick) kommt als „Success" zurück. Wir mussten verify via separater recentMessages-Query nachbauen.
- **GraphQL hat Subscriptions** (`wss://api-de.knuddels.de/mono/subscriptions`, Subprotokoll `graphql-transport-ws`), aber die Idee wurde verworfen — Polling alle 30s reicht und ist deutlich simpler. Falls man push-basiert umbauen will: Endpoint via `__schema { subscriptionType { fields } }` introspecten, `messengerEvent` ist die relevante Subscription (Interface mit 15 Implementierungen, z.B. `MessengerMessageReceived`).
- **Send → Archive Race**: nach `sendMessage(conversationId, ...)` braucht's ≥1s Pause bevor `archiveConversation(conversationId)` zuverlässig greift. Ohne Pause antworten beide Mutationen `error: null`, aber Knuddels' delayed Send-Commit schiebt `visibility` nach dem Archive von `ARCHIVED` zurück auf `VISIBLE`. Bot wartet `time.sleep(1.5)`.

### Dashboard

`dashboard.html` ist Vanilla-HTML/CSS/JS, kein Build-Step. Drei-Spalten-Layout (Posteingang | Channel-Gesendet | PN-Versand), darunter Stats-Leiste und Editor-Module für Channel-Varianten und PN-Vorlage. Pollt `state.json` alle 5s. Dark-OLED-Palette (`#020617` bg), Fira Sans + Fira Code via Google Fonts CDN.

### Erstes Setup

1. `python knuddels_bot.py` starten — beim ersten Start fragt der Wizard interaktiv nach Knuddels-Nick + Passwort und legt `.env` an.
2. Dashboard öffnen (<http://localhost:8080/dashboard.html>), oben „Channels" — Such-Feld benutzen, Channel-Gruppen anklicken → werden in den Verteiler aufgenommen (Sub-Instanzen automatisch beim nächsten DISC-Tick).
3. Im Editor darunter die 10 Channel-Varianten und ggf. die PN-Vorlage durch eigene Texte ersetzen + Speichern. Ab dem nächsten SPAM-/PN-Tick gelten die neuen.
4. PN-Modul ist standardmäßig AUS (leeres PN-Feld). Erst wenn Text gespeichert wird, schaltet der Bot auf den Modus „Channel-Stay + PN-Queue abarbeiten" um.
5. Solange keine Channel-Gruppe gewählt ist (Step 2 übersprungen), idlet der Bot ohne zu posten.
