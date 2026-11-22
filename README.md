# KAIROS Macro Desk

Macro-bias-desk voor XAUUSD, US500 en US100. Draait gratis op GitHub, zonder Claude-abonnement.

- **App:** https://c4kjmvj87m-glitch.github.io/kairos-desk/ (zet hem op het beginscherm van je telefoon)
- **Verversen:** de workflow *KAIROS desk-run* draait op werkdagen om 07:46, 13:46 en 22:46 (Brusselse tijd).
- **Koersen:** Yahoo Finance; goud en zilver als spotprijs via Swissquote (de niveaus komen uit de COMEX-future, omgerekend naar spot).
- **Analyse:** een AI-model schrijft het oordeel en de uitleg. Daarvoor zet je één sleutel als GitHub-secret (zie hieronder). Zonder sleutel draait de desk op de rekenregels alleen.

## AI aanzetten (één keer, 5 minuten)

Kies één van deze drie. Zet de sleutel altijd alleen in GitHub, nooit in een chat of in een bestand.

| Secret-naam | Dienst | Kosten | Sleutel maken |
|---|---|---|---|
| `GROQ_API_KEY` | Groq (gpt-oss-120b) | gratis, geen creditcard | https://console.groq.com/keys |
| `ANTHROPIC_API_KEY` | Claude Haiku 5.5 | minder dan € 0,50 per maand, vooraf tegoed kopen | https://platform.claude.com (API keys) |
| `GEMINI_API_KEY` | Google Gemini | gratis zolang Google je project een gratis tier geeft | https://aistudio.google.com/apikey |

Sleutel in GitHub zetten: **Settings → Secrets and variables → Actions → New repository secret**, vul de naam uit de tabel in, plak de sleutel bij *Secret* en klik *Add secret*.

Heb je er meer dan één, dan gebruikt de desk eerst Claude, dan Gemini, dan Groq.

## Zelf doen

- **Nu verversen:** Actions → *KAIROS desk-run* → *Run workflow*.
- **Ander model (optioneel):** Settings → Secrets and variables → Actions → *Variables*: `KAIROS_GROQ_MODEL`, `KAIROS_GEMINI_MODEL` of `KAIROS_CLAUDE_MODEL`.
- **Schema uitgeschakeld?** GitHub kan geplande workflows in een openbaar project stilzetten na 60 dagen zonder activiteit. Zet hem dan terug aan onder Actions.

## Bestanden

- `index.html`: de app (werkt ook offline met de laatste desk).
- `engine/desk.py`: haalt de data op, rekent het factormodel uit en schrijft `data/desk.json` en `data/runs.json`.
- `.github/workflows/`: het schema (`desk.yml`) en het publiceren van de app (`pages.yml`).

Geen financieel advies. Koersen zijn momentopnames en lopen achter op je broker.
