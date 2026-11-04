/*
 * Charts for the audit view, on TradingView's open-source lightweight-charts (vendored in
 * /static/vendor, no CDN). All times are exchange wall-clock time, see format.js `wallSeconds`.
 */

import { cssVar, el, fill } from "./dom.js";
import {
  barForTime,
  clock,
  isNumber,
  plain,
  rupees,
  snapTime,
  uniqueAscending,
  wallSeconds,
} from "./format.js";

function library() {
  const lib = window.LightweightCharts;
  if (!lib) throw new Error("The chart library did not load (static/vendor/lightweight-charts).");
  return lib;
}

function palette() {
  return {
    text: cssVar("--muted"),
    grid: cssVar("--chart-grid"),
    border: cssVar("--border"),
    up: cssVar("--pos"),
    down: cssVar("--neg"),
    fast: cssVar("--ema-fast"),
    slow: cssVar("--ema-slow"),
    entry: cssVar("--accent"),
    exit: cssVar("--warn"),
    crosshair: cssVar("--muted"),
  };
}

function chartOptions(colors, { timeVisible = true } = {}) {
  return {
    autoSize: true,
    layout: {
      background: { type: "solid", color: "transparent" },
      textColor: colors.text,
      fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
      fontSize: 12,
    },
    grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
    rightPriceScale: { borderColor: colors.border },
    timeScale: { borderColor: colors.border, timeVisible, secondsVisible: false },
    crosshair: {
      mode: 0,
      vertLine: { color: colors.crosshair, labelBackgroundColor: colors.border },
      horzLine: { color: colors.crosshair, labelBackgroundColor: colors.border },
    },
    handleScale: { axisPressedMouseMove: true },
  };
}

function candleOptions(colors) {
  return {
    upColor: colors.up,
    downColor: colors.down,
    wickUpColor: colors.up,
    wickDownColor: colors.down,
    borderVisible: false,
    priceFormat: { type: "price", precision: 2, minMove: 0.05 },
  };
}

const candle = (row) => ({
  time: wallSeconds(row[0]),
  open: row[1],
  high: row[2],
  low: row[3],
  close: row[4],
});

function legendItem(label, value, color) {
  return el(
    "span",
    { class: "legend__item" },
    el("span", { class: "legend__label" }, label),
    el("span", { class: "legend__value" }, value),
    color ? el("span", { class: "legend__swatch", "data-color": color }) : null,
  );
}

export class TradeCharts {
  constructor({ spot, option, equity, spotLegend, optionLegend }) {
    this.lib = library();
    this.nodes = { spot, option, equity, spotLegend, optionLegend };
    const colors = palette();

    this.spotChart = this.lib.createChart(spot, chartOptions(colors));
    this.spotSeries = this.spotChart.addCandlestickSeries(candleOptions(colors));
    this.fastLine = this.spotChart.addLineSeries(this.lineOptions(colors.fast, "EMA fast"));
    this.slowLine = this.spotChart.addLineSeries(this.lineOptions(colors.slow, "EMA slow"));

    this.optionChart = this.lib.createChart(option, chartOptions(colors));
    this.optionSeries = this.optionChart.addCandlestickSeries(candleOptions(colors));
    this.priceLines = [];

    this.equityChart = this.lib.createChart(equity, chartOptions(colors, { timeVisible: false }));
    this.equitySeries = this.equityChart.addBaselineSeries(this.baselineOptions(colors));

    this.syncing = false;
    this.fastName = "EMA";
    this.slowName = "EMA";
    this.link(this.spotChart, this.optionChart);
    this.link(this.optionChart, this.spotChart);
    this.spotChart.subscribeCrosshairMove((param) => this.showSpotLegend(param));
    this.optionChart.subscribeCrosshairMove((param) => this.showOptionLegend(param));
  }

  lineOptions(color, title) {
    return {
      color,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      title,
    };
  }

