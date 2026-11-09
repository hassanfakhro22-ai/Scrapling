# LiveScore.com/de – Fußball-Extraktion (9 Ligen)

Dieser Ordner enthält die mit **Scrapling** extrahierten Daten von [https://www.livescore.com/de/](https://www.livescore.com/de/) sowie das zugehörige Scraper-Skript für die folgenden **9 europäischen Ligen**:

| Land | Liga | LiveScore-URL |
|:---|:---|:---|
| **Deutschland** | 1. Bundesliga | [livescore.com/de/fussball/deutschland/bundesliga/](https://www.livescore.com/de/fussball/deutschland/bundesliga/) |
| **Deutschland** | 2. Bundesliga | [livescore.com/de/fussball/deutschland/2-bundesliga/](https://www.livescore.com/de/fussball/deutschland/2-bundesliga/) |
| **Deutschland** | 3. Liga | [livescore.com/de/fussball/deutschland/3-liga/](https://www.livescore.com/de/fussball/deutschland/3-liga/) |
| **England** | Premier League (1. Liga) | [livescore.com/de/fussball/england/premier-league/](https://www.livescore.com/de/fussball/england/premier-league/) |
| **England** | Championship (2. Liga) | [livescore.com/de/fussball/england/championship/](https://www.livescore.com/de/fussball/england/championship/) |
| **Spanien** | LaLiga (1. Liga) | [livescore.com/de/fussball/spanien/laliga/](https://www.livescore.com/de/fussball/spanien/laliga/) |
| **Spanien** | LaLiga 2 (2. Liga) | [livescore.com/de/fussball/spanien/laliga-2/](https://www.livescore.com/de/fussball/spanien/laliga-2/) |
| **Frankreich** | Ligue 1 (1. Liga) | [livescore.com/de/fussball/frankreich/ligue-1/](https://www.livescore.com/de/fussball/frankreich/ligue-1/) |
| **Frankreich** | Ligue 2 (2. Liga) | [livescore.com/de/fussball/frankreich/ligue-2/](https://www.livescore.com/de/fussball/frankreich/ligue-2/) |

## Enthaltene Dateien

- **[`livescore_de_extrakt.md`](./livescore_de_extrakt.md)** – Übersichtlich formatierter Markdown-Bericht (heutige Spiele, letzte Ergebnisse, nächste Spielpläne in MEZ, Top-Torjäger und vollständige Tabellen aller 9 Ligen).
- **[`livescore_de_extrakt.json`](./livescore_de_extrakt.json)** – Strukturierter JSON-Export aller extrahierten Daten (alle 178 Vereine über die 9 Ligen).
- **[`livescore_scraper.py`](./livescore_scraper.py)** – Wiederverwendbares Scrapling-Skript zum erneuten Abrufen der Live-Daten.

## Lokale Ausführung des Scrapers

```bash
pip install "scrapling[all]"

# Schneller Abruf über FetcherSession (empfohlen)
python livescore/livescore_scraper.py --date 2026-11-09 --tz 1 --output livescore/livescore_de_extrakt.json

# Optional mit Headless-Browser (StealthySession)
scrapling install
python livescore/livescore_scraper.py --browser
```
