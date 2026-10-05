# CBI Mobile API — v1

JSON API consumed by the CBI mobile app (`PBI-app/new_app`, Flutter).
Served by the CBI Django platform (same container as the web portal, e.g.
`http://10.10.10.53:8222`). Implementation lives in the `mobile` Django app.

## Conventions

- Base path: `/mobile/v1/`. Every path below is relative to it.
- Requests and responses are `application/json; charset=utf-8`.
- Authenticated endpoints need `Authorization: Bearer <token>` (from `auth/login/`).
  No cookies, no CSRF token.
- Errors always look like `{"detail": "<message fr>", "code": "<machine_code>"}`.
  Codes: `auth_required`, `session_expired`, `account_inactive`,
  `invalid_credentials`, `identifier_unknown`, `throttled`, `bad_request`,
  `not_found`, `forbidden`, `method_not_allowed`, `server_unavailable`.
- `401` means "log in again" (the app wipes the stored session). `403` means the
  user is authenticated but not allowed.
- Dates are ISO 8601 with timezone (`2026-09-29T08:15:00+01:00`).
- IDs are integers.

## Sessions

- A session lasts `MOBILE_SESSION_DAYS` (default 30) **since last use**, i.e. it
  slides forward while the app is used. Logout revokes it immediately.
- The server never stores the user's password. The app keeps it in the Android
  Keystore-backed secure storage, only to answer PBIRS NTLM challenges for the
  hosts listed in `catalog.servers`.

---

## Public

### `GET config/`
App bootstrap configuration (no auth). Used for the force-update dialog and the
"Aide" / "À propos" screens.

```json
{
  "min_version": "3.0.0",
  "latest_version": "3.0.0",
  "download_url": "https://…/cbi.apk",
  "contact": {"email": "cbi@groupe-hasnaoui.com", "phone": "3004", "website": "https://cbi.groupe-hasnaoui.com"},
  "notification_poll_seconds": 60,
  "server_time": "2026-09-29T08:15:00+01:00"
}
```
The app must show the blocking "Mise à jour requise" dialog when its version is
lower than `min_version`.

### `POST auth/login/`
Body: `{"username": "H0017549" | "prenom.nom@groupe-hasnaoui.com" | "GSH\\H0017549", "password": "…", "device": "Samsung SM-A546B", "app_version": "3.0.0"}`
(`device` and `app_version` are optional).

The identifier is resolved to the AD `sAMAccountName` and checked with an NTLM
bind (the password is never sent to AD in clear text). E-mail addresses are
resolved through the synced user directory.

`200`:
```json
{
  "token": "…",
  "expires_at": "2026-10-29T08:15:00+01:00",
  "user": { …same as GET me/… },
  "credentials": {"domain": "GSH", "username": "H0017549"}
}
```
`credentials` tells the app which `DOMAIN\username` to use for PBIRS NTLM.

Errors: `400 bad_request`, `401 invalid_credentials`, `401 identifier_unknown`
(e-mail not found in the synced directory), `403 account_inactive`,
`429 throttled` (with `Retry-After`). Accounts that only exist locally (not in AD)
fall back to Django authentication, like the web login.

---

## Authenticated

### `POST auth/logout/`
Revokes the current session. `200 {"ok": true}`.

### `GET me/`
```json
{
  "id": 42,
  "username": "H0017549",
  "name": "Mohammed Bouhariz",
  "initials": "MB",
  "email": "…",
  "ad2000": "H0017549",
  "role": "user",
  "is_admin": false,
  "direction": "Direction Système d'Information",
  "pole": "Pôle Services",
  "company": "GSH",
  "description": "Direction Système d'Information · GSH",
  "photo_url": "/mobile/v1/me/photo/" | null,
  "avatar_color": "#358BA4",
  "permissions": {"consolide": true, "pole": true, "direction": false, "module": true, "anomalie": false, "biblio": true}
}
```

### `GET me/photo/` · `GET users/<user_id>/photo/`
The profile image bytes (`image/*`), `404` when there is none. Needs the Bearer
header like every other endpoint. Another user's photo is admin-only (`403`).

### `GET catalog/`
Everything the home, tabs, report lists and favorites screens need, in one call.
Supports `If-None-Match` (returns `304` when nothing changed).

