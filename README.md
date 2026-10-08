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

## Statistieken

Op `/beheer` vind je statistieken per gang, per loper en per uur. Met **Export CSV** download je alle rondes: naam, gang, start, einde, duur en wachttijd. Je kunt ook rechtstreeks de SQLite-database gebruiken (tabel `laps`).

## Tests

```bash
python3 -m unittest -v
```
