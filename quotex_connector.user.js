// ==UserScript==
// @name         QuantTrader Quotex Live Bridge & AI Predictor
// @namespace    http://localhost:8000/
// @version      3.0
// @description  Stream real-time Quotex ticks & candles to QuantTrader AI terminal & display predictions directly on Quotex chart.
// @author       QuantTrader
// @match        *://market-qx.info/*
// @match        *://*.market-qx.info/*
// @match        *://market-qx.pro/*
// @match        *://*.market-qx.pro/*
// @match        *://qxbroker.com/*
// @match        *://*.qxbroker.com/*
// @match        *://*.quotex.com/*
// @match        *://*.quotex-broker.com/*
// @match        *://quotex.market/*
// @match        *://*.quotex.*/*
// @match        *://*/*
// @run-at       document-start
// @grant        unsafeWindow
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(function() {
    'use strict';

    // Only run on Quotex/QX domains
    const host = window.location.hostname.toLowerCase();
    const isQuotex = host.includes("quotex") || host.includes("qx") || host.includes("broker") || host.includes("market-qx");
    if (!isQuotex) return;

    const API_URL = "http://127.0.0.1:8000/api/quotex/feed";
    let isConnected = false;
    let currentAsset = "AUD/JPY";
    let activeCandles = [];
    let currentBar = null;
    let autoTradeInBrowser = false;
    let lastClickTime = 0;
    let lastRecordedPrice = null;

    console.log("%c[QuantTrader]%c Quotex Live Bridge 3.0 Active on " + host, "background: #6366f1; color: white; padding: 2px 6px; border-radius: 4px;", "");

    // -------------------------------------------------------------
    // 1. FLOATING HUD OVERLAY ON QUOTEX
    // -------------------------------------------------------------
    function createHUD() {
        if (document.getElementById("quant-trader-hud")) return;
        const target = document.body || document.documentElement;
        if (!target) return;

        const hud = document.createElement("div");
        hud.id = "quant-trader-hud";
        hud.innerHTML = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;cursor:move;">
                <div style="display:flex;align-items:center;gap:6px;">
                    <div id="qt-status-dot" style="width:10px;height:10px;border-radius:50%;background:#ef4444;box-shadow:0 0 8px #ef4444;transition:all 0.3s ease;"></div>
                    <span style="font-weight:700;font-size:12px;letter-spacing:0.5px;color:#f8fafc;">QuantTrader AI</span>
                </div>
                <span id="qt-asset" style="font-size:10px;padding:2px 6px;background:rgba(255,255,255,0.08);border-radius:4px;color:#cbd5e1;font-family:monospace;font-weight:600;">CONNECTING...</span>
            </div>
            <div style="background:rgba(15,23,42,0.7);border-radius:8px;padding:8px;margin-bottom:8px;text-align:center;">
                <div style="font-size:10px;color:#94a3b8;margin-bottom:2px;letter-spacing:0.5px;">AI PREDICTION (1M)</div>
                <div id="qt-signal" style="font-size:22px;font-weight:900;color:#fbbf24;letter-spacing:1px;">WAIT</div>
                <div style="display:flex;justify-content:center;gap:10px;margin-top:4px;font-size:11px;font-family:monospace;">
                    <span id="qt-call-prob" style="color:#10b981;font-weight:700;">CALL: --%</span>
                    <span style="color:#475569;">|</span>
                    <span id="qt-put-prob" style="color:#f43f5e;font-weight:700;">PUT: --%</span>
                </div>
                <div id="qt-target" style="font-size:10px;color:#818cf8;margin-top:3px;font-family:monospace;">Target: ≥58%</div>
                <div id="qt-price" style="font-size:10px;color:#94a3b8;margin-top:2px;font-family:monospace;">Price: --</div>
            </div>
            <div id="qt-reasons" style="font-size:10px;color:#64748b;max-height:45px;overflow-y:auto;margin-bottom:8px;line-height:1.3;">
                Connecting to QuantTrader AI terminal...
            </div>
            <div style="display:flex;gap:6px;align-items:center;justify-content:space-between;font-size:11px;color:#94a3b8;border-top:1px solid rgba(255,255,255,0.08);padding-top:6px;">
                <label style="display:flex;align-items:center;gap:4px;cursor:pointer;">
                    <input type="checkbox" id="qt-auto-dom-click" style="cursor:pointer;" />
                    <span>Auto-Click Web</span>
                </label>
                <a href="http://127.0.0.1:8000" target="_blank" style="color:#818cf8;text-decoration:none;font-weight:600;">Terminal ↗</a>
            </div>
        `;

        Object.assign(hud.style, {
            position: "fixed",
            top: "70px",
            left: "70px",
            width: "240px",
            background: "rgba(10, 15, 29, 0.96)",
            backdropFilter: "blur(14px)",
            border: "1px solid rgba(99, 102, 241, 0.5)",
            borderRadius: "10px",
            padding: "12px",
            zIndex: "2147483647",
            color: "#e2e8f0",
            fontFamily: "system-ui, -apple-system, sans-serif",
            boxShadow: "0 10px 30px -5px rgba(0, 0, 0, 0.9), 0 0 15px rgba(99, 102, 241, 0.35)",
            userSelect: "none"
        });

        target.appendChild(hud);

        document.getElementById("qt-auto-dom-click").addEventListener("change", (e) => {
            autoTradeInBrowser = e.target.checked;
        });

        // Draggable HUD
        let isDragging = false, startX, startY, initialX, initialY;
        hud.addEventListener("mousedown", (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'A') return;
            isDragging = true;
            startX = e.clientX;
            startY = e.clientY;
            initialX = hud.offsetLeft;
            initialY = hud.offsetTop;
        });
        document.addEventListener("mousemove", (e) => {
            if (!isDragging) return;
            hud.style.left = `${initialX + (e.clientX - startX)}px`;
            hud.style.top = `${initialY + (e.clientY - startY)}px`;
            hud.style.right = "auto";
        });
        document.addEventListener("mouseup", () => { isDragging = false; });
    }

    function updateHUD(status, signal, confidence, reasons, asset, price, ml_prob, threshold, open_trade) {
        createHUD();
        const dot = document.getElementById("qt-status-dot");
        const sigEl = document.getElementById("qt-signal");
        const callProbEl = document.getElementById("qt-call-prob");
        const putProbEl = document.getElementById("qt-put-prob");
        const targetEl = document.getElementById("qt-target");
        const priceEl = document.getElementById("qt-price");
        const reasonsEl = document.getElementById("qt-reasons");
        const assetEl = document.getElementById("qt-asset");

        if (dot) {
            dot.style.background = status ? "#10b981" : "#ef4444";
            dot.style.boxShadow = status ? "0 0 8px #10b981" : "0 0 8px #ef4444";
        }
        if (assetEl && asset) assetEl.innerText = asset.toUpperCase();
        if (priceEl && price) priceEl.innerText = `Price: ${price}`;

        const thrVal = threshold ? Math.round(threshold * 100) : 58;
        if (targetEl) targetEl.innerText = `Target to Trade: ≥${thrVal}%`;

        if (ml_prob !== null && ml_prob !== undefined) {
            const callP = (ml_prob * 100).toFixed(1);
            const putP = ((1.0 - ml_prob) * 100).toFixed(1);
            if (callProbEl) callProbEl.innerText = `CALL: ${callP}%`;
            if (putProbEl) putProbEl.innerText = `PUT: ${putP}%`;
        }

        if (sigEl) {
            if (open_trade) {
                sigEl.innerText = `IN TRADE (${open_trade.dir})`;
                sigEl.style.color = "#38bdf8";
            } else {
                sigEl.innerText = signal;
                if (signal === "CALL") {
                    sigEl.style.color = "#10b981";
                } else if (signal === "PUT") {
                    sigEl.style.color = "#f43f5e";
                } else {
                    sigEl.style.color = "#fbbf24";
                }
            }
        }
        if (reasonsEl && reasons && reasons.length) {
            reasonsEl.innerHTML = reasons.map(r => `• ${r}`).join("<br/>");
        }
    }

    // -------------------------------------------------------------
    // 2. TICK & CANDLE AGGREGATION
    // -------------------------------------------------------------
    function addTick(price, timestampMs) {
        if (!price || isNaN(price) || price <= 0) return;
        lastRecordedPrice = price;
        const ts = timestampMs || Date.now();
        const barSec = Math.floor(ts / 1000 / 60) * 60; // 1-minute bucket (unix seconds)

        if (!currentBar || currentBar.time !== barSec) {
            if (currentBar) {
                activeCandles.push({ ...currentBar });
                if (activeCandles.length > 250) activeCandles.shift();
            }
            currentBar = {
                time: barSec,
                open: price,
                high: price,
                low: price,
                close: price,
                volume: 1.0
            };
        } else {
            currentBar.high = Math.max(currentBar.high, price);
            currentBar.low = Math.min(currentBar.low, price);
            currentBar.close = price;
            currentBar.volume += 1.0;
        }
    }

    function sendToQuantTrader() {
        const candlesPayload = currentBar ? [...activeCandles, { ...currentBar }] : activeCandles;
        const payload = {
            source: "quotex",
            asset: currentAsset,
            candles: candlesPayload,
            current_price: lastRecordedPrice,
            timestamp: Math.floor(Date.now() / 1000)
        };

        const postData = JSON.stringify(payload);

        const handleSuccess = (respText) => {
            try {
                const res = JSON.parse(respText);
                isConnected = true;
                if (res.signal) {
                    updateHUD(
                        true, 
                        res.signal.action, 
                        res.signal.confidence, 
                        res.signal.reasons, 
                        currentAsset, 
                        lastRecordedPrice, 
                        res.signal.ml_prob, 
                        res.signal.threshold,
                        res.open_trade
                    );
                    if (autoTradeInBrowser && !res.open_trade && (res.signal.action === "CALL" || res.signal.action === "PUT")) {
                        checkAndClickDOM(res.signal.action, res.signal.confidence);
                    }
                }
            } catch (e) {
                console.error("[QuantTrader] Parse error:", e);
            }
        };

        if (typeof GM_xmlhttpRequest !== "undefined") {
            GM_xmlhttpRequest({
                method: "POST",
                url: API_URL,
                headers: { "Content-Type": "application/json" },
                data: postData,
                onload: (response) => handleSuccess(response.responseText),
                onerror: () => {
                    // Try fetch fallback if GM_xmlhttpRequest is blocked
                    fetch(API_URL, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: postData
                    })
                    .then(r => r.text())
                    .then(handleSuccess)
                    .catch(() => updateHUD(false, "DISCONNECTED", 0.0, ["Tampermonkey: Click icon -> Always Allow 127.0.0.1", "Or ensure 'python app.py' is running."], currentAsset, lastRecordedPrice));
                }
            });
        } else {
            fetch(API_URL, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: postData
            })
            .then(r => r.text())
            .then(handleSuccess)
            .catch(() => updateHUD(false, "DISCONNECTED", 0.0, ["Start terminal: python app.py"], currentAsset, lastRecordedPrice));
        }
    }

    function checkAndClickDOM(direction, confidence) {
        const now = Date.now();
        if (now - lastClickTime < 3000) return;
        if (confidence < 0.55) return;

        let btn = null;
        if (direction === "CALL") {
            btn = document.querySelector(".btn-call, [data-qa='deal-call'], .call-btn, .button-up, button.call, .button--success");
            if (!btn) {
                const buttons = document.querySelectorAll("button, .btn, div[role='button']");
                for (const b of buttons) {
                    const txt = (b.innerText || "").trim().toLowerCase();
                    if (txt === "buy" || txt === "call" || txt === "up" || txt.startsWith("buy\n")) {
                        btn = b; break;
                    }
                }
            }
        } else if (direction === "PUT") {
            btn = document.querySelector(".btn-put, [data-qa='deal-put'], .put-btn, .button-down, button.put, .button--danger");
            if (!btn) {
                const buttons = document.querySelectorAll("button, .btn, div[role='button']");
                for (const b of buttons) {
                    const txt = (b.innerText || "").trim().toLowerCase();
                    if (txt === "sell" || txt === "put" || txt === "down" || txt.startsWith("sell\n")) {
                        btn = b; break;
                    }
                }
            }
        }

        if (btn) {
            btn.click();
            lastClickTime = now;
            console.log(`%c[QuantTrader Auto-Click]%c ${direction} clicked via DOM at price ${lastRecordedPrice}!`, "background: #10b981; color: white;", "");
        }
    }

    // -------------------------------------------------------------
    // 3. WEBSOCKET HOOKING (Both unsafeWindow & window)
    // -------------------------------------------------------------
    const win = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
    if (win && win.WebSocket) {
        const OrigWebSocket = win.WebSocket;
        win.WebSocket = function(...args) {
            const ws = new OrigWebSocket(...args);
            console.log("[QuantTrader] Quotex WebSocket hooked:", args[0]);

            ws.addEventListener("message", function(e) {
                try {
                    if (typeof e.data === "string") {
                        let raw = e.data;
                        if (raw.startsWith("42")) raw = raw.slice(2);
                        if (raw.startsWith("[") || raw.startsWith("{")) {
                            const parsed = JSON.parse(raw);
                            if (Array.isArray(parsed)) {
                                const d = parsed[1];
                                if (d && typeof d === 'object') {
                                    if (d.price) addTick(Number(d.price));
                                    else if (d.live) addTick(Number(d.live));
                                    else if (d.rate) addTick(Number(d.rate));
                                    else if (Array.isArray(d)) addTick(Number(d[1] || d[0]));
                                }
                            } else if (parsed && typeof parsed === 'object') {
                                if (parsed.price) addTick(Number(parsed.price));
                                else if (parsed.rate) addTick(Number(parsed.rate));
                            }
                        }
                    }
                } catch (err) {}
            });

            return ws;
        };
        win.WebSocket.prototype = OrigWebSocket.prototype;
    }

    // -------------------------------------------------------------
    // 4. ROBUST DOM TICK & ASSET SCANNER
    // -------------------------------------------------------------
    function scanQuotexDOM() {
        // 1. Detect active asset name (e.g. AUD/JPY, USD/JPY, EUR/USD, EUR/USD (OTC))
        let foundAsset = null;
        const pairRegex = /\b([A-Z]{3}\/[A-Z]{3}(?:\s*\(OTC\))?)\b/i;

        // Method A: Active tab
        const activeTabs = document.querySelectorAll(".tabs__item--active, .tab--active, .tab.active, [class*='tab'][class*='active'], [class*='tab-item--active']");
        for (const t of activeTabs) {
            const m = (t.innerText || "").match(pairRegex);
            if (m && m[1]) {
                foundAsset = m[1].toUpperCase();
                break;
            }
        }

        // Method B: Right deal/trade sidebar
        if (!foundAsset) {
            const dealForms = document.querySelectorAll(".section-deal, [class*='deal-form'], [class*='sidebar__asset'], [class*='asset-select'], [data-qa='asset-name'], [class*='current-symbol']");
            for (const df of dealForms) {
                const m = (df.innerText || "").match(pairRegex);
                if (m && m[1]) {
                    foundAsset = m[1].toUpperCase();
                    break;
                }
            }
        }

        // Method C: Any header or tabs container
        if (!foundAsset) {
            const headers = document.querySelectorAll(".header-chart, header, .tabs, [class*='tabs']");
            for (const h of headers) {
                const m = (h.innerText || "").match(pairRegex);
                if (m && m[1]) {
                    foundAsset = m[1].toUpperCase();
                    break;
                }
            }
        }

        if (foundAsset) {
            const clean = foundAsset.trim();
            if (clean && clean !== currentAsset) {
                currentAsset = clean;
                console.log("[QuantTrader] Switched Active Asset to:", currentAsset);
            }
        }

        // 2. Check "PAIR INFORMATION: 1.11921" or direct price elements
        const allSpans = document.querySelectorAll("span, div, p");
        let foundPrice = null;

        for (const el of allSpans) {
            if (el.innerText && el.innerText.includes("PAIR INFORMATION:")) {
                const match = el.innerText.match(/PAIR INFORMATION:\s*([0-9.]+)/i);
                if (match && match[1]) {
                    foundPrice = parseFloat(match[1]);
                    break;
                }
            }
        }

        // 3. Fallback: Check right price axis bubble or current-quote
        if (!foundPrice) {
            const quoteEls = document.querySelectorAll(".deal-form__current-quote, .current-price, .chart-quote, .section-deal__rate, [data-qa='current-quote'], [class*='current-quote'], [class*='price-current'], [class*='rate-value']");
            for (const el of quoteEls) {
                const txt = el.innerText ? el.innerText.replace(/[^0-9.]/g, '') : "";
                const num = parseFloat(txt);
                if (!isNaN(num) && num > 0) {
                    foundPrice = num;
                    break;
                }
            }
        }

        if (foundPrice && !isNaN(foundPrice)) {
            addTick(foundPrice);
        }
    }

    // Run DOM scanner every 200ms
    setInterval(scanQuotexDOM, 200);

    // Heartbeat every 1000ms: Guaranteed constant sync with QuantTrader AI engine
    setInterval(sendToQuantTrader, 1000);

    // Ensure HUD is rendered
    setInterval(createHUD, 1000);

})();
