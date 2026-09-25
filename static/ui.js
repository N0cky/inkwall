/* Gemeinsame Helfer der Oberfläche: JSON-Aufrufe, Toast, Formatierung, Escaping. */
window.ui = (function () {
    'use strict';

    // Auch ' – fields.js setzt Werte in Attribute mit einfachen Anführungszeichen
    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    async function json(url, options) {
        var opts = Object.assign({ headers: {} }, options || {});
        if (opts.body && typeof opts.body !== 'string') {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(opts.body);
        }
        var response = await fetch(url, opts);
        var data = null;
        try { data = await response.json(); } catch (e) { data = null; }
        if (!response.ok) {
            var err = new Error((data && (data.error || (data.errors && data.errors.general && data.errors.general[0]))) || ('HTTP ' + response.status));
            err.status = response.status;
            err.data = data;
            throw err;
        }
        return data;
    }

    var toastHost = null;
    function toast(message, kind, ms) {
        if (!toastHost) {
            toastHost = document.createElement('div');
            toastHost.className = 'toast-host';
            document.body.appendChild(toastHost);
        }
        var el = document.createElement('div');
        el.className = 'toast ' + (kind || 'info');
        el.textContent = message;
        toastHost.appendChild(el);
        requestAnimationFrame(function () { el.classList.add('show'); });
        setTimeout(function () {
            el.classList.remove('show');
            setTimeout(function () { el.remove(); }, 250);
        }, ms || 3200);
    }

    function fmtTime(iso) {
        if (!iso) return '–';
        var normalized = String(iso).replace(/\+00:00$/, 'Z');
        var d = new Date(normalized.endsWith('Z') || /[+-]\d\d:\d\d$/.test(normalized) ? normalized : normalized + 'Z');
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
    }

    function fmtDateTime(iso) {
        if (!iso) return '–';
        var normalized = String(iso).replace(/\+00:00$/, 'Z');
        var d = new Date(normalized.endsWith('Z') ? normalized : normalized + 'Z');
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'short' });
    }

    /* "vor 4 min", "vor 2 h", "gerade eben" */
    function ago(seconds) {
        if (seconds == null) return 'noch nie';
        if (seconds < 45) return 'gerade eben';
        if (seconds < 3600) return 'vor ' + Math.round(seconds / 60) + ' min';
        if (seconds < 86400) return 'vor ' + Math.round(seconds / 3600) + ' h';
        return 'vor ' + Math.round(seconds / 86400) + ' Tagen';
    }

    /* "3:20" für Countdown */
    function mmss(seconds) {
        var s = Math.max(0, Math.round(seconds));
        var m = Math.floor(s / 60);
        return m + ':' + String(s % 60).padStart(2, '0');
    }

    return { esc: esc, json: json, toast: toast, fmtTime: fmtTime, fmtDateTime: fmtDateTime, ago: ago, mmss: mmss };
})();
