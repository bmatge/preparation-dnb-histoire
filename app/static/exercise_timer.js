/**
 * Timer à double décompte pour les exercices du DNB.
 *
 * Détecte au chargement les éléments [data-exercise-timer] et démarre
 * un chrono persistant. Le départ est mémorisé dans localStorage pour
 * survivre aux rechargements et aux changements d'étape sur les
 * parcours multi-étapes (DC, rédaction). Pas de dépendance externe.
 *
 * Attributs attendus sur l'élément racine :
 *   data-timer-key      clé localStorage (usage : "<kind>_<session_id>")
 *   data-exercise-sec   durée allouée à l'exercice courant, en secondes
 *   data-epreuve-sec    durée totale de l'épreuve DNB, en secondes
 *
 * Sous-éléments ciblés (optionnels mais recommandés) :
 *   [data-timer-exercise]      span texte temps restant exercice
 *   [data-timer-epreuve]       span texte temps restant épreuve
 *   [data-timer-exercise-bar]  barre de progression exercice
 *   [data-timer-epreuve-bar]   barre de progression épreuve
 */
(function () {
  "use strict";

  function formatDuration(totalSeconds) {
    var s = Math.max(0, Math.floor(totalSeconds));
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = s % 60;
    function pad2(n) {
      return (n < 10 ? "0" : "") + n;
    }
    if (h > 0) {
      return h + "h" + pad2(m) + ":" + pad2(sec);
    }
    return m + ":" + pad2(sec);
  }

  function readStart(storageKey) {
    try {
      var raw = localStorage.getItem(storageKey);
      if (!raw) return null;
      var parsed = parseInt(raw, 10);
      if (isNaN(parsed) || parsed <= 0) return null;
      return parsed;
    } catch (e) {
      return null;
    }
  }

  function writeStart(storageKey, ts) {
    try {
      localStorage.setItem(storageKey, String(ts));
    } catch (e) {
      // localStorage indispo (mode privé strict). Le chrono fonctionnera
      // quand même mais ne persistera pas entre rechargements.
    }
  }

  function initTimer(root) {
    var storageKey = "exercise_timer_" + (root.dataset.timerKey || "anon");
    var exerciseSec = parseInt(root.dataset.exerciseSec || "0", 10);
    var epreuveSec = parseInt(root.dataset.epreuveSec || "0", 10);

    if (!exerciseSec || !epreuveSec) {
      return;
    }

    var start = readStart(storageKey);
    if (start === null) {
      start = Date.now();
      writeStart(storageKey, start);
    }

    var exerciseText = root.querySelector("[data-timer-exercise]");
    var epreuveText = root.querySelector("[data-timer-epreuve]");
    var exerciseBar = root.querySelector("[data-timer-exercise-bar]");
    var epreuveBar = root.querySelector("[data-timer-epreuve-bar]");

    function tick() {
      var elapsedSec = (Date.now() - start) / 1000;

      var exRemaining = exerciseSec - elapsedSec;
      var epRemaining = epreuveSec - elapsedSec;

      if (exerciseText) {
        if (exRemaining >= 0) {
          exerciseText.textContent = formatDuration(exRemaining);
          exerciseText.classList.remove("text-rose-600");
        } else {
          exerciseText.textContent = "+" + formatDuration(-exRemaining);
          exerciseText.classList.add("text-rose-600");
        }
      }
      if (epreuveText) {
        if (epRemaining >= 0) {
          epreuveText.textContent = formatDuration(epRemaining);
          epreuveText.classList.remove("text-rose-600");
        } else {
          epreuveText.textContent = "+" + formatDuration(-epRemaining);
          epreuveText.classList.add("text-rose-600");
        }
      }
      if (exerciseBar) {
        var pctEx = Math.min(100, (elapsedSec / exerciseSec) * 100);
        exerciseBar.style.width = pctEx + "%";
        if (elapsedSec > exerciseSec) {
          exerciseBar.classList.remove("bg-brand-500");
          exerciseBar.classList.add("bg-rose-500");
        }
      }
      if (epreuveBar) {
        var pctEp = Math.min(100, (elapsedSec / epreuveSec) * 100);
        epreuveBar.style.width = pctEp + "%";
        if (elapsedSec > epreuveSec) {
          epreuveBar.classList.remove("bg-purple-500");
          epreuveBar.classList.add("bg-rose-500");
        }
      }
    }

    tick();
    setInterval(tick, 1000);
  }

  function bootstrap() {
    var nodes = document.querySelectorAll("[data-exercise-timer]");
    Array.prototype.forEach.call(nodes, initTimer);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
