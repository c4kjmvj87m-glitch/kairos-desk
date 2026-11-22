# KAIROS Macro Desk

Je KAIROS-desk, los van het Claude-abonnement. De pagina is een exacte kopie van de desk in Claude: dezelfde code, hetzelfde uiterlijk en dezelfde onderdelen.

- **App:** https://c4kjmvj87m-glitch.github.io/kairos-desk/ (zet hem op het beginscherm van je telefoon)
- **Verversen:** de workflow *KAIROS desk-run* draait op werkdagen om 07:15, 13:15 en 22:15 (Brusselse tijd), net als in Claude. Het scherm ververst vanzelf.

## Hoe het werkt

| Onderdeel | Bron |
|---|---|
| Goud | spotprijs van Swissquote; kaarsen van het actiefste COMEX-contract, omgerekend naar spot met het verschil van dat moment |
| S&P 500 en Nasdaq 100 | de index (Yahoo); buiten de beurstijden de future, omgerekend zoals een CFD |
| Zones en plannen | berekend uit de M15- en H1-kaarsen: basis met een sterke departure, alleen verse zones |
| Dollar, rente, olie, zilver, valuta | Yahoo Finance en het Amerikaanse ministerie van Financiën |
| Fed-kans | berekend uit de fed-funds-futures, rente van de NY Fed, vergaderdata van de Fed |
| Agenda | ForexFactory |
| Nieuws en stemming | Google News, CNN Fear & Greed, Stocktwits |
| Berichten op X | Claude zoekt ze op internet (4 zoekopdrachten per run) |
| Teksten, plannen, les en quiz | Claude Haiku 5.5, in hetzelfde eenvoudige Nederlands als in Claude |

**Kosten:** ongeveer € 3,50 per maand aan Claude-tegoed (ongeveer 5 cent per run). Zonder zoeken op X zet je de variabele `KAIROS_SEARCHES` op `0` (Settings → Secrets and variables → Actions → Variables). Dan kost het ongeveer € 1 per maand.

## Sleutels (Settings → Secrets and variables → Actions)

| Secret | Waarvoor |
|---|---|
| `ANTHROPIC_API_KEY` | Claude: de analyse, de teksten en het zoeken op X |
| `GROQ_API_KEY` | gratis reserve als Claude niet werkt (alleen de korte analyse) |

Zet een sleutel nooit in een chat of in een bestand.

## Op je toestel

- **Je naam in de begroeting:** open de app één keer met `?naam=JouwNaam` achter de link.
- **Vraag de desk:** bij je eerste vraag plak je een Claude-sleutel. Die blijft alleen op dat toestel en gaat alleen naar Claude. Je mag hiervoor een aparte sleutel maken.
- **Logboek:** je trades blijven op het toestel waar je ze invoert.
- **Voorlezen:** gebruikt de stem van je toestel.

## Bestanden

- `index.html`: de kopie van de Claude-desk, gemaakt met `tools/sync_page.py`.
- `kairos-shim.js`: vervangt wat Claude levert (database, naam, logboek, voorlezen, Vraag de desk).
- `engine/kairos.py`: de desk-run; schrijft `data/desk.json`, `data/runs.json` en `data/state.json`. `engine/desk.py` bevat de bouwstenen.

Geen financieel advies. Koersen lopen achter op je broker: controleer ze in MT5 en trade nooit zonder stop.
