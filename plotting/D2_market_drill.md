# D2: One Day of Market Data (~30 min)

A trader asks: *"What happened in XYZ today, and how unusual was it?"*

**Data**: run `python3 make_data.py` once to create these files.
- `data/d2_quotes_1s.parquet`: today's quotes at one-second resolution. Columns: `ts`, `bid`, `ask`, and `volume` (shares traded in that second). The tick size is $0.01.
- `data/d2_intraday_20d.parquet`: the last 20 trading days, one row per minute. Columns: `date`, `minute` (minutes since 09:30), `volume`, and `spread_bps`.

For each question: choose a chart and say **why that chart**, make it, then explain what it shows in 2–3 sentences.

1. How did the price move over the day?
2. Something happened around 10:00. Zoom in on 09:59:30–10:01:00: what did the quotes and the trading do?
3. How wide is the spread normally?
4. What does a typical day's trading volume look like across the session?
5. Is the spread pattern the same every day? Does any day stand out?

**Present**: describe today to the trader in four sentences, using at most two charts.
