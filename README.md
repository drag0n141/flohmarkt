# Flohmarkt – Tischvergabe

Kleine Flask-Webanwendung für die Vergabe von Flohmarkt-Tischen:
Registrierung, Tischauswahl (mit Live-Belegungsstatus) und Bezahlung der
Standgebühr direkt per PayPal. Die Zahlung wird **serverseitig** über die
PayPal Orders API (v2) erstellt und erfasst – der Tisch wird erst nach
bestätigter Zahlung endgültig gebucht.

## Ablauf
1. Besucher wählt einen freien Tisch im Raster.
2. Formular mit Name/E-Mail/Telefon → Tisch wird für 10 Minuten reserviert.
3. PayPal-Zahlung über die eingebetteten Smart Buttons.
4. Nach erfolgreicher Zahlung (serverseitig per Capture bestätigt) gilt der
   Tisch als vergeben.

Nicht bezahlte Reservierungen laufen nach 10 Minuten automatisch ab und der
Tisch wird wieder frei (Konstante `HOLD_MINUTES` in `app.py`).

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env bearbeiten: NUM_TABLES, PRICE, PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_MODE
```

### PayPal-Zugangsdaten
1. Auf https://developer.paypal.com einloggen → "Apps & Credentials".
2. Für Tests: Sandbox-App erstellen → Client ID + Secret in `.env` eintragen,
   `PAYPAL_MODE=sandbox` lassen.
3. Für den echten Flohmarkt: Live-App erstellen (erfordert ein verifiziertes
   PayPal-Business-Konto), Client ID + Secret eintragen, `PAYPAL_MODE=live`.

### Starten (lokal/Test)
```bash
python app.py
```
→ http://localhost:5000

### Produktivbetrieb
Für den echten Einsatz nicht den eingebauten Dev-Server nutzen, sondern z. B.:
```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```
dahinter einen Reverse Proxy mit HTTPS (nginx + Let's Encrypt). **HTTPS ist
für PayPal-Live-Zahlungen zwingend erforderlich.**

## Ermäßigter Preis für interne Mitarbeiter
Läuft über **Gutscheincodes** statt über eine automatische Erkennung – im
Registrierungsformular gibt es ein optionales Feld "Gutscheincode". Ist der
Code gültig und noch nicht (vollständig) eingelöst, gilt `PRICE_INTERNAL`
statt `PRICE_STANDARD`; der tatsächlich zu zahlende Preis wird fest an der
Anmeldung gespeichert.

Verwaltung unter `/admin/vouchers`:
- **Einzelnen Code anlegen** – z. B. ein gemeinsamer Code für alle
  Mitarbeiter mit hoher "Max. Nutzungen"-Zahl.
- **Mehrere Einzel-Codes generieren** – erzeugt zufällige, je einmal
  einlösbare Codes (z. B. für individuelle Verteilung).
- Codes lassen sich deaktivieren oder löschen.

Ein Code wird bei der Registrierung reserviert (wie die Tischreservierung)
und bei Ablauf der 10-Minuten-Frist oder manueller Stornierung im
Admin-Bereich automatisch wieder freigegeben.

## Admin-Bereich
Erreichbar unter `/admin/login` (Passwort aus `ADMIN_PASSWORD` in `.env`).

- **Tischübersicht** (`/admin`): zeigt alle aktiven Anmeldungen mit Name,
  E-Mail, Telefon, Tischnummer, gezahltem Preis, ggf. verwendetem
  Gutscheincode und Status. Über "Freigeben" kann ein Tisch manuell wieder
  freigegeben werden (z. B. bei Stornierung oder No-Show) – ein dabei
  verwendeter Gutscheincode wird automatisch wieder nutzbar.
- **Lageplan verwalten** (`/admin/floorplan`): Bild des Geländes hochladen
  (ersetzt ein vorhandenes Bild) und anschließend jede Tischnummer per Klick
  an die passende Stelle im Bild setzen. Sobald mindestens ein Tisch
  positioniert ist, zeigt die öffentliche Startseite automatisch den
  Lageplan mit farbigen Markern (grün=frei, gelb=reserviert, rot=vergeben)
  statt des einfachen Rasters. Ohne Lageplan bleibt das Raster als Fallback
  aktiv.

Login läuft über eine serverseitige Session (`SECRET_KEY` in `.env` setzen).

## Deployment im Kubernetes-Cluster
Im Ordner `kubernetes/` liegen fertige Manifeste (Namespace, ConfigMap,
ExternalSecret, PVC, Deployment/Service, HTTPRoute) sowie ein `Dockerfile`
und eine GitHub-Actions-Pipeline (`.github/workflows/docker-publish.yml`),
die das Image bei jedem Push nach `ghcr.io/<dein-user>/flohmarkt`
baut und pusht (amd64 + arm64).

**Ablauf:**
1. Repo (inkl. `Dockerfile` und `.github/workflows/`) auf GitHub pushen –
   die Pipeline baut automatisch das Image. Das GHCR-Package danach einmal
   auf "public" stellen (oder ein `imagePullSecret` mit einem GHCR-Token
   anlegen), sonst kann der Cluster es nicht ziehen.
2. In 1Password ein Item `flohmarkt-tische` mit den Feldern
   `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `ADMIN_PASSWORD`,
   `SECRET_KEY` anlegen (passend zu deinem ESO/1Password-Connect-Setup).
