# Strategy Desk (website)

A static copy of the NIFTY options backtest: real simulated trades you can filter by timeframe,
strategy, time of day, day of the week and date, each drawn on a chart. Everything runs in your
browser; there is no server, no login and nothing here can place an order.

Backtests are simulations on free community data and do not promise future results.

## Run it locally

```bash
python3 -m http.server 8000   # then open http://localhost:8000
```

Opening `index.html` straight from the folder will not work: browsers refuse to load the data
files from `file://`.

## Publish it

Push this folder to a public GitHub repository, then **Settings, Pages, Deploy from a branch,
`main`, `/ (root)`**.

This folder is generated: do not edit it by hand. Rebuild it from the main project with
`make site`.
