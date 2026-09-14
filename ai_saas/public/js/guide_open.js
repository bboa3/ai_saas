// Guide-open beacon (content layer 2, docs/content-layers-implementation.md).
// Loaded on every website page (hooks.web_include_js); does nothing unless the URL
// carries the per-company guide parameters ?o=<opportunity>&t=<token>. One quiet
// call, no UI, and the parameters are removed from the address bar afterwards so a
// copied link stops carrying the company's token around.
(function () {
  try {
    var q = new URLSearchParams(window.location.search);
    var o = q.get("o"), t = q.get("t");
    if (!o || !t) return;
    var body = "o=" + encodeURIComponent(o) + "&t=" + encodeURIComponent(t);
    fetch("/api/method/ai_saas.api.content.guide_opened", {
      method: "POST", credentials: "same-origin", keepalive: true,
      headers: { "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
      body: body
    }).catch(function () {});
    if (window.history && history.replaceState) {
      q.delete("o"); q.delete("t");
      var rest = q.toString();
      history.replaceState(null, "", window.location.pathname + (rest ? "?" + rest : "") + window.location.hash);
    }
  } catch (e) {}
})();
