/*
 * draft_autosave.js — sauvegarde locale des brouillons longs + reprise globale.
 *
 * Deux modes complémentaires :
 *
 * 1. Autosave sur les pages de rédaction. Cible tout
 *    <textarea data-draft-key="..."> : à chaque frappe, la valeur est
 *    stockée dans localStorage (debounce 2 s). À l'ouverture de la page,
 *    si un brouillon existe pour ce même sujet et diffère de la valeur
 *    affichée, un bandeau local propose la reprise sur place.
 *
 * 2. Bandeau global "tu as un travail en cours" sur les pages qui ne
 *    contiennent pas de textarea instrumenté (accueil, sélecteur de
 *    matière, index d'épreuve). Scanne toutes les clés stockées, pioche
 *    les brouillons non vides récents, et affiche un encart cliquable
 *    qui renvoie directement sur l'étape correspondante.
 *
 * Format de clé : "<matiere>:<epreuve>:<etape>:<sujet_id>[:<option>]".
 *   - "hg-emc:dc:step2:17"                 → HG-EMC DC étape 2
 *   - "fr:redaction:step6:30:imagination"  → Français rédaction étape 6
 * Elle sert à la fois d'identifiant de stockage et de source pour
 * reconstruire le libellé et l'URL de reprise.
 *
 * Stockage : { value: string, savedAt: number (ms epoch) } sous chaque clé,
 * préfixé par "rtd:draft:" pour isoler et purger proprement. Les brouillons
 * vieux de plus de 30 jours sont effacés au chargement. En cas d'accès
 * localStorage bloqué (mode privé Safari anciens, politique stricte), le
 * helper se désactive silencieusement.
 */