3. Die Dateien unter `kubernetes/apps/flohmarkt/flohmarkt-tische/` in dein
   `home-ops`-Repo übernehmen. Anpassen:
   - `app/pvc.yaml`: `storageClassName` an deine Rook-Ceph-StorageClass.
   - `app/httproute.yaml`: `parentRefs` (Name/Namespace deines Envoy
     Gateway) und `hostnames`.
   - `app/externalsecret.yaml`: Name deines `ClusterSecretStore`.
   - `ks.yaml`: Name deiner Flux `GitRepository`-Source.
   - `app/deployment.yaml`: Image-Referenz, falls du kein `latest` willst
     (empfehlenswert: von Renovate auf den Image-Digest pinnen lassen).
4. Committen und pushen – Flux reconciled automatisch.

**Hinweise:**
- SQLite verträgt nur einen gleichzeitigen Schreiber, daher `replicas: 1`
  und `strategy: Recreate` im Deployment – bei einem Rollout gibt es kurz
  Downtime, für diese Größenordnung unkritisch.
- DB-Datei und hochgeladener Lageplan liegen auf **derselben PVC**, aber
  über zwei `subPath`-Mounts getrennt (`/data` bzw.
  `/app/static/uploads`), damit beides den Neustart übersteht.
- `/admin` ist ausschließlich durch das eigene Passwort der App
  geschützt, nicht durch eine cluster-weite ForwardAuth (Tinyauth) – bei
  Bedarf lässt sich das über eine eigene `HTTPRoute`/`SecurityPolicy` nur
  für den `/admin`-Pfad ergänzen.
- Ohne eigenen `Dockerfile`-Build kannst du für einen ersten Test auch
  einfach lokal mit `docker build -t flohmarkt-tische .` bauen und in
  deine eigene Registry pushen.

## Sicherheit
Bereits eingebaut:
- **CSRF-Schutz** (Flask-WTF) für alle Admin-Formulare und -AJAX-Calls; die
  öffentliche JSON-API (`/api/...`) ist bewusst ausgenommen, da sie ohnehin
  nicht cookie-basiert authentifiziert ist.
- **Rate-Limiting** (Flask-Limiter) auf Login (`10/Minute`) und den
  öffentlichen Endpunkten (`/api/register`, `/api/check-voucher` etc.,
  `20–30/Minute`) gegen Brute-Force auf Passwort bzw. Gutscheincodes.
- **Zeitkonstanter Passwortvergleich** (`hmac.compare_digest`) beim
  Admin-Login.
- **Echte Bildvalidierung** (Pillow) beim Lageplan-Upload statt reiner
  Endungsprüfung, plus 8 MB Upload-Limit.
- **Sichere Session-Cookies** (HttpOnly, SameSite=Lax, Secure – über
  `SESSION_COOKIE_SECURE` in `.env` für lokale HTTP-Tests abschaltbar).
- **Security-Header** (CSP ohne `unsafe-inline` für Skripte, X-Frame-Options,
  X-Content-Type-Options, Referrer-Policy) via `after_request`.
- `.gitignore` verhindert, dass `.env`, die SQLite-DB oder hochgeladene
  Lagepläne versehentlich committet werden.

Bewusst nicht eingebaut (für diese Größenordnung meist nicht nötig, aber
gut zu wissen):
- Kein Login-Lockout nach X Fehlversuchen (nur Rate-Limit) – bei Bedarf
  z. B. über Flask-Limiter mit persistentem Storage (Redis) ausbaubar.
- Rate-Limiting nutzt In-Memory-Storage – passend für `replicas: 1`, aber
  nicht clusterweit synchron, falls doch mehrere Instanzen laufen sollten.
- Kein Audit-Log für Admin-Aktionen.

## Anpassungen
- Tischanzahl/Preis/Währung: über `.env` oder direkt am Kopf von `app.py`.
- Datenbank ist eine einzelne SQLite-Datei (`flohmarkt.db`), wird beim
  ersten Start automatisch angelegt.
- Für eine Bestätigungs-E-Mail nach Zahlung: in `api_capture_order()` in
  `app.py` einen Mailversand ergänzen (z. B. mit `smtplib` oder einem
  Mail-API-Dienst).

## Bekannte Grenzen
- Race Conditions bei gleichzeitiger Auswahl desselben Tisches werden über
  den DB-Status abgefangen (`409`-Fehler, Grid/Lageplan wird neu geladen).
- Der Admin-Login ist ein einzelnes gemeinsames Passwort (kein
  Mehrbenutzer-System mit eigenen Accounts).
- Für eine Bestätigungs-E-Mail nach Zahlung: in `api_capture_order()` in
  `app.py` einen Mailversand ergänzen (z. B. mit `smtplib` oder einem
  Mail-API-Dienst).
