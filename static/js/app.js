/* Shared front-end helpers exposed as window.Conduction. See templates/base.html.
   NOTE: contract named this static/js/app.js; the harness claim gate could not create
   the static/js/ directory, so it is served from /static/app.js instead. */
(function () {
    "use strict";

    /** HTML-escape any value for safe interpolation into innerHTML. */
    function escapeHtml(value) {
        if (value === null || value === undefined) return "";
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    /** fetch + JSON, throwing an Error carrying the server's error text. */
    async function fetchJson(url, options) {
        const response = await fetch(url, options);
        const text = await response.text();
        let data = null;
        try { data = text ? JSON.parse(text) : null; } catch (err) { data = null; }
        if (!response.ok) {
            const detail = (data && (data.error || data.message)) || text || response.statusText;
            const error = new Error(detail);
            error.status = response.status;
            error.data = data;
            throw error;
        }
        return data;
    }

    /**
     * EventSource with named-event handlers and auto-reconnect.
     * handlers: {eventName: fn(data, rawEvent), open?, error?, status?(state)}
     * Returns {close()}.
     */
    function openStream(url, handlers) {
        handlers = handlers || {};
        let source = null;
        let closed = false;
        let retryDelay = 1000;

        function setStatus(state) {
            if (typeof handlers.status === "function") handlers.status(state);
            document.querySelectorAll("[data-connection-badge]").forEach(function (node) {
                node.textContent = state;
                node.className = "connection-badge " + state;
            });
        }

        function connect() {
            if (closed) return;
            source = new EventSource(url);
            source.onopen = function () {
                retryDelay = 1000;
                setStatus("connected");
                if (handlers.open) handlers.open();
            };
            source.onerror = function (event) {
                setStatus("disconnected");
                if (handlers.error) handlers.error(event);
                if (source) source.close();
                if (closed) return;
                setTimeout(connect, retryDelay);
                retryDelay = Math.min(retryDelay * 2, 15000);
            };
            Object.keys(handlers).forEach(function (name) {
                if (name === "open" || name === "error" || name === "status") return;
                source.addEventListener(name, function (event) {
                    let payload = event.data;
                    try { payload = JSON.parse(event.data); } catch (err) { /* keep raw */ }
                    handlers[name](payload, event);
                });
            });
        }

        connect();
        return {
            close: function () {
                closed = true;
                if (source) source.close();
            }
        };
    }

    const PILL_CLASSES = {
        running: "pill-running",
        completed: "pill-completed",
        passed: "pill-passed",
        ok: "pill-passed",
        failed: "pill-failed",
        errored: "pill-failed",
        error: "pill-failed",
        stale: "pill-stale",
        pending: "pill-pending",
        skipped: "pill-skipped",
        replay: "pill-replay",
        interrupted: "pill-interrupted"
    };

    /** Status -> pill markup (status text is escaped). */
    function pill(status) {
        const key = String(status || "unknown").toLowerCase();
        const cls = PILL_CLASSES[key] || "pill-stale";
        return '<span class="pill ' + cls + '">' + escapeHtml(key) + "</span>";
    }

    /** USD formatting that stays readable for sub-cent amounts. */
    function fmtCost(value) {
        const amount = Number(value);
        if (!isFinite(amount)) return "$0.00";
        if (amount === 0) return "$0.00";
        if (Math.abs(amount) < 0.01) return "$" + amount.toFixed(4);
        if (Math.abs(amount) < 100) return "$" + amount.toFixed(2);
        return "$" + amount.toFixed(0);
    }

    /** ISO timestamp -> compact relative age ("3m", "2h", "-" when unknown). */
    function fmtAgo(iso) {
        if (!iso) return "-";
        const then = Date.parse(iso);
        if (isNaN(then)) return "-";
        let seconds = Math.floor((Date.now() - then) / 1000);
        if (seconds < 0) seconds = 0;
        if (seconds < 60) return seconds + "s";
        if (seconds < 3600) return Math.floor(seconds / 60) + "m";
        if (seconds < 86400) return Math.floor(seconds / 3600) + "h";
        return Math.floor(seconds / 86400) + "d";
    }

    /** URL-path-safe id encoding. */
    function encodeId(id) {
        return encodeURIComponent(String(id === null || id === undefined ? "" : id));
    }

    /* ---- theme (dark default, persisted in localStorage) ---- */
    const THEME_KEY = "conduction-theme";

    function applyTheme(theme) {
        const value = theme === "light" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", value);
        try { localStorage.setItem(THEME_KEY, value); } catch (err) { /* private mode */ }
        document.querySelectorAll("[data-theme-toggle]").forEach(function (node) {
            node.textContent = value === "dark" ? "dark" : "light";
            node.setAttribute("aria-label", "Switch to " + (value === "dark" ? "light" : "dark") + " theme");
        });
    }

    function currentTheme() {
        return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    }

    function initTheme() {
        let stored = null;
        try { stored = localStorage.getItem(THEME_KEY); } catch (err) { stored = null; }
        applyTheme(stored === "light" ? "light" : "dark");
        document.querySelectorAll("[data-theme-toggle]").forEach(function (node) {
            node.addEventListener("click", function () {
                applyTheme(currentTheme() === "dark" ? "light" : "dark");
            });
        });
    }

    /* ---- topbar live-agent counter (polls /api/observe/summary every 5 s) ---- */
    function initTopbarCounter() {
        const counter = document.querySelector("[data-live-agents]");
        if (!counter) return;
        async function tick() {
            try {
                const summary = await fetchJson("/api/observe/summary");
                const live = Number(summary && summary.live_agents) || 0;
                counter.textContent = live + (live === 1 ? " agent live" : " agents live");
                counter.className = "badge" + (live > 0 ? " pill-running" : "");
            } catch (err) {
                counter.textContent = "agents -";
            }
        }
        tick();
        setInterval(tick, 5000);
    }

    window.Conduction = {
        escapeHtml: escapeHtml,
        fetchJson: fetchJson,
        openStream: openStream,
        pill: pill,
        fmtCost: fmtCost,
        fmtAgo: fmtAgo,
        encodeId: encodeId,
        applyTheme: applyTheme
    };
    // Legacy pages call a bare escapeHtml(); keep the global alias.
    if (typeof window.escapeHtml !== "function") window.escapeHtml = escapeHtml;

    document.addEventListener("DOMContentLoaded", function () {
        initTheme();
        initTopbarCounter();
    });
})();
