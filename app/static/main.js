const seriesColors = {
  download_mbps: "#c45d2b",
  upload_mbps: "#0a8f8a",
  avg_ms: "#22c55e",
  jitter_ms: "#1f2937",
  packet_loss: "#ef4444",
};

function formatNumber(value, decimals = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "n/a";
  }
  return Number(value).toFixed(decimals);
}

function normalizeValues(values) {
  const filtered = values.filter((value) => value !== null && value !== undefined);
  if (!filtered.length) {
    return { min: 0, max: 1 };
  }
  const min = Math.min(...filtered);
  const max = Math.max(...filtered);
  if (min === max) {
    return { min: min - 1, max: max + 1 };
  }
  return { min, max };
}

function toggleHidden(element, hidden) {
  if (!element) {
    return;
  }
  element.classList.toggle("hidden", hidden);
}

function buildPath(values, bounds, dimensions) {
  const { left, right, top, bottom, width, height } = dimensions;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const scale = bounds.max - bounds.min;
  const points = [];

  values.forEach((value, index) => {
    if (value === null || value === undefined) {
      points.push(null);
      return;
    }
    const x = values.length > 1 ? left + (index / (values.length - 1)) * plotWidth : left + plotWidth / 2;
    const y = top + ((bounds.max - value) / scale) * plotHeight;
    points.push({ x, y, value, index });
  });

  let path = "";
  points.forEach((point, index) => {
    if (!point) {
      return;
    }
    const previous = points[index - 1];
    path += `${previous ? " L" : "M"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`;
  });

  return { path: path.trim(), points };
}

function xTicks(labels) {
  if (!labels.length) {
    return [];
  }
  const indexes = [0, Math.floor((labels.length - 1) / 3), Math.floor(((labels.length - 1) * 2) / 3), labels.length - 1];
  return [...new Set(indexes)].map((index) => ({ index, label: labels[index] }));
}

