// web/weblogin.js 권한 판정이 swap.py 와 같은지(perm_cases.json 공통 사례). node test_weblogin.js
const assert = require("assert");
const W = require("./web/weblogin.js");
const C = require("./perm_cases.json");
const A = "Microsoft.Web/sites/slotsswap/Action";
let n = 0;
for (const c of C.allowed) {
  const [ok, why] = W.swapAllowed(c.perms);
  assert.strictEqual(ok, c.ok, c.name);
  if (c.why) assert.ok(why.includes(c.why), c.name + ": " + why);
  n++;
}
for (const [p, ok] of C.match) { assert.strictEqual(W.actionMatch(p, A), ok, p); n++; }
console.log(`weblogin: ${n}건 OK`);
