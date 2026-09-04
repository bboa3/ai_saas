// /registo — three steps, resumable, calling ai_saas.api.signup (A7).
(function () {
  var cfg = window.MZ_SIGNUP || {};
  var TOKEN_KEY = "mz_signup_token";
  var token = cfg.token || "";
  // Direct call to /api/method — not frappe.call, whose status-code handlers open a
  // "Pedido inválido" / "Não permitido" dialog on every refused request (a stale resume
  // token on page load, a validation refusal) before our own handling runs. All messages
  // are shown by this page, inline.
  var api = function (method, args) {
    return new Promise(function (resolve, reject) {
      var done = false;
      var timer = setTimeout(function () { if (!done) { done = true; console.error("registo:", method, "timed out"); reject("Sem resposta do servidor. Verifique a ligação e tente novamente."); } }, 25000);
      var headers = { "Content-Type": "application/json", "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" };
      var csrf = (window.frappe && frappe.csrf_token) || (window.csrf_token) || "";
      if (csrf && csrf !== "None") headers["X-Frappe-CSRF-Token"] = csrf;
      fetch("/api/method/ai_saas.api.signup." + method, { method: "POST", credentials: "same-origin", headers: headers, body: JSON.stringify(args || {}) })
        .then(function (res) { return res.text().then(function (t) { var j = null; try { j = t ? JSON.parse(t) : null; } catch (e) {} return { ok: res.ok, status: res.status, body: j }; }); })
        .then(function (r) {
          if (done) return; done = true; clearTimeout(timer);
          if (r.ok && r.body && !r.body.exc) { resolve(r.body.message); return; }
          console.error("registo:", method, "failed", r.status, r.body);
          reject(serverMessage(r.body));
        })
        .catch(function (e) { if (done) return; done = true; clearTimeout(timer); console.error("registo:", method, "network", e); reject("Não foi possível contactar o servidor. Verifique a ligação e tente novamente."); });
    });
  };
  function serverMessage(body) {
    try {
      var msgs = JSON.parse(body._server_messages);
      var m = JSON.parse(msgs[msgs.length - 1]).message;
      if (m) return String(m).replace(/<[^>]+>/g, "");
    } catch (e) {}
    try { if (body.exception) { var ex = String(body.exception); var i = ex.lastIndexOf(": "); if (i > -1 && ex.length - i < 200) return ex.slice(i + 2); } } catch (e) {}
    return "Não foi possível continuar. Tente novamente.";
  }
  function forgetToken() { token = ""; try { localStorage.removeItem(TOKEN_KEY); } catch (e) {} }
  function isStaleToken(msg) { return /inválida ou expirada/i.test(msg || ""); }
  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };
  var val = function (id) { var e = document.getElementById(id); return e ? (e.type === "checkbox" ? (e.checked ? 1 : 0) : e.value.trim()) : ""; };

  // ---- errors / validation ------------------------------------------------
  function showError(field, msg) {
    var e = document.querySelector('.reg__err[data-for="' + field + '"]'); var input = document.getElementById(field);
    if (e) { e.textContent = msg || ""; e.classList.toggle("show", !!msg); }
    if (input) { input.classList.toggle("is-invalid", !!msg); input.classList.toggle("is-valid", !msg && !!input.value); }
  }
  var rules = {
    full_name: function (v) { return v.length < 2 ? "Indique o seu nome." : ""; },
    email: function (v) { return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v) ? "" : "Indique um email válido, ex. nome@empresa.co.mz."; },
    phone: function (v) { return !v || v.replace(/\D/g, "").length >= 9 ? "" : "Indique um número com pelo menos 9 dígitos."; },
    company_name: function (v) { return v.length < 2 ? "Indique o nome da empresa." : ""; },
    tax_id: function (v) { return /^\d{9}$/.test(v.replace(/\D/g, "")) ? "" : "O NUIT tem 9 dígitos."; },
    industry: function (v) { return v || cfg.presetIndustry ? "" : "Escolha o sector de actividade."; },
    address: function (v) { return v.length >= 5 ? "" : "Indique o endereço: rua, bairro e cidade."; },
    // Only asked when the typed address named no city we could recognise.
    city: function (v) { return $("#city-row").hidden || v.length >= 2 ? "" : "Indique a cidade."; },
    subdomain: function (v) { return /^[a-z0-9][a-z0-9\-]{1,38}[a-z0-9]$/.test(v) ? "" : "Só letras minúsculas, números e hífens (3 a 40 caracteres)."; },
    users: function (v) { var min = cfg.minimumUsers || 2; return (parseInt(v, 10) || 0) >= min ? "" : "O plano é por utilizador — mínimo " + min + " utilizador(es)."; },
    terms_accepted: function (v) { return v ? "" : "É necessário aceitar os termos para criar a conta."; }
  };
  function validate(field) { var msg = rules[field] ? rules[field](val(field)) : ""; showError(field, msg); return !msg; }
  Object.keys(rules).forEach(function (f) {
    var el = document.getElementById(f); if (!el) return;
    el.addEventListener("blur", function () { if (el.value || el.type === "checkbox") validate(f); });
    el.addEventListener("input", function () { if (el.classList.contains("is-invalid")) validate(f); });
  });

  // ---- steps --------------------------------------------------------------
  var stepIndex = { "1": 1, "2": 2, "3": 3 };
  function show(step) {
    $$(".reg__step").forEach(function (s) { s.hidden = s.dataset.step !== String(step); });
    var n = stepIndex[step]; var p = $("#reg-progress");
    p.hidden = !n;
    if (n) { $("#reg-progress-text").textContent = "Passo " + n + " de 3"; $("#reg-bar").style.width = (n * 33.34) + "%"; }
    window.scrollTo(0, 0);
    var h = document.querySelector('.reg__step[data-step="' + step + '"] h1'); if (h) h.focus();
    if (step === 3) fillSummary();
  }
  // ---- plan: chosen on step 3, preselected from the pricing page's ?plan= -----
  function planLabel(name) { var r = document.querySelector('input[name="plan"][value="' + name + '"]'); return r ? r.parentNode.textContent.trim().replace(/\s+/g, " ") : name; }
  function currentPlan() { var r = document.querySelector('input[name="plan"]:checked'); return r ? r.value : cfg.preselectedPlan; }
  function setPlan(name) { var r = document.querySelector('input[name="plan"][value="' + name + '"]'); if (r) r.checked = true; $("#reg-plan-label").textContent = planLabel(currentPlan()); paintTotal(); }
  // Per-user pricing with the first user included: N seats bill N-1 plan costs (min 1).
  function paintTotal() {
    var out = $("#reg-plan-total"); if (!out) return;
    var r = document.querySelector('input[name="plan"]:checked');
    var users = parseInt(val("users"), 10) || 0;
    var cost = r ? parseFloat(r.dataset.cost) : 0;
    if (!r || !cost || users < 1) { out.textContent = ""; return; }
    var billed = Math.max(users - 1, 1);
    var per = (r.dataset.currency || "MZN") + "/" + (r.dataset.interval === "Month" ? "mês" : "ano");
    var fmt = function (n) { return n.toLocaleString("pt-PT", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); };
    out.textContent = users > 1
      ? users + " utilizadores (1.º incluído): " + billed + " × " + fmt(cost) + " = " + fmt(cost * billed) + " " + per
      : "1 utilizador = " + fmt(cost) + " " + per;
  }
  var usersInput = document.getElementById("users");
  if (usersInput) usersInput.addEventListener("input", paintTotal);
  setPlan(cfg.preselectedPlan);
  $("#reg-plan-change").addEventListener("click", function () {
    var c = $("#reg-plan-choices"); c.hidden = !c.hidden; this.setAttribute("aria-expanded", c.hidden ? "false" : "true");
    this.textContent = c.hidden ? "alterar" : "fechar";
  });
  $$('input[name="plan"]').forEach(function (r) { r.addEventListener("change", function () {
    $("#reg-plan-label").textContent = planLabel(r.value); paintTotal();
    var c = $("#reg-plan-choices"); c.hidden = true; $("#reg-plan-change").setAttribute("aria-expanded", "false"); $("#reg-plan-change").textContent = "alterar";
  }); });
  function fillSummary() {
    // The subdomain is automatic: the field stays hidden behind "Vai entrar em …" and the
    // discreet "alterar" button. A value the visitor typed or opened this session is theirs
    // — never overwritten. A restored value they never touched gets normalized, and replaced
    // by a fresh suggestion only if still invalid. Whenever the automation cannot deliver
    // (no company name, no suggestion, request failed), the field reveals itself.
    var current = val("subdomain");
    if (current && !subdomainTouched && rules.subdomain(current)) {
      var norm = normalizeSlug(current);
      if (norm !== current) { $("#subdomain").value = norm; current = norm; }
    }
    // Re-suggest when: empty, invalid, or THIS session's auto value was made for a company
    // name that has since been edited (subdomainFor). A value restored from a previous
    // session (subdomainFor === null) is kept — it may have been the visitor's own choice.
    var staleAuto = subdomainFor !== null && subdomainFor !== val("company_name");
    if (!subdomainTouched && val("company_name") && (!current || rules.subdomain(val("subdomain")) || staleAuto)) {
      var forName = val("company_name");
      api("suggest_subdomain", { company_name: forName, token: token }).then(function (r) {
        if (subdomainTouched) return;
        if (r.subdomain) { subdomainFor = forName; $("#subdomain").value = r.subdomain; previewDomain(); checkSubdomain(); }
        else if (!val("subdomain")) revealSubdomain(false);
      }).catch(function () { if (!val("subdomain")) revealSubdomain(false); });
    } else if (!current && !val("company_name")) {
      revealSubdomain(false);
    } else if (current) {
      checkSubdomain(); // reassurance badge (and reveal-on-taken) for a kept value
    }
    previewDomain();
  }
  // ---- subdomain availability ---------------------------------------------
  // The address bar mirrors the field (or shows the placeholder until a suggestion
  // lands); its state icon is where a browser puts its own — ✓ free, ✕ taken, spinner
  // while the server is asked.
  var ADDR_STATE_TEXT = { idle: "", checking: "A verificar disponibilidade", ok: "Disponível", bad: "Indisponível" };
  function previewDomain() {
    var slug = val("subdomain");
    $("#reg-addr-slug").textContent = slug || "sua-empresa";
    $("#reg-addressbar").classList.toggle("is-empty", !slug);
  }
  function setAddrState(state) {
    $("#reg-addr-state").dataset.state = state;
    $("#reg-addr-state-text").textContent = ADDR_STATE_TEXT[state] || "";
    $("#reg-addressbar").classList.toggle("is-bad", state === "bad");
  }
  var subTimer = null;
  // The field is hidden while the automation is doing fine; anything that needs the
  // visitor's eyes (an error, a taken slug, no suggestion) reveals it.
  function revealSubdomain(focus) {
    var row = $("#subdomain-row");
    if (row.hidden) { row.hidden = false; $("#reg-subdomain-change").hidden = true; }
    if (focus) $("#subdomain").focus();
  }
  $("#reg-subdomain-change").addEventListener("click", function () {
    subdomainTouched = true; // opened on purpose: the automation stops overwriting
    revealSubdomain(true);
  });
  function checkSubdomain() {
    previewDomain();
    var slug = val("subdomain");
    if (!validate("subdomain")) { setAddrState("bad"); revealSubdomain(false); return; }
    setAddrState("checking");
    clearTimeout(subTimer);
    subTimer = setTimeout(function () {
      api("check_subdomain", { subdomain: slug }).then(function (r) {
        if (val("subdomain") !== slug) return;
        if (r.available) { showError("subdomain", ""); setAddrState("ok"); } else { showError("subdomain", r.reason); setAddrState("bad"); revealSubdomain(false); }
      }).catch(function () { if (val("subdomain") === slug) setAddrState("idle"); });
    }, 350);
  }
  // What the visitor types becomes a slug as they type it: "Minha Empresa" turns into
  // "minha-empresa" instead of a validation error. Edge hyphens are left alone while
  // typing ("minha-" is a word in progress); the validator still catches them at the end.
  function normalizeSlug(v) {
    return v.toLowerCase()
      .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
      .replace(/[\s_]+/g, "-")
      .replace(/[^a-z0-9-]/g, "")
      .replace(/-{2,}/g, "-");
  }
  var subdomainTouched = false;
  var subdomainFor = null; // company name this session's auto-suggestion was made for
  $("#subdomain").addEventListener("input", function () {
    subdomainTouched = true;
    var input = this, raw = input.value, pos = input.selectionStart;
    var norm = normalizeSlug(raw);
    if (norm !== raw) {
      var caret = normalizeSlug(raw.slice(0, pos)).length;
      input.value = norm;
      input.setSelectionRange(caret, caret);
    }
    checkSubdomain();
  });

  // ---- navigation ---------------------------------------------------------
  $$("[data-back]").forEach(function (b) { b.addEventListener("click", function () { show(parseInt(b.dataset.back, 10) - 1); }); });
  $$("[data-next]").forEach(function (b) {
    b.addEventListener("click", function () {
      var step = parseInt(b.dataset.next, 10);
      var fields = step === 1 ? ["full_name", "email", "phone"] : ["company_name", "tax_id", "industry", "address", "city"];
      var ok = fields.map(validate).every(Boolean); if (!ok) return;
      b.disabled = true;
      var step1 = { full_name: val("full_name"), email: val("email"), phone: val("phone"), plan: currentPlan(), domain: cfg.domain || "" };
      var p = step === 1
        ? (token
            // Back to step 1 to correct something: the signup already exists — update it, never start again.
            ? api("update", { token: token, step: 1, data: step1 }).then(function (r) { if (r.state === "continue") r.step = 2; return r; })
            : api("start", step1).then(function (r) { if (r.token) { token = r.token; try { localStorage.setItem(TOKEN_KEY, token); } catch (e) {} } return r; }))
        : api("update", { token: token, step: 2, data: { company_name: val("company_name"), tax_id: val("tax_id"), tax_regime: val("tax_regime"), industry: val("industry"), address: val("address"), city: val("city") } });
      p.then(function (r) {
         b.disabled = false;
         // The address named no city: reveal the one extra question instead of failing.
         if (r.state === "need_city") { askCity(r.message); return; }
         if (r.state !== "continue") { terminal(r); return; }
         show(step + 1);
       })
       .catch(function (msg) {
         if (typeof msg !== "string") { console.error("registo:", msg); msg = "Não foi possível continuar. Tente novamente."; }
         if (isStaleToken(msg)) {
           // The stored signup no longer exists (deleted, or another site): forget it and begin again.
           forgetToken();
           if (step === 1) { b.disabled = false; b.click(); return; }
           show(1); b.disabled = false; showError("email", "O seu registo anterior já não existe. Confirme os dados e continue."); return;
         }
         b.disabled = false; showError(fields[fields.length - 1], msg);
       });
    });
  });
  $("#reg-submit").addEventListener("click", function () {
    var btn = this; var g = $("#reg-global-err"); g.classList.remove("show");
    if (!validate("subdomain")) { revealSubdomain(true); return; }
    if (!validate("users") || !validate("terms_accepted")) return;
    var label = btn.textContent; btn.disabled = true;
    // Creating the account is two calls and half a dozen documents — seconds, not
    // milliseconds. The waiting belongs on the progress screen, not on a dead button:
    // show it at once and let the answer arrive underneath it.
    working("A criar a sua conta…", "Estamos a preparar tudo. Não feche esta página — demora alguns segundos.");
    api("update", { token: token, step: 3, data: { subdomain: val("subdomain"), plan: currentPlan(), users: val("users"), terms_accepted: 1 } })
      .then(function () { return api("submit", { token: token }); })
      .then(function (r) { try { terminal(r); } catch (e) { console.error("registo: terminal", e); throw "Conta criada, mas a página não conseguiu mostrar o resultado. Verifique o seu email."; } })
      .catch(function (msg) {
        // Back to the form exactly as it was, with the reason under the button.
        show(3); btn.disabled = false; btn.textContent = label;
        if (isStaleToken(msg)) { forgetToken(); show(1); return; }
        g.textContent = typeof msg === "string" ? msg : "Não foi possível criar a conta. Tente novamente."; g.classList.add("show");
      });
  });

  function working(title, text) {
    show("progress");
    $("#reg-spinner").hidden = false;
    $("#reg-done-title").textContent = title;
    $("#reg-done-text").textContent = text;
    $("#reg-done-link").hidden = true;
    $("#reg-restart").hidden = true;
  }

  function askCity(message) {
    var row = $("#city-row"); row.hidden = false;
    showError("city", message || "Indique a cidade.");
    $("#city").focus();
  }
  console.log("registo.js", cfg.assetVersion || "");

  // ---- terminal states + polling -------------------------------------------
  var pollTimer = null, pollDelay = 15000;
  function terminal(r) {
    show("progress");
    var title = $("#reg-done-title"), text = $("#reg-done-text"), link = $("#reg-done-link"), spin = $("#reg-spinner");
    if (r.state === "progress") {
      pollTimer = setTimeout(function () {
        api("status", { token: token }).then(function (s) { pollDelay = 15000; terminal(s); })
          .catch(function () { pollDelay = Math.min(pollDelay * 2, 120000); terminal({ state: "progress" }); });
      }, pollDelay);
    } else if (r.state === "complete") {
      spin.hidden = true; title.textContent = "A sua conta está pronta";
      text.textContent = "Enviámos o email de entrega com o acesso. Verifique também a pasta de spam.";
      if (r.site_url) { link.href = r.site_url; link.hidden = false; }
    } else if (r.state === "failed") {
      spin.hidden = true; title.textContent = "Está a demorar mais do que o esperado"; text.textContent = r.message || "";
    } else if (r.state === "refused") {
      spin.hidden = true; title.textContent = "Não foi possível concluir"; text.textContent = r.message || "";
      // A refusal is about these data (a company that already has an account, a mistyped
      // NUIT) — never a locked browser: the visitor can always begin a new registration.
      $("#reg-restart").hidden = false;
    }
  }
  $("#reg-restart").addEventListener("click", function () { forgetToken(); location.href = cfg.route || "/registo"; });

  // ---- resume ----------------------------------------------------------------
  function restore(r) {
    if (!r) return;
    if (r.fields) {
      Object.keys(r.fields).forEach(function (k) {
        var el = document.getElementById(k); if (!el || r.fields[k] == null) return;
        if (el.type === "checkbox") el.checked = !!r.fields[k]; else el.value = r.fields[k];
      });
      if (r.fields.plan) setPlan(r.fields.plan); else paintTotal();
    }
    if (r.state === "continue") show(Math.min(Math.max(r.step || 1, 1), 3)); else terminal(r);
  }
  if (cfg.resume) { restore(cfg.resume); }
  else {
    var saved = null; try { saved = localStorage.getItem(TOKEN_KEY); } catch (e) {}
    // Forget the stored token only when the server says it no longer exists — a transient
    // failure (network, server error) must not throw away a valid signup.
    if (saved) { token = saved; api("status", { token: token }).then(restore).catch(function (msg) { if (isStaleToken(msg)) forgetToken(); }); }
  }
})();