```json
{
  "generated_at": "…",
  "servers": [{"id": 1, "name": "PBIRS principal", "base_url": "http://10.20.10.63", "host": "10.20.10.63", "scheme": "http"}],
  "sections": [
    {
      "key": "consolide", "title": "Consolidé", "layout": "row",
      "groups": [
        {"key": "consolide", "name": "Consolidé", "code": "CONSOLIDE",
         "tabs": [{"key": "direction:7", "name": "Direction Finance et Comptabilité", "code": "DFC", "report_ids": [12, 15]}]}
      ]
    },
    {"key": "pole", "title": "Pôle", "layout": "row", "groups": [{"key": "pole:3", "name": "Pôle Production", "code": "PP", "tabs": [ … ]}]},
    {"key": "societe", "title": "Société", "layout": "grid", "groups": [{"key": "societe:9", "name": "MDM", "code": "MDM", "parent": "Pôle Production", "logo_url": "/mobile/v1/metadata/9/logo/?v=societe-3f2a9c1b7d4e", "tabs": [ … ]}]},
    {"key": "direction", "title": "Direction", "layout": "row", "groups": [ … ]},
    {"key": "module", "title": "Modules", "layout": "row", "groups": [ … ]},
    {"key": "anomalie", "title": "Anomalies", "layout": "row", "groups": [ … ]},
    {"key": "biblio", "title": "Bibliothèque", "layout": "row", "groups": [ … ]}
  ],
  "reports": {
    "12": {"id": 12, "name": "Encaissement Clients", "description": "", "location": "Consolidé / DFC",
           "server_id": 1, "embed_url": "http://10.20.10.63/Reports/powerbi/Consolid%C3%A9/…?rs:embed=true",
           "phone": {"id": 31, "server_id": 1, "embed_url": "http://10.20.10.63/Reports/powerbi/…%20(t%C3%A9l%C3%A9phone)?rs:embed=true"} | null,
           "modified_at": "…", "favorite": true}
  },
  "favorite_ids": [12],
  "unread_notification_count": 3
}
```

Rules:
- Only sections the user may see and that contain at least one report are
  returned, in the order above. Empty groups and tabs are omitted.
- `consolide`: one group; its tabs are the directions. The home row shows the
  **tabs** as cards (legacy behaviour). Tapping a card opens the tab screen
  positioned on that tab.
- Other sections: the home row/grid shows the **groups** as cards. Tapping one
  opens its tabs (one tab = no tab bar).
- `pole` and `direction` groups only hold reports that are not tagged with a
  société. Société-tagged reports live under `societe`, exactly like the web.
- `direction` only lists reports that no earlier section already shows (the web's
  direction view overlaps consolidé/pôle), so users who only have the direction
  view still reach everything.
- Tab `name` is a direction (or `Général` for untagged reports). `code` is a short
  upper-case code the app can map to an image asset: initials without French
  stop-words ("Direction Finance et Comptabilité" → `DFC`), or the word itself
  for one-word names (`MDM`, `GENERAL`).
- `layout` is a hint: `row` = horizontal list, `grid` = 2-row horizontal grid.
- `reports` holds every report referenced by any tab, keyed by id (as a string).
- `server_id` points into `servers`. The WebView may only send credentials to
  those hosts.
- `logo_url` (pôle and société groups, only when an admin uploaded one on
  `/powerbi/servers/`): the card shows the logo with the name underneath. The URL
  changes whenever the logo changes, so it can be cached forever.
