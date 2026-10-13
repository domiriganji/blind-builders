/* Optional. The site works fully without this file.
   On a job page: check every few seconds, and when the work is done play three short taps
   (rhythm, not pitch) and move focus to a link to the result. */
(function () {
  document.documentElement.className += " js";
  // A page that comes back with mistakes: put focus on the list of problems so it is read first.
  var summary = document.querySelector(".error-summary");
  if (summary) { try { summary.focus(); } catch (e) {} }
  var main = document.getElementById("main");
  if (!main) return;
  var id = main.getAttribute("data-job-id");
  var status = main.getAttribute("data-job-status");
  if (!id || status === "failed" || status === "done") return;
  var live = document.getElementById("job-live");
  var step = document.getElementById("job-step");
  var sound = document.getElementById("job-sound");
  try { if (sound && window.localStorage.getItem("studio-sound") === "off") sound.checked = false; } catch (e) {}
  if (sound) sound.addEventListener("change", function () {
    try { window.localStorage.setItem("studio-sound", sound.checked ? "on" : "off"); } catch (e) {}
  });
  function taps() {
    try {
      var C = window.AudioContext || window.webkitAudioContext;
      if (!C) return;
      var ctx = new C();
      [0, 0.2, 0.4].forEach(function (t) {
        var o = ctx.createOscillator(), g = ctx.createGain(), s = ctx.currentTime + t;
        o.frequency.value = 660;
        g.gain.setValueAtTime(0.0001, s);
        g.gain.exponentialRampToValueAtTime(0.35, s + 0.01);
        g.gain.exponentialRampToValueAtTime(0.0001, s + 0.08);
        o.connect(g); g.connect(ctx.destination); o.start(s); o.stop(s + 0.1);
      });
    } catch (e) {}
  }
  function poll() {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/jobs/" + id + ".json");
    xhr.onload = function () {
      var j;
      try { j = JSON.parse(xhr.responseText); } catch (e) { return setTimeout(poll, 8000); }
      if (j.status === "done") {
        if (!sound || sound.checked) taps();
        var a = document.createElement("a");
        a.href = j.result_url || "/jobs";
        a.textContent = "Done. Open the result: " + j.title;
        live.textContent = "";
        live.appendChild(a);
        a.focus();
        return;
      }
      if (j.status === "failed") {
        live.textContent = "This did not finish. " + j.error + " Reload the page for options.";
        return;
      }
      if (step && j.progress) step.textContent = j.progress;
      setTimeout(poll, 4000);
    };
    xhr.onerror = function () { setTimeout(poll, 8000); };
    xhr.send();
  }
  setTimeout(poll, 3000);
})();
