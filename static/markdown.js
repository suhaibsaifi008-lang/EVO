/* Minimal offline-safe markdown renderer for EVO replies.
   Supports: fenced code blocks, inline code, bold/italic, links,
   unordered/ordered lists, headings, paragraphs. Escapes HTML first. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function inline(text) {
    let out = esc(text);
    out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    out = out.replace(/(^|[\s])(https?:\/\/[^\s<]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
    return out;
  }

  window.mdRender = function (src) {
    const lines = String(src || "").split(/\r?\n/);
    const out = [];
    let inCode = false, codeBuf = [], listMode = null, paraBuf = [];

    function flushPara() {
      if (paraBuf.length) {
        out.push("<p>" + inline(paraBuf.join(" ")) + "</p>");
        paraBuf = [];
      }
    }
    function flushList() {
      if (listMode) { out.push(listMode === "ul" ? "</ul>" : "</ol>"); listMode = null; }
    }

    for (const raw of lines) {
      const fence = raw.match(/^```(\w*)\s*$/);
      if (fence) {
        if (inCode) {
          out.push('<pre><code>' + esc(codeBuf.join("\n")) + "</code></pre>");
          codeBuf = []; inCode = false;
        } else { flushPara(); flushList(); inCode = true; }
        continue;
      }
      if (inCode) { codeBuf.push(raw); continue; }

      if (!raw.trim()) { flushPara(); flushList(); continue; }

      const h = raw.match(/^(#{1,4})\s+(.*)$/);
      if (h) { flushPara(); flushList(); const l = h[1].length;
        out.push(`<h${l + 2}>` + inline(h[2]) + `</h${l + 2}>`); continue; }

      const ul = raw.match(/^\s*[-*•]\s+(.*)$/);
      const ol = raw.match(/^\s*(\d+)[.)]\s+(.*)$/);
      if (ul) { flushPara();
        if (listMode !== "ul") { flushList(); out.push("<ul>"); listMode = "ul"; }
        out.push("<li>" + inline(ul[1]) + "</li>"); continue; }
      if (ol) { flushPara();
        if (listMode !== "ol") { flushList(); out.push("<ol>"); listMode = "ol"; }
        out.push("<li>" + inline(ol[2]) + "</li>"); continue; }

      flushList();
      paraBuf.push(raw.trim());
    }
    if (inCode && codeBuf.length) out.push("<pre><code>" + esc(codeBuf.join("\n")) + "</code></pre>");
    flushPara(); flushList();
    return out.join("");
  };
})();