  baselineOptions(colors) {
    return {
      baseValue: { type: "price", price: 0 },
      topLineColor: colors.up,
      topFillColor1: `${colors.up}55`,
      topFillColor2: `${colors.up}08`,
      bottomLineColor: colors.down,
      bottomFillColor1: `${colors.down}08`,
      bottomFillColor2: `${colors.down}55`,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
      priceFormat: { type: "custom", formatter: (value) => rupees(value) },
    };
  }

  /** Keep the two intraday charts scrolled and zoomed together (they share a trading day). */
  link(from, to) {
    from.timeScale().subscribeVisibleTimeRangeChange((range) => {
      if (this.syncing || !range) return;
      this.syncing = true;
      try {
        to.timeScale().setVisibleRange(range);
      } catch {
        // the other chart has no data yet; nothing to align
      } finally {
        this.syncing = false;
      }
    });
  }

  // --------------------------------------------------------------------------- trade

  showTrade(detail) {
    const { trade, spot, option } = detail;
    const bars = spot.bars;

    this.spotSeries.setData(bars.map(candle));
    const line = (index) =>
      bars
        .filter((row) => isNumber(row[index]))
        .map((row) => ({ time: wallSeconds(row[0]), value: row[index] }));
    this.fastLine.setData(line(5));
    this.slowLine.setData(line(6));
    this.fastName = `EMA ${spot.fast}`;
    this.slowName = `EMA ${spot.slow}`;
    this.fastLine.applyOptions({ title: this.fastName });
    this.slowLine.applyOptions({ title: this.slowName });

    this.spotSeries.setMarkers(this.spotMarkers(trade, bars, spot.timeframe));
    this.spotMeta = { bars, timeframe: spot.timeframe };

    const candles = option.candles.map(candle);
    this.optionSeries.setData(candles);
    for (const priceLine of this.priceLines) this.optionSeries.removePriceLine(priceLine);
    this.priceLines = [];
    const colors = palette();
    const addLine = (price, title, color) => {
      if (!isNumber(price)) return;
      this.priceLines.push(
        this.optionSeries.createPriceLine({
          price,
          color,
          lineWidth: 1,
          lineStyle: this.lib.LineStyle.Dashed,
          axisLabelVisible: true,
          title,
        }),
      );
    };
    addLine(trade.option_entry_fill, "Entry fill", colors.entry);
    addLine(trade.option_exit_fill, "Exit fill", colors.exit);
    this.optionSeries.setMarkers(this.optionMarkers(trade, candles));

    this.fitDay(trade.date);
    this.clearLegends();
  }

  spotMarkers(trade, bars, timeframe) {
    const colors = palette();
    const bullish = trade.direction === "bullish";
    const markers = [];
    const entry = barForTime(bars, trade.entry_decision, timeframe);
    if (entry !== null) {
      markers.push({
        time: entry,
        position: bullish ? "belowBar" : "aboveBar",
        shape: bullish ? "arrowUp" : "arrowDown",
        color: colors.entry,
        text: `Entry ${plain(trade.spot_entry, 2)}`,
      });
    }
    const exit = barForTime(bars, trade.exit_decision, timeframe);
    if (exit !== null) {
      markers.push({
        time: exit,
        position: bullish ? "aboveBar" : "belowBar",
        shape: "circle",
        color: colors.exit,
        text: `Exit ${plain(trade.spot_exit, 2)}`,
      });
    }
    return markers.sort((a, b) => a.time - b.time);
  }

  optionMarkers(trade, candles) {
    const colors = palette();
    const times = candles.map((c) => c.time);
    const sell = trade.side === "SELL";
    const markers = [];
    const entry = snapTime(times, wallSeconds(trade.entry_fill));
    if (entry !== null) {
      markers.push({
        time: entry,
        position: sell ? "aboveBar" : "belowBar",
        shape: sell ? "arrowDown" : "arrowUp",
        color: colors.entry,
        text: `${trade.side} ${plain(trade.option_entry_fill, 2)}`,
      });
    }
    const exit = snapTime(times, wallSeconds(trade.exit_fill));
    if (exit !== null) {
      markers.push({
        time: exit,
        position: sell ? "belowBar" : "aboveBar",
        shape: "circle",
        color: colors.exit,
        text: `${sell ? "BUY" : "SELL"} ${plain(trade.option_exit_fill, 2)}`,
      });
    }
    return markers.sort((a, b) => a.time - b.time);
  }

