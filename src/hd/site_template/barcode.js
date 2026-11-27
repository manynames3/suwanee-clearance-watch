/* UPC-A / EAN-13 barcode as SVG. Client-side only, no dependencies.
 * UPC-A is EAN-13 with a leading zero, so one encoder serves both. */
(function (root) {
  var L = ["0001101","0011001","0010011","0111101","0100011","0110001","0101111","0111011","0110111","0001011"];
  var G = ["0100111","0110011","0011011","0100001","0011101","0111001","0000101","0010001","0001001","0010111"];
  var R = ["1110010","1100110","1101100","1000010","1011100","1001110","1010000","1000100","1001000","1110100"];
  var PARITY = ["LLLLLL","LLGLGG","LLGGLG","LLGGGL","LGLLGG","LGGLLG","LGGGLL","LGLGLG","LGLGGL","LGGLGL"];

  function checkDigit(first12) {
    var sum = 0;
    for (var i = 0; i < 12; i++) sum += Number(first12[i]) * (i % 2 === 0 ? 1 : 3);
    return (10 - (sum % 10)) % 10;
  }

  /* Returns a valid 13-digit string, or null. Accepts 12 (UPC-A), 13 (EAN-13)
   * and 14 digits when the first is a zero (GTIN-14 of a UPC). */
  function normalize(raw) {
    var d = String(raw == null ? "" : raw).replace(/\D/g, "");
    if (d.length === 14 && d[0] === "0") d = d.slice(1);
    if (d.length === 12) d = "0" + d;
    if (d.length !== 13) return null;
    return checkDigit(d.slice(0, 12)) === Number(d[12]) ? d : null;
  }

  /* The 95 modules as a string of 0/1. */
  function modules(d13) {
    var parity = PARITY[Number(d13[0])];
    var out = "101";
    for (var i = 1; i <= 6; i++) {
      out += (parity[i - 1] === "L" ? L : G)[Number(d13[i])];
    }
    out += "01010";
    for (var j = 7; j <= 12; j++) out += R[Number(d13[j])];
    return out + "101";
  }

  function svg(d13, opts) {
    opts = opts || {};
    var quiet = 9, unit = opts.unit || 2, h = opts.height || 70;
    var m = modules(d13), ns = "http://www.w3.org/2000/svg";
    var width = (m.length + quiet * 2) * unit;
    var s = document.createElementNS(ns, "svg");
    s.setAttribute("viewBox", "0 0 " + width + " " + (h + 18));
    s.setAttribute("role", "img");
    s.setAttribute("aria-label", "Barcode " + d13);
    var bg = document.createElementNS(ns, "rect");
    bg.setAttribute("width", width); bg.setAttribute("height", h + 18); bg.setAttribute("fill", "#fff");
    s.appendChild(bg);
    var i = 0;
    while (i < m.length) {
      if (m[i] === "1") {
        var j = i;
        while (j < m.length && m[j] === "1") j++;
        var r = document.createElementNS(ns, "rect");
        r.setAttribute("x", (quiet + i) * unit); r.setAttribute("y", 0);
        r.setAttribute("width", (j - i) * unit); r.setAttribute("height", h);
        r.setAttribute("fill", "#000");
        s.appendChild(r);
        i = j;
      } else i++;
    }
    var t = document.createElementNS(ns, "text");
    t.setAttribute("x", width / 2); t.setAttribute("y", h + 14);
    t.setAttribute("text-anchor", "middle"); t.setAttribute("font-size", "13");
    t.setAttribute("font-family", "monospace"); t.setAttribute("fill", "#000");
    t.textContent = d13[0] === "0" ? d13.slice(1) : d13;
    s.appendChild(t);
    return s;
  }

  var api = { normalize: normalize, modules: modules, checkDigit: checkDigit, svg: svg };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.HDBarcode = api;
})(typeof window !== "undefined" ? window : this);
