// Vanilla JS for the hourly status strips:
//   1. Local time — the server renders timestamps in UTC; we relabel them in
//      the viewer's local timezone (bar tooltips, the "Last updated" /
//      incident-start clocks, and where the day dividers fall).
//   2. Paging — each strip holds STRIP_DAYS*24 hourly bars (one per hour). Only
//      one 2-day page (48 bars) is shown at a time; the ‹ › controls in the
//      "System Status" header move the window and a label shows its date range.
//      The newest page is shown first, so the right-most bar is the last hour.
//   3. Tooltip — hover/focus shows a bar's hour, state, uptime, endpoint;
//      click pins it, click outside dismisses.

(function () {
  "use strict";

  // --- Local-time formatting (viewer's timezone + abbreviation, 24h) ---
  // `undefined` locale → the browser's locale; timeZoneName: "short" appends
  // e.g. "PDT" so it's obvious the times are local, not UTC.
  function makeFmt(opts) {
    try {
      return new Intl.DateTimeFormat(undefined, opts);
    } catch (e) {
      return null;
    }
  }
  var HOUR_FMT = makeFmt({
    month: "short", day: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short",
  });
  var DAY_KEY_FMT = makeFmt({ year: "numeric", month: "2-digit", day: "2-digit" });
  var RANGE_FMT_FROM = makeFmt({ month: "short", day: "numeric" });
  var RANGE_FMT_TO = makeFmt({ month: "short", day: "numeric", year: "numeric" });

  function parseBarDate(iso) {
    // iso is the UTC hour key "YYYY-MM-DDTHH:00Z"; Date parses the Z instant.
    if (!iso) return null;
    var d = new Date(iso);
    return isNaN(d.getTime()) ? null : d;
  }

  // Rewrite server-rendered UTC timestamps (.js-localtime[data-ts], epoch
  // seconds) into the viewer's local time.
  function localizeTimes(root) {
    if (!HOUR_FMT) return;
    (root || document).querySelectorAll(".js-localtime").forEach(function (el) {
      var ts = parseFloat(el.dataset.ts);
      if (isNaN(ts)) return;
      el.textContent = HOUR_FMT.format(new Date(ts * 1000));
    });
  }

  // --- 2-day (48-hour) paging across every strip, driven from the header ---
  var BARS_PER_PAGE = 48;
  var pageIndex = 0; // 0 = newest page (right-most bar = the last hour)

  function barsOf(strip) {
    return Array.prototype.slice.call(strip.querySelectorAll(".bar"));
  }
  function firstStrip() {
    // Every strip spans the same hours (server pads them identically), so the
    // first one is representative for the bar count and the date-range label.
    return document.querySelector(".strip");
  }
  function totalBars() {
    var s = firstStrip();
    return s ? barsOf(s).length : 0;
  }
  function maxPageIndex() {
    var n = totalBars();
    return n ? Math.max(0, Math.ceil(n / BARS_PER_PAGE) - 1) : 0;
  }

  // Show only the current page's bars (hide the rest) and re-mark the
  // local-midnight day dividers among the bars that are now visible.
  function applyPage() {
    document.querySelectorAll(".strip").forEach(function (strip) {
      var bars = barsOf(strip);
      var end = bars.length - BARS_PER_PAGE * pageIndex; // exclusive
      var start = Math.max(0, end - BARS_PER_PAGE);
      var prevKey = null;
      bars.forEach(function (bar, i) {
        var visible = i >= start && i < end;
        bar.style.display = visible ? "" : "none";
        bar.classList.remove("bar-day-start");
        if (!visible) return;
        var d = parseBarDate(bar.dataset.barHourIso);
        if (DAY_KEY_FMT && d) {
          var key = DAY_KEY_FMT.format(d);
          if (prevKey !== null && key !== prevKey) bar.classList.add("bar-day-start");
          prevKey = key;
        }
      });
    });
    updateRangeLabel();
    updatePagerButtons();
  }

  function updateRangeLabel() {
    var label = document.getElementById("pager-range");
    if (!label) return;
    var strip = firstStrip();
    var visible = strip ? barsOf(strip).filter(function (b) { return b.style.display !== "none"; }) : [];
    if (!visible.length || !RANGE_FMT_FROM || !RANGE_FMT_TO) { label.textContent = ""; return; }
    var first = parseBarDate(visible[0].dataset.barHourIso);
    var last = parseBarDate(visible[visible.length - 1].dataset.barHourIso);
    if (!first || !last) { label.textContent = ""; return; }
    label.textContent = RANGE_FMT_FROM.format(first) + " – " + RANGE_FMT_TO.format(last);
  }

  function updatePagerButtons() {
    var older = document.getElementById("pager-older");
    var newer = document.getElementById("pager-newer");
    if (older) older.disabled = pageIndex >= maxPageIndex();
    if (newer) newer.disabled = pageIndex <= 0;
  }

  function initPager() {
    var pager = document.getElementById("strip-pager");
    var older = document.getElementById("pager-older");
    var newer = document.getElementById("pager-newer");
    if (!pager || !older || !newer || !totalBars()) return; // no models → stay hidden
    older.addEventListener("click", function () {
      pageIndex = Math.min(maxPageIndex(), pageIndex + 1); // ‹ = older
      applyPage();
    });
    newer.addEventListener("click", function () {
      pageIndex = Math.max(0, pageIndex - 1); // › = newer
      applyPage();
    });
    pager.hidden = false;
    applyPage();
  }

  // Script is deferred, so the DOM is parsed; wait for load so layout is ready.
  window.addEventListener("load", function () {
    localizeTimes();
    initPager();
  });

  // --- Tooltip ---
  var tooltip = document.getElementById("bar-tooltip");
  if (!tooltip) return;

  var pinned = false;

  function showFor(bar, evt) {
    // Prefer the viewer's local time; fall back to the server's UTC label.
    var when = bar.dataset.barHour || bar.dataset.barDate || "";
    var localDate = HOUR_FMT && parseBarDate(bar.dataset.barHourIso);
    if (localDate) when = HOUR_FMT.format(localDate);
    var state = bar.dataset.barState || "";
    var uptime = bar.dataset.barUptime || "";
    var endpoint = bar.dataset.barEndpoint;
    var detail = bar.dataset.barDetail; // why a non-healthy hour is non-healthy

    // Healthy bars get a minimal tooltip (hour + uptime); non-healthy add the
    // reason (e.g. "Elevated latency · p95 8.20s (SLO 5.00s)").
    var stateClass = state === "down" ? "down" : "";
    var stateLabel = state.charAt(0).toUpperCase() + state.slice(1);
    var html = '<strong class="' + stateClass + '">' + stateLabel + "</strong><br>";
    html += when + "<br>";
    if (detail) html += detail + "<br>";
    html += "Uptime: " + uptime;
    if (endpoint) html += "<br>Endpoint: " + endpoint;
    tooltip.innerHTML = html;
    tooltip.hidden = false;

    var rect = bar.getBoundingClientRect();
    var tipRect = tooltip.getBoundingClientRect();
    var x = rect.left + window.scrollX + (rect.width / 2) - (tipRect.width / 2);
    var y = rect.top + window.scrollY - tipRect.height - 8;
    tooltip.style.left = Math.max(8, x) + "px";
    tooltip.style.top = Math.max(8, y) + "px";
  }

  function hide() {
    if (pinned) return;
    tooltip.hidden = true;
  }

  function onMouseOver(evt) {
    var bar = evt.target.closest(".bar");
    if (!bar || !bar.dataset.barState) return;
    showFor(bar, evt);
  }

  function onMouseOut(evt) {
    var bar = evt.target.closest(".bar");
    if (!bar) return;
    hide();
  }

  function onClick(evt) {
    var bar = evt.target.closest(".bar");
    if (bar && bar.dataset.barState && bar.dataset.barState !== "healthy") {
      pinned = true;
      tooltip.style.pointerEvents = "auto";
      showFor(bar, evt);
      evt.stopPropagation();
      return;
    }
    if (pinned) {
      pinned = false;
      tooltip.style.pointerEvents = "none";
      hide();
    }
  }

  document.addEventListener("mouseover", onMouseOver);
  document.addEventListener("mouseout", onMouseOut);
  document.addEventListener("click", onClick);
})();
