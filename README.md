# KAIROS Macro Desk

Macro bias desk for XAUUSD, US500 and US100. Runs on GitHub, without a Claude subscription.

- **App:** https://c4kjmvj87m-glitch.github.io/kairos-desk/ (add it to your phone's home screen)
- **Refresh:** the workflow *KAIROS desk-run* runs on weekdays at 07:46, 13:46 and 22:46 (Brussels time).
- **Analysis:** GitHub Models (free within the limits). With an `ANTHROPIC_API_KEY` secret it uses Claude Haiku 5.5 instead (pay per use). If neither works, the desk runs on the calculation rules only.

## Doing it yourself

- **Refresh now:** Actions → *KAIROS desk-run* → *Run workflow*.
- **Use Claude (optional):** Settings → Secrets and variables → Actions → *New repository secret*, name `ANTHROPIC_API_KEY`.
- **Different free model (optional):** Settings → Secrets and variables → Actions → Variables → `KAIROS_MODEL`, for example `openai/gpt-4.1-mini`.
- **Schedule paused?** GitHub can turn off scheduled workflows in a public project after 60 days without activity. Turn it back on under Actions.

## Files

- `index.html`: the app (also works offline with the last desk).
- `engine/desk.py`: fetches the data, calculates the factor model and writes `data/desk.json` and `data/runs.json`.
- `.github/workflows/`: the schedule (`desk.yml`) and publishing the app (`pages.yml`).

Not financial advice. Prices are snapshots and lag behind your broker.
