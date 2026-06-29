# Nussebassene

En enkel tippeapp for utslagsrunder.

## Arkitektur

- GitHub Pages: frontend
- Vercel: kun innsending av tips
- GitHub-repoet: datalager for tips og genererte datafiler
- GitHub Actions: henter kamper/resultater via `sync-kamper.py`, beregner poeng og bygger `data/data.js`
- cron-job.org: ekstern trigger av `oppdater.yml`

## Viktige filer

- `index.html`: forside/stilling/kamper/innleveringsstatus
- `tipping.html`: innlevering av tips
- `api/submit-tip.js`: Vercel serverless-funksjon som skriver tips til GitHub
- `scripts/sync-kamper.py`: henter FIFA Calendar som primærkilde og bruker football-data.org som kontroll/fallback
- `scripts/poengregning.py`: beregner poeng fra `data/status.json` uten egne API-kall
- `scripts/bygg-innleveringsstatus.py`: bygger innleveringsstatus
- `.github/workflows/oppdater.yml`: GitHub Actions-jobb

## GitHub secrets

Legg inn i repoet:

```text
FOOTBALL_DATA_TOKEN
```

Valgfri repo variable:

```text
FIFA_CALENDAR_URL
FOOTBALL_DATA_COMPETITION=WC
```

`FIFA_CALENDAR_URL` kan brukes hvis standardkandidaten ikke gir korrekt FIFA Calendar-respons.

Valgfri kontrollinnstilling:

```text
FD_RESULT_FALLBACK=true
```

Når `FD_RESULT_FALLBACK=true`, kan football-data.org fylle inn ferdig resultat hvis FIFA Calendar mangler resultat. Når FIFA og football-data begge har resultat, vinner FIFA alltid. Sett `FD_RESULT_FALLBACK=false` hvis appen aldri skal bruke football-data-resultat til poengberegning.

## Vercel env

Legg inn i Vercel-prosjektet:

```text
GITHUB_OWNER=RambergApps
GITHUB_REPO=Nussebassene
GITHUB_BRANCH=main
GITHUB_TOKEN=<PAT>
SUBMIT_TOKEN=<valgfritt>
ALLOWED_ORIGIN=https://rambergapps.github.io
```

Hvis Vercel også skal trigge Actions etter innsending, må tokenet ha tilgang til Actions workflow dispatch.

## Frontend-konfig

I `app-config.js`, sett:

```js
window.NUSSEBASSENE_CONFIG = {
  apiBaseUrl: "https://DITT-VERCEL-PROSJEKT.vercel.app",
  submitToken: ""
};
```

Hvis `SUBMIT_TOKEN` er satt i Vercel, må samme verdi legges inn som `submitToken`. Dette er ikke ekte sikkerhet, siden frontend-kode er synlig. Bruk det kun som enkel friksjon mot tilfeldige kall.

## cron-job.org

Request:

```http
POST https://api.github.com/repos/RambergApps/Nussebassene/actions/workflows/oppdater.yml/dispatches
Authorization: Bearer <GitHub PAT>
Accept: application/vnd.github+json
Content-Type: application/json
```

Body:

```json
{
  "ref": "main"
}
```

Start med hvert 10. minutt. På kampdager kan frekvens settes til hvert 5. minutt.

## Poengregler

Standard:

- 3 poeng for eksakt 90-minuttersresultat
- 1 poeng for riktig utfall etter 90 minutter
- Poeng beregnes først når kampen er ferdig
- Tips skjules før avspark og vises etter avspark

Kan justeres i Actions med miljøvariabler:

```text
POINTS_EXACT=3
POINTS_OUTCOME=1
```
