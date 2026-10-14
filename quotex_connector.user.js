// ==UserScript==
// @name         QuantTrader Quotex Live Bridge & AI Predictor
// @namespace    http://localhost:8000/
// @version      2.0
// @description  Stream real-time Quotex ticks/candles to QuantTrader AI terminal & display predictions directly on Quotex chart.
// @author       QuantTrader
// @match        *://qxbroker.com/*
// @match        *://*.quotex.com/*
// @match        *://*.quotex-broker.com/*
// @match        *://quotex.market/*
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(function() {
    'use strict';

    const API_URL = "http://127.0.0.1:8000/api/quotex/feed";
    let isConnected = false;
    let lastSignal = { action: "WAIT", confidence: 0.0, reasons: [] };
    let currentAsset = "QUOTEX-CHART";
    let activeCandles = [];
    let currentBar = null; // { time, open, high, low, close, volume }
    let lastSentTime = 0;
    let autoTradeInBrowser = false;
    let lastClickTime = 0;

    console.log("%c[QuantTrader]%c Quotex Real-Time Bridge Initializing...", "background: #6366f1; color: white; padding: 2px 6px; border-radius: 4px;", "");

    // -------------------------------------------------------------
    // 1. FLOATING HUD OVERLAY ON QUOTEX
    // -------------------------------------------------------------
    function createHUD() {
        if (document.getElementById("quant-trader-hud")) return;

        const hud = document.createElement("div");
        hud.id = "quant-trader-hud";
        hud.innerHTML = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
                <div style="display:flex;align-items:center;gap:6px;">
                    <div id="qt-status-dot" style="width:10px;height:10px;border-radius:50%;background:#ef4444;box-shadow:0 0 8px #ef4444;"></div>
                    <span style="font-weight:700;font-size:12px;letter-spacing:0.5px;color:#f8fafc;">QuantTrader AI</span>
                </div>
                <span id="qt-asset" style="font-size:10px;padding:2px 6px;background:rgba(255,255,255,0.08);border-radius:4px;color:#94a3b8;font-family:monospace;">CONNECTING</span>
            </div>
            <div style="background:rgba(15,23,42,0.6);border-radius:6px;padding:8px;margin-bottom:8px;text-align:center;">
                <div style="font-size:11px;color:#94a3b8;margin-bottom:2px;">AI PREDICTION (1M)</div>
                <div id="qt-signal" style="font-size:18px;font-weight:800;color:#94a3b8;">WAIT</div>
                <div id="qt-conf" style="font-size:12px;font-family:monospace;color:#cbd5e1;margin-top:2px;">Confidence: 0%</div>
            </div>
            <div id="qt-reasons" style="font-size:10px;color:#64748b;max-height:42px;overflow-y:auto;margin-bottom:8px;line-height:1.3;">
                Waiting for 1m candle sync...
            </div>
            <div style="display:flex;gap:6px;align-items:center;justify-content:space-between;font-size:11px;color:#94a3b8;border-top:1px solid rgba(255,255,255,0.08);padding-top:6px;">
                <label style="display:flex;align-items:center;gap:4px;cursor:pointer;">
                    <input type="checkbox" id="qt-auto-dom-click" style="cursor:pointer;" />
                    <span>Auto-Click Web</span>
                </label>
                <a href="http://127.0.0.1:8000" target="_blank" style="color:#818cf8;text-decoration:none;font-weight:600;">Open Terminal ↗</a>
            </div>
        `;

        Object.assign(hud.style, {
            position: "fixed",
            top: "70px",
            right: "260px",
            width: "220px",
            background: "rgba(10, 15, 29, 0.92)",
            backdropFilter: "blur(12px)",
            border: "1px solid rgba(99, 102, 241, 0.3)",
            borderRadius: "10px",
            padding: "12px",
            zIndex: "999999",
            color: "#e2e8f0",
            fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
            boxShadow: "0 10px 25px -5px rgba(0, 0, 0, 0.7), 0 0 15px rgba(99, 102, 241, 0.2)",
            userSelect: "none"
        });

        document.body.appendChild(hud);

        document.getElementById("qt-auto-dom-click").addEventListener("change", (e) => {
            autoTradeInBrowser = e.target.checked;
        });

        // Make HUD draggable
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

    function updateHUD(status, signal, confidence, reasons, asset) {
        createHUD();
        const dot = document.getElementById("qt-status-dot");
        const sigEl = document.getElementById("qt-signal");
        const confEl = document.getElementById("qt-conf");
        const reasonsEl = document.getElementById("qt-reasons");
        const assetEl = document.getElementById("qt-asset");

        if (dot) {
            dot.style.background = status ? "#10b981" : "#ef4444";
            dot.style.boxShadow = status ? "0 0 8px #10b981" : "0 0 8px #ef4444";
        }
        if (assetEl && asset) assetEl.innerText = asset.toUpperCase();

        if (sigEl) {
            sigEl.innerText = signal;
            if (signal === "CALL") {
                sigEl.style.color = "#10b981";
            } else if (signal === "PUT") {
                sigEl.style.color = "#ef4444";
            } else {
                sigEl.style.color = "#94a3b8";
            }
        }
        if (confEl) {
            confEl.innerText = `Confidence: ${(confidence * 100).toFixed(1)}%`;
        }
        if (reasonsEl && reasons && reasons.length) {
            reasonsEl.innerHTML = reasons.map(r => `• ${r}`).join("<br/>");
        }
    }

    // -------------------------------------------------------------
    // 2. CANDLE AGGREGATION & TRANSMISSION TO PYTHON BACKEND
    // -------------------------------------------------------------
    function addTick(price, timestampMs) {
        if (!price || isNaN(price)) return;
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

        const now = Date.now();
        if (now - lastSentTime >= 1000 && activeCandles.length >= 10) {
            lastSentTime = now;
            sendToQuantTrader();
        }
    }

    function sendToQuantTrader() {
        const payload = {
            source: "quotex",
            asset: currentAsset,
            candles: [...activeCandles, { ...currentBar }],
            current_price: currentBar ? currentBar.close : null,
            timestamp: Math.floor(Date.now() / 1000)
        };

        const postData = JSON.stringify(payload);

        const handleSuccess = (respText) => {
            try {
                const res = JSON.parse(respText);
                isConnected = true;
                if (res.signal) {
                    lastSignal = res.signal;
                    updateHUD(true, res.signal.action, res.signal.confidence, res.signal.reasons, currentAsset);
                    
                    // Optional In-Browser DOM Triggering (alternative to desktop mouse clicks)
                    if (autoTradeInBrowser && (res.signal.action === "CALL" || res.signal.action === "PUT")) {
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
                onerror: () => updateHUD(false, "DISCONNECTED", 0.0, ["Cannot connect to http://127.0.0.1:8000. Is app.py running?"], currentAsset)
            });
        } else {
            fetch(API_URL, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: postData
            })
            .then(r => r.text())
            .then(handleSuccess)
            .catch(() => updateHUD(false, "DISCONNECTED", 0.0, ["Cannot connect to http://127.0.0.1:8000. Is app.py running?"], currentAsset));
        }
    }

    // Optional direct DOM clicker inside the webpage
    function checkAndClickDOM(direction, confidence) {
        const now = Date.now();
        if (now - lastClickTime < 3000) return; // debounce 3s
        if (confidence < 0.65) return;

        let btn = null;
        if (direction === "CALL") {
            btn = document.querySelector(".btn-call, [data-qa='deal-call'], .call-btn, .button-up");
        } else if (direction === "PUT") {
            btn = document.querySelector(".btn-put, [data-qa='deal-put'], .put-btn, .button-down");
        }

        if (btn) {
            btn.click();
            lastClickTime = now;
            console.log(`%c[QuantTrader Auto-Click]%c ${direction} triggered on Quotex!`, "background:#10b981;color:white;font-weight:bold;", "");
        }
    }

    // -------------------------------------------------------------
    // 3. WEBSOCKET & DOM SNIFFING
    // -------------------------------------------------------------
    // Hook native WebSocket to capture Quotex tick stream
    const OrigWebSocket = window.WebSocket;
    window.WebSocket = function(...args) {
        const ws = new OrigWebSocket(...args);
        console.log("[QuantTrader] WebSocket hooked to:", args[0]);

        ws.addEventListener("message", function(e) {
            try {
                if (typeof e.data === "string") {
                    // Try parsing JSON or socket.io packet (e.g. 42["tick", ...])
                    let raw = e.data;
                    if (raw.startsWith("42")) raw = raw.slice(2);
                    if (raw.startsWith("[") || raw.startsWith("{")) {
                        const parsed = JSON.parse(raw);
                        parseQuotexMessage(parsed);
                    }
                }
            } catch (err) {}
        });

        return ws;
    };
    window.WebSocket.prototype = OrigWebSocket.prototype;

    function parseQuotexMessage(msg) {
        // Quotex message patterns: ["s", { "live": [price, timestamp] }] or { "price": ... }
        if (Array.isArray(msg)) {
            const topic = msg[0];
            const data = msg[1];
            if ((topic === "s" || topic === "live" || topic === "tick") && data) {
                if (Array.isArray(data)) {
                    addTick(Number(data[1] || data[0]));
                } else if (data.price) {
                    addTick(Number(data.price), data.time ? data.time * 1000 : null);
                } else if (data.live) {
                    addTick(Number(data.live));
                }
            }
        } else if (msg && msg.price) {
            addTick(Number(msg.price));
        }
    }

    // Fallback: DOM Poller (Extracts asset name and active price tag if WebSocket format changes)
    setInterval(() => {
        // 1. Detect Asset Name
        const assetEl = document.querySelector(".asset-select__name, .current-symbol, .trading-chart-header__symbol, [data-qa='asset-name']");
        if (assetEl && assetEl.innerText) {
            const clean = assetEl.innerText.replace(/[\n\r]+/g, ' ').trim();
            if (clean && clean !== currentAsset) {
                currentAsset = clean;
                activeCandles = []; // reset for new asset
            }
        }

        // 2. Detect Live Price from Chart or Deal Panel
        const priceEls = document.querySelectorAll(".deal-form__current-quote, .current-price, .chart-quote, .section-deal__rate");
        for (const el of priceEls) {
            const txt = el.innerText ? el.innerText.replace(/[^0-9.]/g, '') : "";
            const num = parseFloat(txt);
            if (!isNaN(num) && num > 0) {
                addTick(num);
                break;
            }
        }
    }, 250);

    // Initial HUD Mount
    setTimeout(createHUD, 1500);

})();
