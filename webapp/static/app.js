(function () {
  "use strict";

  var tg = window.Telegram && window.Telegram.WebApp;
  var params = new URLSearchParams(location.search);
  var requestToken = params.get("r") || "";
  var HEARTBEAT_MS = 15000;

  var el = function (id) { return document.getElementById(id); };
  var grid = el("grid"), msg = el("msg"), check = el("check");
  var attemptId = null, picked = [], shownAt = 0, hadTouch = false, busy = false, done = false;

  document.addEventListener("touchstart", function () { hadTouch = true; }, { passive: true });

  function haptic(kind, value) {
    try { tg.HapticFeedback[kind](value); } catch (e) { /* older clients */ }
  }

  function api(path, body) {
    body = Object.assign({ r: requestToken }, body || {});
    return fetch("/api/" + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "tma " + (tg ? tg.initData : "") },
      body: JSON.stringify(body)
    }).then(function (res) {
      if (!res.ok) return res.text().then(function (t) { throw new Error(t || res.statusText); });
      return res.json();
    });
  }

  function setChat(data) {
    if (data && data.chat_title) el("chat").textContent = "Joining " + data.chat_title;
  }

  function showState(icon, text, tone, link) {
    done = true;
    el("challenge").hidden = true;
    el("manual-box").hidden = true;
    el("state").hidden = false;
    el("state").dataset.tone = tone;
    el("state-icon").textContent = icon;
    el("state-text").textContent = text;
    el("title").hidden = el("sub").hidden = true;
    var a = el("state-link");
    a.hidden = !link;
    if (link) a.href = link;
    if (tg) tg.disableClosingConfirmation();
  }

  function renderTiles(data) {
    grid.innerHTML = "";
    grid.style.setProperty("--cols", data.columns || 3);
    picked = [];
    data.tiles.forEach(function (tile, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "tile";
      b.setAttribute("aria-pressed", "false");
      b.setAttribute("aria-label", "Tile " + (i + 1));
      var img = document.createElement("img");
      img.alt = "";
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(tile.svg);
      b.appendChild(img);
      b.addEventListener("click", function () { toggle(b, tile.id); });
      grid.appendChild(b);
    });
    grid.classList.remove("waiting");
    shownAt = performance.now();
    updateCheck();
  }

  function toggle(button, id) {
    if (busy) return;
    var at = picked.indexOf(id);
    if (at >= 0) {
      picked.splice(at, 1);
      button.setAttribute("aria-pressed", "false");
    } else if (picked.length < 2) {
      picked.push(id);
      button.setAttribute("aria-pressed", "true");
    }
    haptic("selectionChanged");
    updateCheck();
  }

  function updateCheck() {
    check.disabled = picked.length !== 2 || busy;
    check.textContent = picked.length === 2 ? "Check" : "Pick " + (2 - picked.length) + " more";
  }

  function countdown(seconds, suggestManual) {
    grid.classList.add("waiting");
    el("manual-hint").hidden = !suggestManual;
    var left = seconds;
    (function tick() {
      if (left <= 0) { load(); return; }
      msg.textContent = "New grid in " + left + " s";
      left -= 1;
      setTimeout(tick, 1000);
    })();
  }

  function handle(data) {
    setChat(data);
    switch (data.status) {
      case "challenge":
        attemptId = data.attempt;
        el("challenge").hidden = false;
        el("manual-box").hidden = false;
        el("manual-hint").hidden = !data.suggest_manual;
        renderTiles(data);
        if (!data.request_open) msg.textContent = "Your join request already timed out. Passing still verifies you for next time.";
        break;
      case "wait":
        el("challenge").hidden = false;
        el("manual-box").hidden = false;
        countdown(data.wait, data.suggest_manual);
        break;
      case "wrong":
        haptic("notificationOccurred", "error");
        msg.textContent = "Not quite. Here's a new grid.";
        msg.dataset.tone = "bad";
        load(true);
        break;
      case "approved":
        haptic("notificationOccurred", "success");
        showState("✓", "You're in" + (data.chat_title ? ". Welcome to " + data.chat_title + "." : "."), "good");
        setTimeout(function () { if (tg) tg.close(); }, 1600);
        break;
      case "verified_late":
        haptic("notificationOccurred", "success");
        showState("✓", "Verified. Your join request had already timed out, so request to join again and you'll be let in straight away.", "good", data.chat_link);
        break;
      case "queued":
        showState("⌛", "Sent to the admins" + (data.chat_title ? " of " + data.chat_title : "") + ". They'll decide on your request.", "neutral");
        break;
      default:
        showState("!", "This request is closed.", "neutral");
    }
  }

  function load(keepMessage) {
    busy = true;
    return api("challenge").then(function (data) {
      busy = false;
      if (!keepMessage && data.status === "challenge") { msg.textContent = ""; delete msg.dataset.tone; }
      handle(data);
    }).catch(fail);
  }

  function fail(err) {
    busy = false;
    showState("!", "Something went wrong: " + err.message, "bad");
  }

  check.addEventListener("click", function () {
    if (picked.length !== 2 || busy) return;
    busy = true;
    updateCheck();
    api("answer", {
      attempt: attemptId,
      picked: picked,
      solve_ms: Math.round(performance.now() - shownAt),
      had_touch: hadTouch
    }).then(function (data) { busy = false; handle(data); }).catch(fail);
  });

  el("manual").addEventListener("click", function () {
    if (busy) return;
    busy = true;
    api("manual").then(function (data) { busy = false; handle(data); }).catch(fail);
  });

  // 5-minute grace period: the server runs the timer, the app just reports (design §5).
  function report(type) {
    if (done) return;
    api("event", { type: type }).catch(function () { /* next heartbeat retries */ });
  }

  if (!tg || !tg.initData) {
    showState("!", "Open this page from Telegram.", "neutral");
    return;
  }

  tg.ready();
  tg.expand();
  tg.enableClosingConfirmation();
  tg.onEvent("deactivated", function () { report("deactivated"); });
  tg.onEvent("activated", function () { report("activated"); });
  setInterval(function () { report("heartbeat"); }, HEARTBEAT_MS);

  load();
})();