function renderChart(element, payload) {
  if (!payload || !payload.labels || !payload.labels.length) {
    element.innerHTML = '<div class="chart-empty">Waiting for samples...</div>';
    return;
  }

  const dimensions = {
    width: 960,
    height: element.classList.contains("compact") ? 220 : 340,
    left: 72,
    right: 24,
    top: 18,
    bottom: 58,
  };
  const values = payload.series.flatMap((series) => series.values);
  const bounds = normalizeValues(values);
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
    const value = bounds.max - ratio * (bounds.max - bounds.min);
    const y = dimensions.top + ratio * (dimensions.height - dimensions.top - dimensions.bottom);
    return { value, y };
  });
  const xTickItems = xTicks(payload.labels);
  const seriesData = payload.series.map((series) => ({
    ...series,
    color: seriesColors[series.key] || "#16212e",
    ...buildPath(series.values, bounds, dimensions),
  }));

  const legend = payload.series
    .map(
      (series) =>
        `<span><i style="background:${seriesColors[series.key] || "#16212e"}"></i>${series.label}</span>`,
    )
    .join("");

  const yTickMarkup = yTicks
    .map(
      (tick) => `
        <g>
          <line x1="${dimensions.left}" y1="${tick.y}" x2="${dimensions.width - dimensions.right}" y2="${tick.y}" class="chart-grid-line" />
          <text x="${dimensions.left - 12}" y="${tick.y + 4}" text-anchor="end" class="chart-axis-text">${formatNumber(tick.value, 2)}</text>
        </g>
      `,
    )
    .join("");

  const xTickMarkup = xTickItems
    .map((tick) => {
      const plotWidth = dimensions.width - dimensions.left - dimensions.right;
      const x = payload.labels.length > 1
        ? dimensions.left + (tick.index / (payload.labels.length - 1)) * plotWidth
        : dimensions.left + plotWidth / 2;
      return `
        <g>
          <line x1="${x}" y1="${dimensions.height - dimensions.bottom}" x2="${x}" y2="${dimensions.height - dimensions.bottom + 6}" class="chart-axis-line" />
          <text x="${x}" y="${dimensions.height - 16}" text-anchor="middle" class="chart-axis-text">${tick.label}</text>
        </g>
      `;
    })
    .join("");

  const pathMarkup = seriesData
    .map((series) => {
      if (!series.path) {
        return "";
      }
      return `<path d="${series.path}" fill="none" stroke="${series.color}" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" />`;
    })
    .join("");

  element.innerHTML = `
    <div class="chart-shell">
      <div class="chart-legend">${legend}</div>
      <div class="chart-stage">
        <svg viewBox="0 0 ${dimensions.width} ${dimensions.height}" width="100%" height="${dimensions.height}" preserveAspectRatio="none" aria-hidden="true">
          <g>
            ${yTickMarkup}
            <line x1="${dimensions.left}" y1="${dimensions.height - dimensions.bottom}" x2="${dimensions.width - dimensions.right}" y2="${dimensions.height - dimensions.bottom}" class="chart-axis-line" />
            <line x1="${dimensions.left}" y1="${dimensions.top}" x2="${dimensions.left}" y2="${dimensions.height - dimensions.bottom}" class="chart-axis-line" />
            ${xTickMarkup}
            ${pathMarkup}
            <line class="chart-hover-line hidden" x1="0" y1="${dimensions.top}" x2="0" y2="${dimensions.height - dimensions.bottom}"></line>
            <g class="chart-hover-points"></g>
            <text x="${dimensions.width / 2}" y="${dimensions.height - 2}" text-anchor="middle" class="chart-axis-label">${payload.x_label}</text>
            <text x="16" y="${dimensions.height / 2}" text-anchor="middle" class="chart-axis-label" transform="rotate(-90 16 ${dimensions.height / 2})">${payload.y_label}</text>
          </g>
        </svg>
        <div class="chart-tooltip hidden"></div>
      </div>
      <div class="meta">
        <span>Latest sample: ${payload.labels[payload.labels.length - 1]}</span>
        <span>Range: ${formatNumber(bounds.min)} to ${formatNumber(bounds.max)} ${payload.unit}</span>
      </div>
    </div>
  `;

  const stage = element.querySelector(".chart-stage");
  const tooltip = element.querySelector(".chart-tooltip");
  const hoverLine = element.querySelector(".chart-hover-line");
  const hoverPoints = element.querySelector(".chart-hover-points");
  const plotWidth = dimensions.width - dimensions.left - dimensions.right;

  function updateTooltip(index, event) {
    if (index < 0 || index >= payload.labels.length) {
      return;
    }
    const x = payload.labels.length > 1
      ? dimensions.left + (index / (payload.labels.length - 1)) * plotWidth
      : dimensions.left + plotWidth / 2;
    hoverLine.setAttribute("x1", x);
    hoverLine.setAttribute("x2", x);
    hoverLine.classList.remove("hidden");

    const pointMarkup = seriesData
      .map((series) => {
        const point = series.points[index];
        if (!point) {
          return "";
        }
        return `<circle cx="${point.x}" cy="${point.y}" r="5.5" fill="${series.color}" class="chart-point" />`;
      })
      .join("");
    hoverPoints.innerHTML = pointMarkup;

    const valuesMarkup = seriesData
      .map((series) => {
        const value = series.values[index];
        const unit = series.unit || payload.unit;
        return `<div><span class="tooltip-swatch" style="background:${series.color}"></span>${series.label}: ${formatNumber(value)} ${unit}</div>`;
      })
      .join("");
    tooltip.innerHTML = `<strong>${payload.labels[index]}</strong>${valuesMarkup}`;
    tooltip.classList.remove("hidden");

    const rect = stage.getBoundingClientRect();
    const left = Math.min(rect.width - 180, Math.max(10, event.clientX - rect.left + 16));
    const top = Math.max(10, event.clientY - rect.top - 10);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }

  stage.addEventListener("mousemove", (event) => {
    const rect = stage.getBoundingClientRect();
    const relativeX = event.clientX - rect.left;
    const plotLeft = (dimensions.left / dimensions.width) * rect.width;
    const plotRight = rect.width - (dimensions.right / dimensions.width) * rect.width;
    const clamped = Math.min(plotRight, Math.max(plotLeft, relativeX));
    const ratio = (clamped - plotLeft) / Math.max(1, plotRight - plotLeft);
    const index = payload.labels.length > 1 ? Math.round(ratio * (payload.labels.length - 1)) : 0;
    updateTooltip(index, event);
  });

  stage.addEventListener("mouseleave", () => {
    hoverLine.classList.add("hidden");
    hoverPoints.innerHTML = "";
    tooltip.classList.add("hidden");
  });
}

