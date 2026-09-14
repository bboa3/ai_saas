// /alerta-fiscal — one step, calling ai_saas.api.content.subscribe. Same posture as
// /registo: direct fetch (no frappe.call dialogs), every message shown inline.
(function () {
  var $ = function (sel) { return document.querySelector(sel); };
  var val = function (id) { var e = document.getElementById(id); return e ? e.value.trim() : ""; };
  function showError(field, msg) {
    var e = document.querySelector('.reg__err[data-for="' + field + '"]'); var input = document.getElementById(field);
    if (e) { e.textContent = msg || ""; e.classList.toggle("show", !!msg); }
    if (input) { input.classList.toggle("is-invalid", !!msg); input.classList.toggle("is-valid", !msg && !!input.value); }
  }
  var rules = {
    full_name: function (v) { return v.length < 2 ? "Indique o seu nome." : ""; },
    email: function (v) { return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v) ? "" : "Indique um email válido, ex. nome@empresa.co.mz."; },
    company_name: function (v) { return v.length < 2 ? "Indique o nome da empresa." : ""; },
    segment: function (v) { return v ? "" : "Escolha o sector de actividade."; }
  };
  function validate() {
    var ok = true;
    Object.keys(rules).forEach(function (f) { var m = rules[f](val(f)); showError(f, m); if (m) ok = false; });
    return ok;
  }
  function serverMessage(body) {
    try {
      var msgs = JSON.parse(body._server_messages);
      var m = JSON.parse(msgs[msgs.length - 1]).message;
      if (m) return String(m).replace(/<[^>]+>/g, "");
    } catch (e) {}
    return "Não foi possível subscrever. Tente novamente.";
  }
  var btn = $("#btn-subscribe");
  if (!btn) return;
  Object.keys(rules).forEach(function (f) {
    var el = document.getElementById(f);
    if (el) el.addEventListener("blur", function () { showError(f, rules[f](val(f))); });
  });
  btn.addEventListener("click", function () {
    if (!validate()) return;
    var err = $("#alert-error"); err.hidden = true;
    btn.disabled = true; btn.textContent = "A subscrever…";
    var headers = { "Content-Type": "application/json", "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" };
    var csrf = (window.frappe && frappe.csrf_token) || window.csrf_token || "";
    if (csrf && csrf !== "None") headers["X-Frappe-CSRF-Token"] = csrf;
    fetch("/api/method/ai_saas.api.content.subscribe", {
      method: "POST", credentials: "same-origin", headers: headers,
      body: JSON.stringify({ full_name: val("full_name"), email: val("email"), company_name: val("company_name"), segment: val("segment"), tax_regime: val("tax_regime") })
    }).then(function (r) { return r.text().then(function (t) { var j = null; try { j = t ? JSON.parse(t) : null; } catch (e) {} return { ok: r.ok, body: j }; }); })
      .then(function (r) {
        if (!(r.ok && r.body && !r.body.exc)) throw new Error(serverMessage(r.body || {}));
        $("#alert-form").hidden = true; $("#alert-done").hidden = false;
        try { if (window.fbq && (window.MZ_ALERT || {}).pixel) fbq("track", "Lead"); } catch (e) {}
      })
      .catch(function (e) {
        err.textContent = e.message || "Não foi possível subscrever. Tente novamente."; err.hidden = false;
        btn.disabled = false; btn.textContent = "Quero receber o Alerta Fiscal";
      });
  });
})();
