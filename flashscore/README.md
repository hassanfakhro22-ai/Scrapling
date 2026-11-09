# Flashscore.de – Dauerhafte Fußballdatenerfassung, Änderungshistorie & Multi-Format-Export

Kontinuierlich laufendes Python-System auf Basis von **Scrapling (`v0.5.1`)** und **SQLite (WAL-Modus)** zur permanenten Überwachung, Extraktion, historischen Speicherung und lokalen Exportierung sämtlicher Fußballdaten von `https://www.flashscore.de` für alle 10 definierten europäischen Profiligen.

> **Abgrenzung gemäß Vorgabe:** Dieses Paket realisiert ausschließlich die dauerhafte Fußballdatenerfassung, historische Speicherung und Exportfunktion. Es enthält **keine** GUI/Web-App, **keine** Spielprognosen und **keinen** Wettalgorithmus; sämtliche Buchmacher-/Quotenblöcke werden bereits im Parser ausgefiltert.

---

## 1. Abgedeckte Ligen (10 Wettbewerbe, 202 Vereine)

| Land | Liga | Interner Schlüssel | Lokaler Zielordner |
|---|---|---|---|
| Deutschland | 1. Bundesliga | `de_bundesliga` | `Flashscore_Daten/Deutschland/Bundesliga` |
| Deutschland | 2. Bundesliga | `de_2_bundesliga` | `Flashscore_Daten/Deutschland/2_Bundesliga` |
| Deutschland | 3. Liga | `de_3_liga` | `Flashscore_Daten/Deutschland/3_Liga` |
| England | Premier League | `en_premier_league` | `Flashscore_Daten/England/Premier_League` |
| England | Championship (2. Liga) | `en_championship` | `Flashscore_Daten/England/Championship` |
| England | League One (3. Liga) | `en_league_one` | `Flashscore_Daten/England/League_One` |
| Frankreich | Ligue 1 | `fr_ligue_1` | `Flashscore_Daten/Frankreich/Ligue_1` |
| Frankreich | Ligue 2 | `fr_ligue_2` | `Flashscore_Daten/Frankreich/Ligue_2` |
| Spanien | LaLiga (1. Liga) | `es_laliga` | `Flashscore_Daten/Spanien/LaLiga` |
| Spanien | Segunda División / La Liga2 | `es_segunda_division` | `Flashscore_Daten/Spanien/Segunda_Division` |

---

## 2. Abfrage-Intervalle & Fehlerresilienz

- **Aktuelle Spielstände & Live-Ereignisse:** alle **15 Sekunden** (`1H`, `HT`, `2H`, `ET`, `FT`, Tore, Vorlagen, Elfmeter, Karten, Auswechslungen, VAR)
- **Live-Statistiken:** alle **30 Sekunden** (Ballbesitz, Schüsse gesamt/aufs Tor/vorbei/geblockt, Großchancen, xG, Eckbälle, Abseits, Fouls, Karten, Pässe + Passquote, Zweikämpfe, Tacklings, Paraden)
- **Aufstellungen:** vor Spielbeginn (90-Minuten-Vorlauffenster) sowie bei jeder Änderung (`Voraussichtliche Aufstellungen` -> bestätigte `Startaufstellungen` -> kurzfristige Startelf-Änderungen -> Wechsel)
- **Verletzungen, Sperren & Fragliche Spieler:** alle **15 Minuten** (`verletzt`, `gesperrt`, `krank`, `fraglich`, `sicher_ausfallend` inkl. Grund & voraussichtlichem Rückkehrdatum)
- **Tabellen (`overall`, `home`, `away`, `form_5`, `form_10`):** sofortige Aktualisierung **nach jedem beendeten Spiel** sowie beim Liga-Sync
- **Spielpläne:** regelmäßige Synchronisation aller 10 Ligen mit Rate-Limiter
- **Stabilität:** Verbindungsabbruch-Erkennung (`CONNECTION_LOST` / `CONNECTION_RESTORED`), exponentieller Backoff, rotierende Fehler-Logdateien (`flashscore_system.log`, `flashscore_errors.log`) und automatischer Neustart (`DAEMON_AUTO_RESTART`) ohne Datenverlust

---

## 3. Lokale Ordnerstruktur

Standardmäßig wird `Flashscore_Daten/` verwendet (über `--data-dir` oder `FLASHSCORE_DATA_DIR` frei wählbar):

```text
Flashscore_Daten/
├── Deutschland/{Bundesliga, 2_Bundesliga, 3_Liga}/
├── England/{Premier_League, Championship, League_One}/
├── Frankreich/{Ligue_1, Ligue_2}/
├── Spanien/{LaLiga, Segunda_Division}/
├── Spieler/
├── Verletzungen/
├── Live_Spiele/
├── Spielstatistiken/
├── Aufstellungen/
├── Schiedsrichter/
├── Stadien/
├── Historische_Daten/
├── Backups/
└── Gesamtdatenbank.sqlite
```

Alle historischen Änderungen bleiben dauerhaft in den Append-Only-Tabellen (`change_history`, `standings_history`, `score_history`, `stats_history`, `lineup_history`, `injury_history`, `system_events`) erhalten und werden **niemals überschrieben**.

---

## 4. Steuerung & Export per Kommandozeile

```bash
# 1. Datenbank & Ordnerstruktur initialisieren (optional mit verifiziertem 10-Ligen-Bestand)
.venv/bin/python -m flashscore init --seed

# 2. Dauerbetrieb im Hintergrund starten / Status prüfen / stoppen
.venv/bin/python -m flashscore daemon start
.venv/bin/python -m flashscore daemon status
.venv/bin/python -m flashscore daemon stop

# Alternativ im Vordergrund mit Supervisor starten (eigener Zielordner wählbar)
.venv/bin/python -m flashscore --data-dir /pfad/zu/Flashscore_Daten run

# 3. Manueller On-Demand-Export (Formate: json, csv, xlsx, sqlite, zip)
# Alle 10 Ligen komplett als ZIP oder Excel:
.venv/bin/python -m flashscore export --scope all --format zip
.venv/bin/python -m flashscore export --scope all --format xlsx

# Einzelne Liga:
.venv/bin/python -m flashscore export --scope league --id "Bundesliga" --format xlsx

# Einzelne Mannschaft:
.venv/bin/python -m flashscore export --scope team --id "Bayern München" --format json

# Einzelner Spieler inkl. Verletzungschronik:
.venv/bin/python -m flashscore export --scope player --id "Jules Koundé" --format csv

# Einzelnes Spiel inkl. aller Statistiken, Aufstellungen & Verläufe:
.venv/bin/python -m flashscore export --scope match --id "A9JgHzZA" --format sqlite

# Komplette historische Änderungsprotokolle:
.venv/bin/python -m flashscore export --scope history --format json

# 4. Manuelles Komplett-Backup auslösen
.venv/bin/python -m flashscore backup
```