(function () {
  "use strict";

  var PREFIX = "rtd:draft:";
  var DEBOUNCE_MS = 2000;
  var MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000; // 30 jours

  function storageAvailable() {
    try {
      var probe = "__rtd_probe__";
      window.localStorage.setItem(probe, probe);
      window.localStorage.removeItem(probe);
      return true;
    } catch (e) {
      return false;
    }
  }

  function loadDraft(key) {
    try {
      var raw = window.localStorage.getItem(PREFIX + key);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed.value !== "string") return null;
      return parsed;
    } catch (e) {
      return null;
    }
  }

  function saveDraft(key, value) {
    try {
      var payload = JSON.stringify({ value: value, savedAt: Date.now() });
      window.localStorage.setItem(PREFIX + key, payload);
    } catch (e) {
      // quota ou autre — on abandonne silencieusement
    }
  }

  function clearDraft(key) {
    try {
      window.localStorage.removeItem(PREFIX + key);
    } catch (e) {
      /* no-op */
    }
  }

  function purgeExpired() {
    try {
      var now = Date.now();
      var toRemove = [];
      for (var i = 0; i < window.localStorage.length; i++) {
        var k = window.localStorage.key(i);
        if (!k || k.indexOf(PREFIX) !== 0) continue;
        var raw = window.localStorage.getItem(k);
        if (!raw) continue;
        try {
          var parsed = JSON.parse(raw);
          if (!parsed || typeof parsed.savedAt !== "number") {
            toRemove.push(k);
            continue;
          }
          if (now - parsed.savedAt > MAX_AGE_MS) toRemove.push(k);
        } catch (e) {
          toRemove.push(k);
        }
      }
      toRemove.forEach(function (k) {
        window.localStorage.removeItem(k);
      });
    } catch (e) {
      /* no-op */
    }
  }

  function formatSavedAt(ts) {
    try {
      var d = new Date(ts);
      var now = new Date();
      var sameDay =
        d.getFullYear() === now.getFullYear() &&
        d.getMonth() === now.getMonth() &&
        d.getDate() === now.getDate();
      var hh = String(d.getHours()).padStart(2, "0");
      var mm = String(d.getMinutes()).padStart(2, "0");
      if (sameDay) return "aujourd'hui à " + hh + ":" + mm;
      var dd = String(d.getDate()).padStart(2, "0");
      var mo = String(d.getMonth() + 1).padStart(2, "0");
      return "le " + dd + "/" + mo + " à " + hh + ":" + mm;
    } catch (e) {
      return "précédemment";
    }
  }

  function buildBanner(savedAt, onRestore, onIgnore) {
    var banner = document.createElement("div");
    banner.setAttribute("data-draft-banner", "");
    banner.className =
      "mb-4 rounded-2xl border border-amber-200 bg-amber-50 p-4 flex flex-wrap items-center justify-between gap-3 animate-reveal";

    var message = document.createElement("div");
    message.className = "text-sm text-amber-900";
    var strong = document.createElement("strong");
    strong.textContent = "Tu as un brouillon enregistré ";
    message.appendChild(strong);
    message.appendChild(document.createTextNode(formatSavedAt(savedAt) + "."));
    var hint = document.createElement("div");
    hint.className = "text-xs text-amber-800 mt-0.5";
    hint.textContent =
      "Il est stocké uniquement sur ce navigateur. Tu peux le reprendre ou repartir de zéro.";
    message.appendChild(hint);

    var actions = document.createElement("div");
    actions.className = "flex items-center gap-2";

    var restoreBtn = document.createElement("button");
    restoreBtn.type = "button";
    restoreBtn.className =
      "inline-flex items-center gap-1 rounded-full bg-amber-600 text-white text-xs font-semibold px-3 py-1.5 hover:bg-amber-700 transition";
    restoreBtn.textContent = "Reprendre mon brouillon";
    restoreBtn.addEventListener("click", function () {
      onRestore();
      banner.remove();
    });

    var ignoreBtn = document.createElement("button");
    ignoreBtn.type = "button";
    ignoreBtn.className =
      "inline-flex items-center gap-1 rounded-full bg-white text-amber-800 text-xs font-semibold px-3 py-1.5 border border-amber-300 hover:bg-amber-100 transition";
    ignoreBtn.textContent = "Ignorer";
    ignoreBtn.addEventListener("click", function () {
      onIgnore();
      banner.remove();
    });

    actions.appendChild(restoreBtn);
    actions.appendChild(ignoreBtn);

    banner.appendChild(message);
    banner.appendChild(actions);
    return banner;
  }

  function setupTextarea(textarea) {
    var key = textarea.getAttribute("data-draft-key");
    if (!key) return;

    var initialValue = textarea.value || "";
    var draft = loadDraft(key);

    // Si un brouillon existe et diffère de ce qui est déjà affiché
    // (par ex. un previous_proposal pré-rempli), on propose la reprise.
    if (draft && draft.value && draft.value !== initialValue) {
      var banner = buildBanner(
        draft.savedAt,
        function onRestore() {
          textarea.value = draft.value;
          // Déclenche un input event pour laisser à d'autres scripts (auto-resize,
          // compteur de caractères…) le temps de réagir.
          textarea.dispatchEvent(new Event("input", { bubbles: true }));
          textarea.focus();
        },
        function onIgnore() {
          clearDraft(key);
        }
      );
      // Insère le bandeau juste avant le <form> parent si possible, sinon
      // avant le textarea lui-même.
      var form = textarea.closest("form");
      var host = form && form.parentNode ? form : textarea;
      host.parentNode.insertBefore(banner, host);
    }

    var timer = null;
    textarea.addEventListener("input", function () {
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        saveDraft(key, textarea.value);
      }, DEBOUNCE_MS);
    });

    // Purge le brouillon quand le formulaire est soumis avec succès côté
    // client (envoi HTMX ou submit classique). On ne peut pas distinguer
    // succès/échec ici — si le serveur renvoie une erreur de validation,
    // l'utilisateur sera encore sur la page, ressaisira, et un nouveau
    // brouillon sera créé au prochain input. Acceptable.
    var form = textarea.closest("form");
    if (form) {
      form.addEventListener("submit", function () {
        if (timer) clearTimeout(timer);
        clearDraft(key);
      });
    }
  }

  // --------------------------------------------------------------------
  // Reprise globale : bandeau affiché sur les pages sans textarea
  // instrumenté (accueil, sélecteur de matière, index d'épreuve).
  // --------------------------------------------------------------------

  // Table de correspondance clé → libellé + URL de reprise. On garde ça
  // explicite plutôt qu'un mapping générique, pour éviter que l'ajout
  // d'un futur type de clé ne déclenche silencieusement des URLs
  // cassées. L'URL cible une route serveur /resume/... qui force la
  // session Starlette sur le bon subject_id avant de rediriger vers
  // /step/N — sinon l'élève repartirait sur le sujet courant du cookie,
  // qui n'est pas forcément celui du brouillon.
  function describeKey(key) {
    // HG-EMC développement construit : "hg-emc:dc:stepN:subjectId"
    var mHgemc = key.match(/^hg-emc:dc:step(\d+):(\d+)$/);
    if (mHgemc) {
      var step = mHgemc[1];
      var subjectId = mHgemc[2];
      return {
        matiere: "Histoire-Géo",
        epreuve: "Développement construit",
        step: parseInt(step, 10),
        url:
          "/histoire-geo-emc/developpement-construit/resume/" +
          encodeURIComponent(subjectId) +
          "/step/" +
          encodeURIComponent(step),
      };
    }
    // Français rédaction : "fr:redaction:stepN:subjectId:option"
    var mFr = key.match(/^fr:redaction:step(\d+):(\d+):(\w+)$/);
    if (mFr) {
      var stepFr = mFr[1];
      var subjectIdFr = mFr[2];
      var option = mFr[3];
      return {
        matiere: "Français",
        epreuve:
          "Rédaction" +
          (option === "imagination"
            ? " (imagination)"
            : option === "reflexion"
            ? " (réflexion)"
            : ""),
        step: parseInt(stepFr, 10),
        url:
          "/francais/redaction/resume/" +
          encodeURIComponent(subjectIdFr) +
          "/step/" +
          encodeURIComponent(stepFr) +
          "?option=" +
          encodeURIComponent(option),
      };
    }
    return null;
  }

  function stepLabel(step) {
    // Les étapes 2, 4, 6 correspondent respectivement à brouillon v1,
    // brouillon v2, rédaction complète — dans les deux épreuves.
    if (step === 2) return "brouillon";
    if (step === 4) return "brouillon v2";
    if (step === 6) return "rédaction finale";
    return "étape " + step;
  }

  function collectActiveDrafts() {
    var drafts = [];
    try {
      for (var i = 0; i < window.localStorage.length; i++) {
        var fullKey = window.localStorage.key(i);
        if (!fullKey || fullKey.indexOf(PREFIX) !== 0) continue;
        var key = fullKey.slice(PREFIX.length);
        var raw = window.localStorage.getItem(fullKey);
        if (!raw) continue;
        var parsed;
        try {
          parsed = JSON.parse(raw);
        } catch (e) {
          continue;
        }
        if (!parsed || !parsed.value || !parsed.value.trim()) continue;
        var desc = describeKey(key);
        if (!desc) continue;
        drafts.push({
          key: key,
          savedAt: parsed.savedAt || 0,
          value: parsed.value,
          matiere: desc.matiere,
          epreuve: desc.epreuve,
          step: desc.step,
          url: desc.url,
        });
      }
    } catch (e) {
      return [];
    }
    drafts.sort(function (a, b) {
      return b.savedAt - a.savedAt;
    });
    return drafts;
  }

  function buildGlobalBanner(drafts) {
    var banner = document.createElement("div");
    banner.setAttribute("data-draft-global-banner", "");
    banner.className =
      "mb-6 rounded-2xl border border-amber-200 bg-gradient-to-br from-amber-50 to-orange-50 p-5 animate-reveal";

    var header = document.createElement("div");
    header.className = "flex items-center gap-2 mb-3";
    var icon = document.createElement("span");
    icon.className =
      "inline-flex h-9 w-9 items-center justify-center rounded-xl bg-amber-500 text-white text-lg shadow-sm";
    icon.textContent = "📝";
    var title = document.createElement("div");
    title.className = "font-display font-bold text-amber-900";
    title.textContent =
      drafts.length === 1
        ? "Tu as un travail en cours"
        : "Tu as " + drafts.length + " travaux en cours";
    header.appendChild(icon);
    header.appendChild(title);
    banner.appendChild(header);

    var list = document.createElement("div");
    list.className = "space-y-2";

    drafts.forEach(function (d) {
      var row = document.createElement("div");
      row.className =
        "flex items-center justify-between gap-3 bg-white rounded-xl border border-amber-100 px-4 py-3";

      var info = document.createElement("div");
      info.className = "min-w-0 flex-1";

      var head = document.createElement("div");
      head.className = "text-sm font-semibold text-slate-900";
      head.textContent = d.matiere + " — " + d.epreuve;
      info.appendChild(head);

      var sub = document.createElement("div");
      sub.className = "text-xs text-slate-500 mt-0.5";
      sub.textContent =
        "Sauvegardé " + formatSavedAt(d.savedAt) + " · " + stepLabel(d.step);
      info.appendChild(sub);

      var preview = d.value.trim().replace(/\s+/g, " ").slice(0, 80);
      if (preview) {
        var p = document.createElement("div");
        p.className =
          "text-xs text-slate-600 mt-1 italic truncate font-mono";
        p.textContent =
          "« " + preview + (d.value.length > 80 ? "… »" : " »");
        info.appendChild(p);
      }

      var actions = document.createElement("div");
      actions.className = "flex items-center gap-1 shrink-0";

      var resumeBtn = document.createElement("a");
      resumeBtn.href = d.url;
      resumeBtn.className =
        "inline-flex items-center gap-1 rounded-full bg-amber-600 text-white text-xs font-semibold px-3 py-1.5 hover:bg-amber-700 transition whitespace-nowrap";
      resumeBtn.textContent = "Reprendre →";

      var dismissBtn = document.createElement("button");
      dismissBtn.type = "button";
      dismissBtn.setAttribute("aria-label", "Ignorer ce brouillon");
      dismissBtn.className =
        "inline-flex h-7 w-7 items-center justify-center rounded-full text-slate-400 hover:bg-slate-100 hover:text-slate-700 transition text-lg leading-none";
      dismissBtn.textContent = "×";
      dismissBtn.addEventListener("click", function () {
        clearDraft(d.key);
        row.remove();
        // Si plus aucun brouillon affiché, on retire le bandeau complet.
        if (!list.querySelector("[data-draft-row]")) banner.remove();
      });

      actions.appendChild(resumeBtn);
      actions.appendChild(dismissBtn);

      row.setAttribute("data-draft-row", "");
      row.appendChild(info);
      row.appendChild(actions);
      list.appendChild(row);
    });

    banner.appendChild(list);
    return banner;
  }

  function setupGlobalBanner() {
    var drafts = collectActiveDrafts();
    if (drafts.length === 0) return;
    var main = document.querySelector("main");
    if (!main) return;
    var banner = buildGlobalBanner(drafts);
    main.insertBefore(banner, main.firstChild);
  }

  function init() {
    if (!storageAvailable()) return;
    purgeExpired();
    var textareas = document.querySelectorAll("textarea[data-draft-key]");
    if (textareas.length > 0) {
      // Page de flow : uniquement le comportement local textarea par
      // textarea. Pas de bandeau global pour ne pas doublonner.
      textareas.forEach(setupTextarea);
    } else {
      // Page hors flow : on propose la reprise globale si des brouillons
      // non vides traînent dans le storage.
      setupGlobalBanner();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
