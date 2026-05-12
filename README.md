# Fed/TGA/RRP Liquidity Indicator

This project builds a daily liquidity indicator from three public data sources:

```text
Net liquidity = Federal Reserve total assets - Treasury General Account - ON RRP
```

All values are stored in USD millions.

## Outputs

- `data/liquidity_latest.json`: latest snapshot
- `data/liquidity_history.csv`: historical daily series
- `reports/liquidity_latest.md`: readable summary
- `reports/liquidity_chart.svg`: chart source
- `reports/liquidity_chart.svg.png`: chart image

The script also copies daily outputs to:

```text
~/Desktop/repo/YYYY-MM-DD/
```

## Data Sources

- Federal Reserve H.4.1 total assets, via FRED `WALCL`
- Treasury General Account closing balance, via FiscalData Daily Treasury Statement API
- ON RRP, via FRED `RRPONTSYD`

Reference pages:

- https://www.federalreserve.gov/Releases/H41/default.htm
- https://fiscaldata.treasury.gov/datasets/daily-treasury-statement/
- https://www.newyorkfed.org/markets/desk-operations/reverse-repo

## Usage

Run:

```bash
python3 scripts/update_liquidity.py
```

Optional market comparison work is present in the script but disabled by default to reduce external requests:

```bash
LIQUIDITY_WITH_MARKETS=1 python3 scripts/update_liquidity.py
```

## Notes

This indicator is a liquidity lens, not a trading signal by itself. It is useful for tracking the market's background funding conditions and whether liquidity is expanding, flat, or contracting.