- `phone`: the portrait "phone" edition an admin linked on the report page
  (PBIRS cannot render a report's mobile layout in a browser). The app opens it in
  portrait and the full report in landscape / full screen. `null` when there is
  none or the user may not open it. Phone editions are never listed on their own.

### `GET metadata/<option_id>/logo/`
Logo bytes for a pôle/société (`image/png|jpeg|webp`), Bearer header required,
`Cache-Control: immutable`. `404` when there is none.

### `GET reports/<id>/`
One entry of `catalog.reports`. `404` when it doesn't exist or isn't visible.

### `POST reports/<id>/open/`
Call right before showing the WebView. Records the consultation (history).
`503 server_unavailable` when no PBIRS server is configured.
```json
{"view_id": 991, "embed_url": "http://10.20.10.63/Reports/powerbi/…?rs:embed=true", "server": { …servers item… }, "report": { …report… }}
```

### `GET reports/<id>/mobile-layout/`
The report's Power BI **phone layout** (designed in Power BI Desktop → Mobile
layout), extracted by the backend from the .pbix (downloaded from PBIRS with the
service account; cached, re-read when the report changes on PBIRS). No data,
only placement: per page (section name), the portrait canvas and each visual's
phone position plus its phone-only formatting (`Report/MobileState`).
```json
{"available": true, "version": 1, "pages": {"ReportSection": {"display_name": "CA GLOBAL", "width": 324, "height": 1514,
  "visuals": {"b7b6a95e2ea9445b6096": {"x": 10, "y": 55, "z": 3000, "width": 144, "height": 100, "objects": {"labels": [ … ]}}}}}}
```
`{"available": false, "pages": {}}` when the report has no phone layout or PBIRS
is unreachable (the app then shows the desktop layout). Pages missing from
`pages` have no phone layout. The app injects this into the report page, where
`new_app/assets/js/pbi_mobile_layout.js` rewrites the layout PBIRS sends to its
renderer.

### `POST reports/<id>/close/`
Body `{"view_id": 991, "duration_seconds": 184}`. Records how long the report was
viewed. `200 {"ok": true}`. Unknown/foreign `view_id` → `404`.

### `GET favorites/`
`{"favorites": [ …report…, sorted by location then name ]}`

### `PUT favorites/<id>/` · `DELETE favorites/<id>/`
Add / remove. `200 {"id": 12, "favorite": true|false}`.

### `GET notifications/?after=<id>&limit=50`
Newest first. With `after`, only notifications with a higher id (used by the
background poller to raise local notifications).
```json
{
  "notifications": [{"id": 8, "title": "Accès", "kind": "access", "message": "…", "created_at": "…", "is_read": false, "is_new": true}],
  "unread_count": 3,
  "latest_id": 8
}
```
`kind` is `access` | `report` | `info`. `is_new` = created during the last 7 days
("Nouveau" vs "Déjà vu" sections).

### `GET notifications/unread-count/`
`{"unread_count": 3, "latest_id": 8}`. Cheap endpoint for the badge poller.

### `POST notifications/<id>/read/` · `POST notifications/read-all/` · `DELETE notifications/<id>/`
`200 {"ok": true, "unread_count": 2}`.

### `GET history/?days=30`
The current user's report consultations.
```json
{"history": [{"id": 991, "report_id": 12, "report_name": "Encaissement Clients", "location": "Consolidé / DFC",
              "opened_at": "…", "duration_seconds": 184, "source": "mobile"}]}
```

### `GET history/users/?q=&company=&limit=50&offset=0`  (admins only)
Users ordered by last consultation (legacy "Historique" screen).
```json
{"count": 120, "results": [{"user": {"id": 42, "name": "…", "initials": "MB", "description": "…", "company": "GSH", "photo_url": "/mobile/v1/users/42/photo/", "avatar_color": "#358BA4"},
                             "last": { …history item… }}]}
```
Non-admins get `403`.

### `GET history/users/<user_id>/?days=30`
Detailed history for one user (admins, or the user themselves).
`{"user": { … }, "history": [ …history items… ]}`

### `GET tickets/` · `POST tickets/`
Support requests (same tickets as the web portal). Non-admins see their own.
POST body: `{"title": "…", "description": "…", "ticket_type": "access|bug|dashboard|refresh|other", "priority": "low|medium|high"}`
```json
{"tickets": [{"id": 5, "title": "…", "description": "…", "ticket_type": "access", "ticket_type_label": "Demande d'accès",
              "priority": "medium", "status": "open", "status_label": "Ouvert", "created_at": "…", "updated_at": "…",
              "messages_count": 2}]}
```
POST returns `201` with the ticket.

### `GET tickets/<id>/`
Ticket plus `"messages": [{"id": 1, "sender": "…", "is_mine": true, "content": "…", "created_at": "…"}]`.

### `POST tickets/<id>/messages/`
Body `{"content": "…"}` → `201` with the message.
