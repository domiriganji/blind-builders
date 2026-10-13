/* Optional. The site works fully without this file.
   - A page that comes back with mistakes: focus goes to the list of problems, so it is read first.
   - A page that opens with a notice (for example "Your profile is saved") and no mistakes: focus goes to it.
   - "Show my password" boxes switch a password field to plain text and back.
   - "Copy the code" copies the recovery code and says whether it worked.
   - On a job page: check every few seconds, say the step when it changes, and when the work ends play short
     taps (rhythm, not pitch) and move focus to a link: the result, or what to do next. */
(function () {
  document.documentElement.className += " js";
  var summary = document.querySelector(".error-summary");
  if (summary) {
    try { summary.focus(); } catch (e) {}
  } else {
    var notice = document.querySelector("main .messages");
    if (notice) {
      notice.setAttribute("tabindex", "-1");
      try { notice.focus(); } catch (e) {}
    }
  }

  // Show or hide what was typed in a password or key field.
  Array.prototype.forEach.call(document.querySelectorAll("input[data-shows]"), function (box) {
    var fields = box.getAttribute("data-shows").split(" ").map(function (id) { return document.getElementById(id); })
      .filter(function (f) { return f; });
    function apply(show) { fields.forEach(function (f) { f.type = show ? "text" : "password"; }); }
    box.addEventListener("change", function () { apply(box.checked); });
    if (box.form) box.form.addEventListener("submit", function () { apply(false); });
    if (box.checked) apply(true);
  });

  // Copy a code to the clipboard, and say so in a status message.
  Array.prototype.forEach.call(document.querySelectorAll("[data-copy]"), function (button) {
    var field = document.getElementById(button.getAttribute("data-copy"));
    var status = document.getElementById(button.getAttribute("data-copied"));
    if (!field) return;
    function say(text) {
      if (!status) return;
      status.textContent = "";
      setTimeout(function () { status.textContent = text; }, 50);
    }
    function byHand() {
      var ok = false;
      try { field.focus(); field.select(); ok = document.execCommand("copy"); } catch (e) {}
      say(ok ? "Copied. Paste it into a note or a password manager now."
             : "Your browser did not let this page copy. The code is selected in the box: copy it from there.");
    }
    button.addEventListener("click", function () {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(field.value).then(function () {
          say("Copied. Paste it into a note or a password manager now.");
        }, byHand);
      } else {
        byHand();
      }
    });
  });

  var main = document.getElementById("main");
  if (!main) return;
  var id = main.getAttribute("data-job-id");
  var status = main.getAttribute("data-job-status");
  if (!id || status === "failed" || status === "done") return;
  var live = document.getElementById("job-live");
  var step = document.getElementById("job-step");
  var elapsed = document.getElementById("job-elapsed");
  var check = document.getElementById("job-check");
  var sound = document.getElementById("job-sound");
  var h1 = main.querySelector("h1");
  var h1Text = h1 ? h1.textContent : "";
  var suffix = document.title.indexOf(h1Text) === 0 ? document.title.slice(h1Text.length) : "";
  var lastStep = step ? step.textContent : "";
  var failures = 0;
  try { if (sound && window.localStorage.getItem("studio-sound") === "off") sound.checked = false; } catch (e) {}
  if (sound) sound.addEventListener("change", function () {
    try { window.localStorage.setItem("studio-sound", sound.checked ? "on" : "off"); } catch (e) {}
  });
  function taps(times, gap) {
    if (sound && !sound.checked) return;
    try {
      var C = window.AudioContext || window.webkitAudioContext;
      if (!C) return;
      var ctx = new C();
      times.forEach(function (t) {
        var o = ctx.createOscillator(), g = ctx.createGain(), s = ctx.currentTime + t;
        o.frequency.value = 660;
        g.gain.setValueAtTime(0.0001, s);
        g.gain.exponentialRampToValueAtTime(0.35, s + 0.01);
        g.gain.exponentialRampToValueAtTime(0.0001, s + gap);
        o.connect(g); g.connect(ctx.destination); o.start(s); o.stop(s + gap + 0.02);
      });
    } catch (e) {}
  }
  function setHeading(text) {
    if (h1) h1.textContent = text;
    document.title = text + suffix;
  }
  function link(href, text) {
    var a = document.createElement("a");
    a.href = href;
    a.textContent = text;
    return a;
  }
  function ended() {
    if (check && check.parentNode) check.parentNode.removeChild(check);
  }
  function finished(j) {
    ended();
    taps([0, 0.2, 0.4], 0.08);
    setHeading("Done: " + j.title);
    var a = link(j.result_url || "/jobs", "Done. Open the result: " + j.title);
    live.textContent = "";
    live.appendChild(a);
    a.focus();
  }
  function stopped(j) {
    ended();
    taps([0, 0.6], 0.3);  // two long taps: a different pattern from "done"
    var stalled = j.state === "stalled";
    setHeading((stalled ? "This seems to have stopped: " : "This did not finish: ") + j.title);
    live.textContent = "";
    var p = document.createElement("p");
    p.textContent = stalled
      ? "This started " + j.elapsed + " ago and has not finished, so it has most likely stopped. Please try again."
      : j.error;
    live.appendChild(p);
    var first = null;
    if (j.back_url) {
      var back = link(j.back_url, "Go back and try again");
      var pb = document.createElement("p");
      pb.appendChild(back);
      live.appendChild(pb);
      first = back;
    }
    var all = link("/jobs", "See all your recent work");
    var pa = document.createElement("p");
    pa.appendChild(all);
    live.appendChild(pa);
    (first || all).focus();
  }
  function couldNotCheck() {
    failures += 1;
    if (failures >= 3) {
      live.textContent = "We could not check on your work. Use Check again.";
      return;
    }
    setTimeout(poll, 8000);
  }
  function poll() {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/jobs/" + id + ".json");
    xhr.onload = function () {
      var j;
      try { j = JSON.parse(xhr.responseText); } catch (e) { return couldNotCheck(); }
      if (xhr.status !== 200 || !j || !j.state) return couldNotCheck();
      failures = 0;
      if (j.state === "done") return finished(j);
      if (j.state === "failed" || j.state === "stalled") return stopped(j);
      if (elapsed && j.elapsed) elapsed.textContent = j.elapsed;
      if (step && j.progress && j.progress !== lastStep) {
        lastStep = j.progress;
        step.textContent = j.progress;
        live.textContent = "Now: " + j.progress;
      }
      setTimeout(poll, 4000);
    };
    xhr.onerror = couldNotCheck;
    xhr.send();
  }
  setTimeout(poll, 3000);
})();