  /** Show the whole session, 09:15 to 15:30, on both intraday charts. */
  fitDay(day) {
    const from = wallSeconds(`${day}T09:15:00`);
    const to = wallSeconds(`${day}T15:30:00`);
    this.syncing = true;
    try {
      this.spotChart.timeScale().setVisibleRange({ from, to });
      this.optionChart.timeScale().setVisibleRange({ from, to });
    } catch {
      this.spotChart.timeScale().fitContent();
      this.optionChart.timeScale().fitContent();
    } finally {
      this.syncing = false;
    }
  }

  clear() {
    for (const series of [this.spotSeries, this.fastLine, this.slowLine, this.optionSeries]) {
      series.setData([]);
    }
    this.spotSeries.setMarkers([]);
    this.optionSeries.setMarkers([]);
    for (const priceLine of this.priceLines) this.optionSeries.removePriceLine(priceLine);
    this.priceLines = [];
    this.clearLegends();
  }

  // -------------------------------------------------------------------------- equity

  setEquity(points) {
    const data = uniqueAscending(
      points.map(([iso, value]) => ({ time: wallSeconds(iso), value })),
    );
    this.equitySeries.setData(data);
    if (data.length) this.equityChart.timeScale().fitContent();
    return data.length;
  }

  // ------------------------------------------------------------------------- legends

  clearLegends() {
    this.clearSpotLegend();
    this.clearOptionLegend();
  }

  clearSpotLegend() {
    fill(
      this.nodes.spotLegend,
      el("span", { class: "legend__hint" }, "Hover a bar for its values"),
    );
  }

  clearOptionLegend() {
    fill(
      this.nodes.optionLegend,
      el("span", { class: "legend__hint" }, "Hover a candle for its values"),
    );
  }

  showSpotLegend(param) {
    const bar = param?.seriesData?.get(this.spotSeries);
    if (!param?.time || !bar) return this.clearSpotLegend();
    const fast = param.seriesData.get(this.fastLine);
    const slow = param.seriesData.get(this.slowLine);
    const when = new Date(param.time * 1000).toISOString();
    fill(
      this.nodes.spotLegend,
      legendItem("", clock(when.slice(0, 19))),
      legendItem("O", plain(bar.open, 2)),
      legendItem("H", plain(bar.high, 2)),
      legendItem("L", plain(bar.low, 2)),
      legendItem("C", plain(bar.close, 2)),
      legendItem(this.fastName, plain(fast?.value, 2), "fast"),
      legendItem(this.slowName, plain(slow?.value, 2), "slow"),
    );
  }

  showOptionLegend(param) {
    const bar = param?.seriesData?.get(this.optionSeries);
    if (!param?.time || !bar) return this.clearOptionLegend();
    const when = new Date(param.time * 1000).toISOString();
    fill(
      this.nodes.optionLegend,
      legendItem("", clock(when.slice(0, 19))),
      legendItem("O", plain(bar.open, 2)),
      legendItem("H", plain(bar.high, 2)),
      legendItem("L", plain(bar.low, 2)),
      legendItem("C", plain(bar.close, 2)),
    );
  }

  // --------------------------------------------------------------------------- theme

  applyTheme() {
    const colors = palette();
    for (const chart of [this.spotChart, this.optionChart, this.equityChart]) {
      chart.applyOptions({
        layout: { textColor: colors.text },
        grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
        rightPriceScale: { borderColor: colors.border },
        timeScale: { borderColor: colors.border },
        crosshair: {
          vertLine: { color: colors.crosshair },
          horzLine: { color: colors.crosshair },
        },
      });
    }
    this.spotSeries.applyOptions(candleOptions(colors));
    this.optionSeries.applyOptions(candleOptions(colors));
    this.fastLine.applyOptions({ color: colors.fast });
    this.slowLine.applyOptions({ color: colors.slow });
    this.equitySeries.applyOptions(this.baselineOptions(colors));
  }
}