function renderAllCharts(root = document) {
  root.querySelectorAll("[data-chart]").forEach((element) => {
    const payload = JSON.parse(element.dataset.chartPayload || "{}");
    renderChart(element, payload);
  });
}

function setText(selector, value) {
  const element = document.querySelector(selector);
  if (element) {
    element.textContent = value;
  }
}

function setError(selector, message) {
  const element = document.querySelector(selector);
  if (!element) {
    return;
  }
  element.textContent = message || "";
  toggleHidden(element, !message);
}

function updateSpeedtest(speedtest) {
  setText("#overview-download", `${speedtest.download_text} Mbit/s`);
  setText("#overview-upload", `${speedtest.upload_text} Mbit/s`);
  setText("#overview-latency", `${speedtest.latency_text} ms`);
  setText("#speedtest-server", speedtest.server_text);
  setText("#speedtest-location", speedtest.location_text);
  setText("#speedtest-last", speedtest.last_text);
  const chart = document.querySelector("#speedtest-chart");
  if (chart) {
    chart.dataset.chartPayload = JSON.stringify(speedtest.chart);
    renderChart(chart, speedtest.chart);
  }
  setError("#speedtest-error", speedtest.error);
}

function updatePingTargets(targets) {
  targets.forEach((target) => {
    const card = document.querySelector(`[data-ping-card="${target.id}"]`);
    if (!card) {
      return;
    }
    card.querySelector(".js-ping-avg").textContent = target.latest.avg_text;
    card.querySelector(".js-ping-jitter").textContent = target.latest.jitter_text;
    card.querySelector(".js-ping-loss").textContent = target.latest.loss_text;
    card.querySelector(".js-ping-last").textContent = target.latest.last_text;
    const chart = card.querySelector("[data-chart]");
    if (chart) {
      chart.dataset.chartPayload = JSON.stringify(target.chart);
      renderChart(chart, target.chart);
    }
    setError(`[data-ping-card="${target.id}"] .js-ping-error`, target.latest.error);
  });
}

function updateDashboardPinnedPingCharts(targets) {
  targets.forEach((target) => {
    const card = document.querySelector(`[data-dashboard-ping-card="${target.id}"]`);
    if (!card) {
      return;
    }
    const chart = card.querySelector("[data-dashboard-ping-chart]");
    if (!chart) {
      return;
    }
    chart.dataset.chartPayload = JSON.stringify(target.chart);
    renderChart(chart, target.chart);
  });
}

function updateMtrTargets(targets) {
  targets.forEach((target) => {
    const card = document.querySelector(`[data-mtr-card="${target.id}"]`);
    if (!card) {
      return;
    }
    card.querySelector(".js-mtr-interval").textContent = target.interval_text;
    card.querySelector(".js-mtr-probes").textContent = target.probes_text;
    card.querySelector(".js-mtr-hops").textContent = target.hops_text;
    card.querySelector(".js-mtr-last").textContent = target.last_text;
    const table = card.querySelector(".js-mtr-table");
    if (table) {
      table.innerHTML = target.hops.length
        ? target.hops
            .map(
              (hop) => `
                <tr>
                  <td>${hop.hop_index}</td>
                  <td>${hop.address}</td>
                  <td>${hop.loss_text}</td>
                  <td>${hop.avg_text} ms</td>
                  <td>${hop.jitter_text} ms</td>
                </tr>
              `,
            )
            .join("")
        : '<tr><td colspan="5">No MTR data yet.</td></tr>';
    }
    setError(`[data-mtr-card="${target.id}"] .js-mtr-error`, target.error);
  });
}

renderAllCharts();
