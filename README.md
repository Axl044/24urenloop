# 24-urenloop

Een eenvoudige website om een estafette van 24 uur te volgen. Er zijn twee rollen:

- **Teller** (gsm, `/teller`): drukt op één grote knop **WISSEL** telkens de stok wordt doorgegeven. Het scherm toont de huidige loper, de 2 volgende lopers, de tijd sinds de start van het event en de looptijd van de huidige loper.
- **Wachtlijstbeheerder** (laptop, `/beheer`): voegt lopers toe met hun gang, past de volgorde aan (slepen of pijltjes), beheert de gangen, start en stopt het event, corrigeert rondes en bekijkt statistieken of downloadt een CSV.

De app gebruikt enkel Python 3 (standaardbibliotheek) en SQLite, zonder externe dependencies.

## Starten

```bash
cp .env.example .env        # kies twee verschillende codes
docker compose up -d --build
```

Of zonder Docker:

```bash
TELLER_PIN=1234 ADMIN_PIN=geheim987 python3 server.py
```

De app luistert op poort 8080. Zet er een reverse proxy met HTTPS voor (bv. `24urenloop.axlquirijnen.be`) en geef de header `X-Forwarded-Proto: https` mee, zodat de login-cookie `Secure` wordt. Een Caddy-voorbeeld:

```
24urenloop.axlquirijnen.be {
    reverse_proxy 127.0.0.1:8080
}
```

## Offline ter plaatse, alleen-lezen online

Ter plaatse draait alles op een laptop, zonder internet nodig: gsm's en laptop hangen aan hetzelfde wifi-netwerk (bv. een reisrouter). Online komt enkel een **statisch scorebord** zonder schrijfmogelijkheden.

```
gsm (teller) ──wifi──▶ laptop :8080 ──▶ data/public/{index.html, scorebord.json}
tv / beamer  ──wifi──▶ laptop :8080/scorebord          │
                                                       │ sync/push.sh (rsync over Tailscale)
                                                       ▼
                                   thuisserver: Caddy/nginx serveert enkel die 2 bestanden
```

### 1. Laptop

```bash
cp .env.example .env    # codes invullen, COMPOSE_FILE en BACKUP_PATH aanzetten
docker compose up -d --build
```

`docker-compose.laptop.yml` maakt de app bereikbaar op het lokale netwerk (`http://<ip-van-laptop>:8080`), schrijft de backups naar `BACKUP_PATH` (bv. een USB-stick) en schrijft elke 10 s de publieke export naar `data/public/`. Steek de stick in vóór je de container start, anders maakt Docker een gewone map op die plek.

### 2. Lokaal scorebord

`http://<ip-van-laptop>:8080/scorebord` werkt zonder login, voor een tv of beamer. Het toont de huidige loper, de volgende 3, de laatste rondes en de stats per gang, per uur en per loper. Werkt ook als het internet wegvalt.

### 3. Sync naar de thuisserver

De laptop pusht met rsync over ssh, via Tailscale. Op de thuisserver komt dus geen sync-endpoint en geen app, enkel een map met bestanden.

Op de thuisserver, eenmalig:

```bash
sudo useradd -m -s /bin/sh deploy
sudo install -d -o deploy -m 755 /srv/24urenloop-public
```

Zet de publieke sleutel van de laptop in `~deploy/.ssh/authorized_keys`, beperkt tot rsync in die ene map (`rrsync` zit bij rsync):

```
command="rrsync /srv/24urenloop-public",restrict ssh-ed25519 AAAA... laptop-24urenloop
```

Op oudere Debian/Ubuntu staat rrsync in `/usr/share/doc/rsync/scripts/` in plaats van in het `PATH`.

Caddy serveert de map, enkel om te lezen. Dit vervangt de reverse proxy van hierboven: de app zelf staat dan niet meer online.

```
24urenloop.axlquirijnen.be {
    root * /srv/24urenloop-public
    @write not method GET HEAD
    respond @write 405
    header /scorebord.json Cache-Control "no-cache"
    file_server
}
```

Op de laptop (met Tailscale verbonden):

```bash
SYNC_TARGET=deploy@thuisserver: ./sync/push.sh
```

Het script pusht elke 15 s (`SYNC_SECONDS`) en blijft opnieuw proberen als het internet wegvalt. Het scorebord toont hoe oud de gegevens zijn, en waarschuwt als ze ouder zijn dan 2 minuten.

## Verloop

1. De beheerder logt in met `ADMIN_PIN`, maakt de gangen aan en vult de wachtlijst.
2. De teller logt op de gsm in met `TELLER_PIN`.
3. De beheerder drukt op **Start event**. De eerste loper in de wachtlijst vertrekt dan.
4. Bij elke wissel drukt de teller op **WISSEL**. De volgende loper uit de wachtlijst start en de ronde van de vorige wordt opgeslagen.
5. Na 24 uur drukt de beheerder op **Stop event**.

## Betrouwbaarheid

- Elke wissel krijgt op de gsm een uniek id en het tijdstip van de klik. Valt het netwerk weg, dan blijft de wissel in de gsm bewaard (ook als je de pagina herlaadt) en wordt ze opnieuw verstuurd. De server telt ze nooit dubbel, en het moment van de klik telt, niet het moment waarop de wissel aankomt.
- Een dubbele klik binnen `MIN_LAP_SECONDS` (standaard 5 s) wordt geweigerd.
- **Ongedaan maken**: de teller kan de laatste wissel tot `UNDO_SECONDS` (standaard 60 s) terugdraaien, met twee tikken. De loper komt dan terug vooraan in de wachtlijst. De beheerder kan dit altijd.
- Is de wachtlijst toch leeg, dan wordt de wissel bewaard als loper `?`. De beheerder vult de naam daarna in via *Laatste rondes*.
- SQLite draait met WAL en `synchronous=FULL`. Elke 5 minuten komt er een backup in `data/backups/` (de laatste 100 blijven bewaard).
- Elke actie wordt gelogd in de tabel `log`.
- Het scherm van de gsm blijft aan (Wake Lock). Een groene of rode stip toont de verbinding.

## Instellingen (omgevingsvariabelen)

| Variabele | Standaard | |
|---|---|---|
| `TELLER_PIN` / `ADMIN_PIN` | verplicht | inlogcodes, moeten verschillen |
| `PORT` / `HOST` | `8080` / `0.0.0.0` | |
| `DB_PATH` | `data/24urenloop.db` | |
| `MIN_LAP_SECONDS` | `5` | minimale rondetijd (beschermt tegen dubbele klik) |
| `UNDO_SECONDS` | `60` | hoelang de teller kan terugdraaien |
| `BACKUP_MINUTES` / `BACKUP_KEEP` | `5` / `100` | `0` minuten = geen backups |
| `SECRET` | automatisch | sleutel voor cookies (anders bewaard in de db) |
| `BACKUP_DIR` | `<map van DB_PATH>/backups` | |
| `PUBLIC_DIR` | leeg (uit) | map voor de publieke export (`index.html` + `scorebord.json`) |
| `PUBLIC_SECONDS` | `10` | hoe vaak de publieke export ververst |

## Statistieken

Op `/beheer` vind je statistieken per gang, per loper en per uur. Met **Export CSV** download je alle rondes: naam, gang, start, einde, duur en wachttijd. Je kunt ook rechtstreeks de SQLite-database gebruiken (tabel `laps`).

## Tests

```bash
python3 -m unittest -v
```
