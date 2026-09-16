/* Dialogs and toasts: the replacement for window.prompt / confirm / alert.

   Why: editing one goal used to be three sequential window.prompt() calls --
   you could not see the previous answer while typing the next, cancelling
   half-way left the first answers applied nowhere, and an invalid status was
   only caught after all three. One form with all fields visible, validated on
   submit, is the whole point.

   API (all on window.Conduction):
     toast(message, kind)            -- kind: "info" | "ok" | "warn" | "error"
     confirmDialog(options)          -- Promise<boolean>
     formDialog(options)             -- Promise<values|null>, null = cancelled

   formDialog fields: {name, label, type, value, required, options, rows, hint}
   type is "text" | "textarea" | "select" | "number" | "checkbox". */
(function () {
    "use strict";

    const conduction = (window.Conduction = window.Conduction || {});
    const escapeHtml = conduction.escapeHtml || function (value) {
        return String(value === null || value === undefined ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    };

    const TOAST_MILLIS = 4500;

    function toastHost() {
        let host = document.getElementById("cd-toasts");
        if (!host) {
            host = document.createElement("div");
            host.id = "cd-toasts";
            host.className = "cd-toasts";
            host.setAttribute("role", "status");
            host.setAttribute("aria-live", "polite");
            document.body.appendChild(host);
        }
        return host;
    }

    /** Transient notice in the corner. Returns a dismiss() for long operations. */
    function toast(message, kind) {
        const node = document.createElement("div");
        node.className = "cd-toast cd-toast-" + (kind || "info");
        node.textContent = message;
        const close = document.createElement("button");
        close.type = "button";
        close.className = "cd-toast-x";
        close.setAttribute("aria-label", "Dismiss");
        close.innerHTML = "&times;";
        node.appendChild(close);
        toastHost().appendChild(node);

        let timer = null;
        function dismiss() {
            clearTimeout(timer);
            node.classList.add("cd-toast-out");
            setTimeout(function () { node.remove(); }, 180);
        }
        close.addEventListener("click", dismiss);
        // A sticky toast (kind "error") waits to be dismissed: an error the user
        // scrolled past is an error they never saw.
        if (kind !== "error") timer = setTimeout(dismiss, TOAST_MILLIS);
        return dismiss;
    }

    /* ---- focus trap ---- */

    const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), ' +
        'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    /** Keep Tab inside `container` until dismissed. An aria-modal dialog whose
        Tab key walks out into the page behind it is modal in appearance only.
        Returns a release() to call when the dialog closes. */
    function trapFocus(container) {
        function onKeydown(event) {
            if (event.key !== "Tab") return;
            const items = Array.from(container.querySelectorAll(FOCUSABLE))
                .filter(function (node) { return node.offsetParent !== null; });
            if (!items.length) return;
            const first = items[0];
            const last = items[items.length - 1];
            const active = document.activeElement;
            if (event.shiftKey && (active === first || !container.contains(active))) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && active === last) {
                event.preventDefault();
                first.focus();
            }
        }
        container.addEventListener("keydown", onKeydown);
        return function release() { container.removeEventListener("keydown", onKeydown); };
    }

    /* ---- modal shell shared by confirmDialog and formDialog ---- */

    function openModal(options) {
        const overlay = document.createElement("div");
        overlay.className = "cd-overlay";
        overlay.innerHTML =
            '<div class="cd-modal" role="dialog" aria-modal="true" aria-label="' +
            escapeHtml(options.title) + '">' +
            '  <header class="cd-modal-head"><strong>' + escapeHtml(options.title) + "</strong>" +
            '    <button type="button" class="cd-modal-x" aria-label="Close">&times;</button></header>' +
            '  <form class="cd-modal-body" data-cd-form novalidate>' + options.bodyHtml + "</form>" +
            '  <footer class="cd-modal-foot">' +
            '    <span class="cd-modal-error" data-cd-error></span>' +
            '    <button type="button" class="btn" data-cd-cancel>' + escapeHtml(options.cancelLabel || "Cancel") + "</button>" +
            '    <button type="button" class="btn ' + (options.danger ? "btn-danger" : "btn-primary") +
            '" data-cd-ok>' + escapeHtml(options.confirmLabel || "OK") + "</button>" +
            "  </footer></div>";
        document.body.appendChild(overlay);

        const form = overlay.querySelector("[data-cd-form]");
        const errorSlot = overlay.querySelector("[data-cd-error]");
        const previouslyFocused = document.activeElement;
        const releaseFocus = trapFocus(overlay);

        return new Promise(function (resolve) {
            function close(result) {
                document.removeEventListener("keydown", onKey, true);
                releaseFocus();
                overlay.remove();
                if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
                resolve(result);
            }
            function submit() {
                const outcome = options.collect ? options.collect(form) : true;
                if (outcome && outcome.error) {
                    errorSlot.textContent = outcome.error;
                    return;
                }
                close(outcome && "values" in outcome ? outcome.values : true);
            }
            function onKey(event) {
                if (event.key === "Escape") { event.stopPropagation(); close(options.cancelValue); }
                // Enter submits from any single-line field; a textarea keeps it.
                if (event.key === "Enter" && event.target.tagName !== "TEXTAREA") {
                    event.preventDefault();
                    submit();
                }
            }
            document.addEventListener("keydown", onKey, true);
            overlay.addEventListener("mousedown", function (event) {
                if (event.target === overlay) close(options.cancelValue);
            });
            overlay.querySelector(".cd-modal-x").addEventListener("click", function () { close(options.cancelValue); });
            overlay.querySelector("[data-cd-cancel]").addEventListener("click", function () { close(options.cancelValue); });
            overlay.querySelector("[data-cd-ok]").addEventListener("click", submit);
            form.addEventListener("submit", function (event) { event.preventDefault(); submit(); });

            const first = form.querySelector("input, textarea, select");
            if (first) first.focus(); else overlay.querySelector("[data-cd-ok]").focus();
        });
    }

    /** Yes/no, with a real question instead of window.confirm's bare string. */
    function confirmDialog(options) {
        return openModal({
            title: options.title || "Are you sure?",
            bodyHtml: '<p class="cd-modal-text">' + escapeHtml(options.message || "") + "</p>",
            confirmLabel: options.confirmLabel || "Confirm",
            cancelLabel: options.cancelLabel,
            danger: options.danger,
            cancelValue: false,
            collect: function () { return true; }
        });
    }

    function fieldHtml(field) {
        const id = "cd-field-" + field.name;
        const label = '<label for="' + escapeHtml(id) + '">' + escapeHtml(field.label || field.name) +
            (field.required ? ' <span class="cd-req">*</span>' : "") + "</label>";
        let control;
        if (field.type === "textarea") {
            control = '<textarea id="' + escapeHtml(id) + '" name="' + escapeHtml(field.name) +
                '" rows="' + (field.rows || 3) + '">' + escapeHtml(field.value || "") + "</textarea>";
        } else if (field.type === "select") {
            control = '<select id="' + escapeHtml(id) + '" name="' + escapeHtml(field.name) + '">' +
                (field.options || []).map(function (option) {
                    const value = typeof option === "string" ? option : option.value;
                    const text = typeof option === "string" ? option : option.label;
                    return '<option value="' + escapeHtml(value) + '"' +
                        (String(field.value) === String(value) ? " selected" : "") + ">" +
                        escapeHtml(text) + "</option>";
                }).join("") + "</select>";
        } else if (field.type === "checkbox") {
            control = '<input type="checkbox" id="' + escapeHtml(id) + '" name="' + escapeHtml(field.name) +
                '"' + (field.value ? " checked" : "") + ">";
        } else {
            control = '<input type="' + escapeHtml(field.type || "text") + '" id="' + escapeHtml(id) +
                '" name="' + escapeHtml(field.name) + '" value="' + escapeHtml(field.value === undefined || field.value === null ? "" : field.value) + '">';
        }
        const hint = field.hint ? '<small class="muted">' + escapeHtml(field.hint) + "</small>" : "";
        return '<div class="cd-field">' + label + control + hint + "</div>";
    }

    /** One form, every field visible at once. Resolves null when cancelled. */
    function formDialog(options) {
        const fields = options.fields || [];
        return openModal({
            title: options.title || "Edit",
            bodyHtml: (options.intro ? '<p class="cd-modal-text">' + escapeHtml(options.intro) + "</p>" : "") +
                fields.map(fieldHtml).join(""),
            confirmLabel: options.submitLabel || "Save",
            cancelLabel: options.cancelLabel,
            danger: options.danger,
            cancelValue: null,
            collect: function (form) {
                const values = {};
                for (const field of fields) {
                    const node = form.elements[field.name];
                    const value = field.type === "checkbox" ? node.checked : node.value.trim();
                    if (field.required && !String(value).length) {
                        node.focus();
                        return { error: (field.label || field.name) + " is required" };
                    }
                    values[field.name] = value;
                }
                if (options.validate) {
                    const problem = options.validate(values);
                    if (problem) return { error: problem };
                }
                return { values: values };
            }
        });
    }

    conduction.trapFocus = trapFocus;
    conduction.toast = toast;
    conduction.confirmDialog = confirmDialog;
    conduction.formDialog = formDialog;
})();
