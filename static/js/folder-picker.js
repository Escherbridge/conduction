/* Folder picker: turns any text input into a browse-able path field.

   Markup contract -- add `data-folder-input` to the <input>:
       <input type="text" id="target_repo" data-folder-input class="mono">
   Optional attributes:
       data-folder-title   dialog heading (default "Select a folder")
       data-folder-validate="off"  skip the inline validity badge

   Two ways to pick, because one of them is not always available:
     1. The OS folder dialog, opened SERVER-side via POST /api/fs/pick. The
        server runs on the user's own machine, so this is the real native
        chooser. Only offered when the request came from loopback.
     2. An in-browser directory browser over GET /api/fs/list -- the fallback
        when the server is headless or reached over the LAN.

   See routes/AGENTS.md "fsapi" for the server contract. */
(function () {
    "use strict";

    const escapeHtml = (window.Conduction && window.Conduction.escapeHtml) || function (value) {
        return String(value === null || value === undefined ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    };
    const fetchJson = (window.Conduction && window.Conduction.fetchJson) || async function (url, options) {
        const response = await fetch(url, options);
        const data = await response.json().catch(function () { return null; });
        if (!response.ok) throw new Error((data && data.error) || response.statusText);
        return data;
    };

    let rootsPromise = null;
    /** /api/fs/roots is stable per page load; fetch it once and share it. */
    function loadRoots() {
        if (!rootsPromise) {
            rootsPromise = fetchJson("/api/fs/roots").catch(function () {
                return { roots: [], home: "", native_dialog: false };
            });
        }
        return rootsPromise;
    }

    function annotations(entry) {
        let badges = "";
        if (entry.is_git) badges += '<span class="fp-tag fp-tag-git">git</span>';
        if (entry.has_agentgraph) badges += '<span class="fp-tag fp-tag-ag">runs</span>';
        return badges;
    }

    /* ---- the in-browser directory browser modal ---- */

    function openBrowser(options) {
        const startPath = options.startPath || "";
        const title = options.title || "Select a folder";

        const overlay = document.createElement("div");
        overlay.className = "fp-overlay";
        overlay.innerHTML =
            '<div class="fp-modal" role="dialog" aria-modal="true" aria-label="' + escapeHtml(title) + '">' +
            '  <header class="fp-head">' +
            '    <strong>' + escapeHtml(title) + '</strong>' +
            '    <button type="button" class="fp-close" aria-label="Close">&times;</button>' +
            '  </header>' +
            '  <div class="fp-crumbs mono" data-fp-crumbs></div>' +
            '  <div class="fp-body" data-fp-body><div class="fp-hint">Loading…</div></div>' +
            '  <footer class="fp-foot">' +
            '    <input type="text" class="fp-path mono" data-fp-path spellcheck="false" placeholder="Type or paste a path">' +
            '    <button type="button" class="btn" data-fp-cancel>Cancel</button>' +
            '    <button type="button" class="btn btn-primary" data-fp-choose>Use this folder</button>' +
            '  </footer>' +
            '</div>';
        document.body.appendChild(overlay);

        const body = overlay.querySelector("[data-fp-body]");
        const crumbs = overlay.querySelector("[data-fp-crumbs]");
        const pathField = overlay.querySelector("[data-fp-path]");
        let current = startPath;
        // dialogs.js loads first (see base.html); fall back to a no-op if not.
        const releaseFocus = (window.Conduction.trapFocus || function () { return function () {}; })(overlay);

        return new Promise(function (resolve) {
            function close(result) {
                document.removeEventListener("keydown", onKey);
                releaseFocus();
                overlay.remove();
                resolve(result);
            }
            function onKey(event) {
                if (event.key === "Escape") close(null);
            }
            document.addEventListener("keydown", onKey);
            overlay.addEventListener("mousedown", function (event) {
                if (event.target === overlay) close(null);
            });
            overlay.querySelector(".fp-close").addEventListener("click", function () { close(null); });
            overlay.querySelector("[data-fp-cancel]").addEventListener("click", function () { close(null); });
            overlay.querySelector("[data-fp-choose]").addEventListener("click", function () {
                const typed = pathField.value.trim();
                if (typed) close(typed);
            });
            pathField.addEventListener("keydown", function (event) {
                if (event.key !== "Enter") return;
                event.preventDefault();
                const typed = pathField.value.trim();
                if (typed) navigate(typed);
            });

            /** Render the root shortcuts (allowed roots, known repos, drives). */
            async function showRoots() {
                const info = await loadRoots();
                current = "";
                pathField.value = "";
                crumbs.textContent = "Start from";
                if (!info.roots.length) {
                    body.innerHTML = '<div class="fp-hint">No browsable roots. Set CONDUCTION_ALLOWED_ROOTS.</div>';
                    return;
                }
                body.innerHTML = info.roots.map(function (entry) {
                    return '<button type="button" class="fp-row" data-path="' + escapeHtml(entry.path) + '">' +
                        '<span class="fp-icon">' + (entry.kind === "drive" ? "▣" : "★") + '</span>' +
                        '<span class="fp-name">' + escapeHtml(entry.name) + '</span>' +
                        annotations(entry) +
                        '<span class="fp-sub mono">' + escapeHtml(entry.path) + "</span></button>";
                }).join("");
                bindRows();
            }

            async function navigate(target) {
                body.innerHTML = '<div class="fp-hint">Loading…</div>';
                let data;
                try {
                    data = await fetchJson("/api/fs/list?path=" + encodeURIComponent(target));
                } catch (error) {
                    body.innerHTML = '<div class="fp-hint fp-error">' + escapeHtml(error.message) + "</div>";
                    return;
                }
                current = data.path;
                pathField.value = data.path;
                crumbs.innerHTML =
                    '<button type="button" class="fp-crumb" data-roots>roots</button>' +
                    (data.parent ? ' / <button type="button" class="fp-crumb" data-path="' +
                        escapeHtml(data.parent) + '">up</button>' : "") +
                    ' / <span>' + escapeHtml(data.path) + "</span>";
                crumbs.querySelector("[data-roots]").addEventListener("click", showRoots);
                const upButton = crumbs.querySelector("[data-path]");
                if (upButton) {
                    upButton.addEventListener("click", function () { navigate(data.parent); });
                }
                if (!data.entries.length) {
                    body.innerHTML = '<div class="fp-hint">No sub-folders here. “Use this folder” selects ' +
                        escapeHtml(data.path) + ".</div>";
                    return;
                }
                body.innerHTML = data.entries.map(function (entry) {
                    return '<button type="button" class="fp-row" data-path="' + escapeHtml(entry.path) + '">' +
                        '<span class="fp-icon">▸</span>' +
                        '<span class="fp-name">' + escapeHtml(entry.name) + "</span>" +
                        annotations(entry) + "</button>";
                }).join("") + (data.truncated ? '<div class="fp-hint">…listing truncated.</div>' : "");
                bindRows();
            }

            function bindRows() {
                body.querySelectorAll("[data-path]").forEach(function (row) {
                    row.addEventListener("click", function () { navigate(row.getAttribute("data-path")); });
                    row.addEventListener("dblclick", function () { close(row.getAttribute("data-path")); });
                });
            }

            if (startPath) navigate(startPath); else showRoots();
        });
    }

    /* ---- inline validity badge ---- */

    function attachValidation(input, badge) {
        let timer = null;
        let sequence = 0;
        async function check() {
            const value = input.value.trim();
            if (!value) { badge.className = "fp-badge"; badge.textContent = ""; return; }
            const ticket = ++sequence;
            badge.className = "fp-badge fp-badge-pending";
            badge.textContent = "checking…";
            let data;
            try {
                data = await fetchJson("/api/fs/validate?path=" + encodeURIComponent(value));
            } catch (error) {
                data = { ok: false, error: error.message };
            }
            if (ticket !== sequence) return;  // a newer keystroke already won
            if (data.ok) {
                const marks = [data.is_git ? "git repo" : "folder"];
                if (data.has_agentgraph) marks.push("has runs");
                badge.className = "fp-badge fp-badge-ok";
                badge.textContent = "✓ " + marks.join(" · ");
                badge.title = data.path;
            } else {
                badge.className = "fp-badge fp-badge-bad";
                badge.textContent = data.exists ? "✗ not allowed" : "✗ not found";
                badge.title = data.error || "";
            }
        }
        input.addEventListener("input", function () {
            clearTimeout(timer);
            timer = setTimeout(check, 350);
        });
        input.addEventListener("change", check);
        if (input.value.trim()) check();
        // Returned so a programmatic set (accept()) can cancel the debounce it
        // just triggered and validate once instead of twice.
        return function revalidate() {
            clearTimeout(timer);
            return check();
        };
    }

    /* ---- wiring ---- */

    async function enhance(input) {
        if (input.dataset.folderReady === "1") return;
        input.dataset.folderReady = "1";
        input.setAttribute("spellcheck", "false");

        // Browse-only is the default: a path is something you point at, not
        // something you spell. The field displays the choice and is itself the
        // button. `data-folder-typing="on"` opts back into a typable field; the
        // browser modal keeps a paste box either way, so nothing is lost.
        const browseOnly = input.dataset.folderTyping !== "on";

        const wrap = document.createElement("div");
        wrap.className = "fp-field";
        input.parentNode.insertBefore(wrap, input);

        const row = document.createElement("div");
        row.className = "fp-row-input";
        wrap.appendChild(row);
        row.appendChild(input);

        const browse = document.createElement("button");
        browse.type = "button";
        browse.className = "btn fp-browse";
        row.appendChild(browse);

        if (browseOnly) {
            input.readOnly = true;
            input.classList.add("fp-readonly");
            input.placeholder = input.dataset.folderEmpty || "No folder chosen";
            input.addEventListener("mousedown", function (event) {
                event.preventDefault();
                browse.click();
            });
            input.addEventListener("keydown", function (event) {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    browse.click();
                }
            });
        }

        /** "Choose folder…" until something is chosen, then "Change…". */
        function refreshBrowseLabel() {
            browse.textContent = browseOnly
                ? (input.value.trim() ? "Change…" : "Choose folder…")
                : "Browse…";
        }
        refreshBrowseLabel();
        // accept() dispatches `change`, so picking a folder relabels the button.
        input.addEventListener("change", refreshBrowseLabel);

        const badge = document.createElement("span");
        badge.className = "fp-badge";
        wrap.appendChild(badge);

        const revalidate = input.dataset.folderValidate === "off"
            ? function () {}
            : attachValidation(input, badge);
        const title = input.dataset.folderTitle || "Select a folder";

        function accept(path) {
            if (!path) return;
            input.value = path;
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
            revalidate();
        }

        browse.addEventListener("click", async function () {
            const info = await loadRoots();
            const start = input.value.trim() || info.home || "";
            if (info.native_dialog) {
                browse.disabled = true;
                browse.textContent = "Choosing…";
                let result = null;
                try {
                    result = await fetchJson("/api/fs/pick", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ initial: start })
                    });
                } catch (error) {
                    result = { available: false };
                } finally {
                    browse.disabled = false;
                    refreshBrowseLabel();
                }
                if (result && result.available) {
                    if (!result.cancelled) accept(result.path);
                    return;  // a cancelled native dialog means "never mind", not "fall back"
                }
            }
            accept(await openBrowser({ startPath: start, title: title }));
        });
    }

    function initFolderPickers(scope) {
        (scope || document).querySelectorAll("input[data-folder-input]").forEach(enhance);
    }

    window.Conduction = window.Conduction || {};
    window.Conduction.initFolderPickers = initFolderPickers;
    window.Conduction.browseForFolder = openBrowser;

    document.addEventListener("DOMContentLoaded", function () { initFolderPickers(document); });
})();
