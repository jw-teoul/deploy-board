/* 픽셀 사무실 장면 — 레포 하나 = 캔버스 하나. 방 5개(브랜치·PR·develop·스테이징·운영), 이슈 = 캐릭터.
   단계가 바뀌면 캐릭터가 복도를 따라 다음 방으로 걸어간다. 외부 라이브러리 없음.
   스프라이트: pixel-agents(MIT) 번들 — 캐릭터 원본 JIK-A-4 MetroCity(CC0). 16px 타일, 캐릭터 16x32(행: down/up/right, 열: 걷기3·타이핑2·읽기2). */
(function () {
  const T = 16, ROOM_W = 13, ROOM_H = 9, WALL_H = 2, GAP = 1;
  const STAGES = ["branch", "pr", "develop", "staging", "prod"];
  const ROOM_NAME = { branch: "대기실 (브랜치)", pr: "리뷰실 (PR)", develop: "개발실 (develop)", staging: "스테이징", prod: "운영 서버실" };
  const ROOM_COLOR = { branch: "#9ca3af", pr: "#f59e0b", develop: "#3b82f6", staging: "#8b5cf6", prod: "#16a34a" };
  const FLOOR_OF = { branch: 1, pr: 2, develop: 0, staging: 3, prod: 0 };
  const WORLD_W = GAP + STAGES.length * (ROOM_W + GAP);     // 타일
  const WORLD_H = WALL_H + ROOM_H;                           // 타일
  const CORR = WALL_H + ROOM_H - 1;                          // 복도(문이 있는 맨 아래 줄) y 타일
  const WALK = 48;                                           // px/초(월드 기준)
  const FRAME = { walk: [0, 1, 2, 1], typing: [3, 4], reading: [5, 6] };
  const ROW = { down: 0, up: 1, right: 2 };

  // ---------- 이미지 ----------
  const IMG = {};
  const FILES = {
    chars: [0, 1, 2, 3, 4, 5].map(i => `assets/characters/char_${i}.png`),
    floors: [0, 1, 2, 3].map(i => `assets/tiles/floor_${i}.png`),
    wall: "assets/tiles/wall_0.png",
    DESK: "assets/furniture/DESK/DESK_FRONT.png", PC: "assets/furniture/PC/PC_FRONT_ON_1.png", PC2: "assets/furniture/PC/PC_FRONT_ON_2.png", PCB: "assets/furniture/PC/PC_BACK.png",
    CHAIR: "assets/furniture/CUSHIONED_CHAIR/CUSHIONED_CHAIR_FRONT.png", SOFA: "assets/furniture/SOFA/SOFA_FRONT.png",
    WHITEBOARD: "assets/furniture/WHITEBOARD/WHITEBOARD.png", PLANT: "assets/furniture/PLANT/PLANT.png", LPLANT: "assets/furniture/LARGE_PLANT/LARGE_PLANT.png",
    BOOKSHELF: "assets/furniture/BOOKSHELF/BOOKSHELF.png", COFFEE: "assets/furniture/COFFEE/COFFEE.png", CLOCK: "assets/furniture/CLOCK/CLOCK.png",
    BIN: "assets/furniture/BIN/BIN.png", TABLE: "assets/furniture/TABLE_FRONT/TABLE_FRONT.png",
  };
  let ready = null;
  function load() {
    if (ready) return ready;
    const one = src => new Promise(res => { const im = new Image(); im.onload = () => res(im); im.onerror = () => res(null); im.src = src; });
    ready = (async () => {
      IMG.chars = await Promise.all(FILES.chars.map(one));
      IMG.floors = await Promise.all(FILES.floors.map(one));
      for (const k of Object.keys(FILES)) if (k !== "chars" && k !== "floors") IMG[k] = await one(FILES[k]);
    })();
    return ready;
  }

  // ---------- 방 배치 ----------
  function roomX(i) { return GAP + i * (ROOM_W + GAP); }           // 방 왼쪽 타일 x
  function layout(stage, i) {
    const x0 = roomX(i), y0 = WALL_H;
    const deco = [], seats = [], stands = [];
    const desk = (dx, dy) => {                                     // dx,dy: 방 안 타일 좌표(책상 왼쪽 위)
      deco.push({ img: "DESK", x: x0 + dx, y: y0 + dy, w: 3, h: 2, sort: (y0 + dy + 2) * T });
      deco.push({ img: "PC", x: x0 + dx + 2, y: y0 + dy - 1, w: 1, h: 2, sort: (y0 + dy + 2) * T + 1, anim: true });
      seats.push({ x: x0 + dx + 1, y: y0 + dy, pose: "typing", dir: "down", sit: true });
    };
    const stand = (dx, dy, pose, dir) => stands.push({ x: x0 + dx, y: y0 + dy, pose: pose || "idle", dir: dir || "down" });
    if (stage === "branch") {
      deco.push({ img: "SOFA", x: x0 + 1, y: y0 + 1, w: 2, h: 1, sort: (y0 + 2) * T });
      deco.push({ img: "TABLE", x: x0 + 5, y: y0 + 1, w: 3, h: 4, sort: (y0 + 5) * T });
      deco.push({ img: "LPLANT", x: x0 + 10, y: y0, w: 2, h: 3, sort: (y0 + 3) * T });
      deco.push({ img: "COFFEE", x: x0 + 3, y: y0 + 1, w: 1, h: 1, sort: (y0 + 2) * T });
      deco.push({ img: "CLOCK", x: x0 + 6, y: 0, w: 1, h: 2, wall: true });
      for (const dy of [3, 5, 7]) for (let dx = 1; dx <= 11; dx += 2) stand(dx, dy, "idle", "down");
    } else if (stage === "pr") {
      deco.push({ img: "WHITEBOARD", x: x0 + 2, y: 0, w: 2, h: 2, wall: true });
      deco.push({ img: "WHITEBOARD", x: x0 + 8, y: 0, w: 2, h: 2, wall: true });
      deco.push({ img: "TABLE", x: x0 + 5, y: y0 + 2, w: 3, h: 4, sort: (y0 + 6) * T });
      deco.push({ img: "PLANT", x: x0 + 11, y: y0 + 0, w: 1, h: 2, sort: (y0 + 2) * T });
      deco.push({ img: "BIN", x: x0 + 0, y: y0 + 7, w: 1, h: 1, sort: (y0 + 8) * T });
      for (const dy of [1, 3, 5, 7]) for (const dx of [1, 3, 9, 11]) stand(dx, dy, "reading", "down");
      for (const dx of [4, 8]) for (const dy of [2, 4, 6]) stand(dx, dy, "reading", dx < 6 ? "right" : "left");
    } else {
      if (stage === "prod") {
        for (const dx of [1, 4, 7, 10]) deco.push({ img: "BOOKSHELF", x: x0 + dx, y: 0, w: 2, h: 1, wall: true });
        for (const dx of [0, 2, 4, 6, 8, 10, 12]) deco.push({ img: "PCB", x: x0 + dx, y: y0 + 7, w: 1, h: 2, sort: (y0 + 9) * T });
      } else {
        deco.push({ img: "CLOCK", x: x0 + 6, y: 0, w: 1, h: 2, wall: true });
        deco.push({ img: "PLANT", x: x0 + 12, y: y0 + 0, w: 1, h: 2, sort: (y0 + 2) * T });
        deco.push({ img: "BIN", x: x0 + 0, y: y0 + 7, w: 1, h: 1, sort: (y0 + 8) * T });
      }
      for (const dy of [1, 4]) for (const dx of [1, 5, 9]) desk(dx, dy);
      for (const dy of [3, 6]) for (let dx = 0; dx <= 12; dx += 2) stand(dx, dy, "idle", "up");
      if (stage !== "prod") for (let dx = 1; dx <= 11; dx += 2) stand(dx, 7, "idle", "down");
    }
    return { stage, i, x0, deco, spots: seats.concat(stands) };
  }

  // ---------- 캐릭터 ----------
  function hash(s) { let h = 2166136261; for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 16777619); return Math.abs(h); }
  class Char {
    constructor(issue, office) {
      this.key = issue.key; this.issue = issue; this.office = office;
      this.sprite = hash(issue.key) % 6;
      this.x = 0; this.y = 0;             // 픽셀(발 위치 기준: 타일 좌하단)
      this.path = []; this.dir = "down"; this.pose = "idle"; this.t = Math.random() * 10;
      this.spot = null; this.stage = null; this.leaving = false;
    }
    setTarget(spot, stage) {
      this.spot = spot; this.stage = stage;
      const tx = spot.x * T, ty = spot.y * T;
      if (this.x === 0 && this.y === 0) { this.x = tx; this.y = ty; this.path = []; this.pose = spot.pose; this.dir = spot.dir; return; }
      // 현재 → 복도(같은 x) → 목표 x → 목표 y
      this.path = [{ x: this.x, y: CORR * T }, { x: tx, y: CORR * T }, { x: tx, y: ty }];
      this.pose = "walk";
    }
    enterFrom(spot, stage, fromX) {
      this.x = fromX; this.y = CORR * T; this.spot = spot; this.stage = stage;
      this.path = [{ x: spot.x * T, y: CORR * T }, { x: spot.x * T, y: spot.y * T }];
      this.pose = "walk";
    }
    leave(toX) { this.leaving = true; this.path = [{ x: this.x, y: CORR * T }, { x: toX, y: CORR * T }]; this.pose = "walk"; }
    step(dt) {
      this.t += dt;
      if (!this.path.length) return;
      const p = this.path[0], dx = p.x - this.x, dy = p.y - this.y, d = Math.hypot(dx, dy), mv = WALK * dt;
      if (d <= mv) { this.x = p.x; this.y = p.y; this.path.shift(); if (!this.path.length) { this.pose = this.leaving ? "gone" : this.spot.pose; this.dir = this.spot.dir; } return; }
      this.x += dx / d * mv; this.y += dy / d * mv;
      this.dir = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? "right" : "left") : (dy > 0 ? "down" : "up");
    }
    frame() {
      if (this.pose === "walk") return FRAME.walk[Math.floor(this.t / 0.15) % 4];
      if (this.pose === "typing") return FRAME.typing[Math.floor(this.t / 0.3) % 2];
      if (this.pose === "reading") return FRAME.reading[Math.floor(this.t / 0.6) % 2];
      return 1;
    }
    draw(ctx) {
      const im = IMG.chars[this.sprite]; if (!im) return;
      const flip = this.dir === "left", row = ROW[flip ? "right" : this.dir] ?? 0, f = this.frame();
      const sitOff = this.pose === "typing" && this.spot?.sit ? 6 : 0;
      const dx = Math.round(this.x), dy = Math.round(this.y) - 2 * T + T + sitOff; // 발이 타일 바닥에
      ctx.save();
      if (flip) { ctx.translate(dx + T, 0); ctx.scale(-1, 1); ctx.drawImage(im, f * T, row * 2 * T, T, 2 * T, 0, dy, T, 2 * T); }
      else ctx.drawImage(im, f * T, row * 2 * T, T, 2 * T, dx, dy, T, 2 * T);
      ctx.restore();
    }
    get sortY() { return this.y + T - (this.pose === "typing" && this.spot?.sit ? 6 : 0); }
  }

  // ---------- 사무실 ----------
  const offices = new Map();
  class Office {
    constructor(repo) {
      this.repo = repo; this.rooms = STAGES.map((s, i) => layout(s, i));
      this.chars = new Map(); this.canvas = null; this.ctx = null;
      this.world = document.createElement("canvas"); this.world.width = WORLD_W * T; this.world.height = WORLD_H * T;
      this.wctx = this.world.getContext("2d"); this.wctx.imageSmoothingEnabled = false;
      this.bg = null; this.scale = 1; this.hits = []; this.last = performance.now(); this.raf = null; this.counts = {};
      try { this.prev = JSON.parse(localStorage.getItem("px:" + repo) || "{}"); } catch { this.prev = {}; }
      this.onClick = null;
    }
    attach(canvas) {
      this.canvas = canvas; this.ctx = canvas.getContext("2d");
      canvas.onclick = e => { const h = this.hit(e); if (h && this.onClick) this.onClick(h.key); };
      canvas.onmousemove = e => { const h = this.hit(e); canvas.style.cursor = h ? "pointer" : "default"; canvas.title = h ? `${h.key} ${h.issue.title || ""}` : ""; };
      this.resize();
      if (!this.raf) this.loop();
    }
    hit(e) {
      const r = this.canvas.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
      return this.hits.find(h => x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + h.h);
    }
    resize() {
      const cssW = this.canvas.parentElement.clientWidth || 1100;
      this.scale = Math.max(1, Math.min(2.5, cssW / (WORLD_W * T)));
      const w = Math.round(WORLD_W * T * this.scale), h = Math.round(WORLD_H * T * this.scale);
      const dpr = window.devicePixelRatio || 1;
      this.canvas.style.width = w + "px"; this.canvas.style.height = h + "px";
      this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr);
      this.dpr = dpr; this.cssW = w; this.cssH = h;
    }
    update(issues) {
      // 방별 자리 배정: 최근 활동순으로 앞자리. 자리 수를 넘는 이슈는 「+N」으로만 표시.
      const byStage = {}; for (const s of STAGES) byStage[s] = [];
      for (const i of issues) if (byStage[i.stage]) byStage[i.stage].push(i);
      const keep = new Set(); this.counts = {};
      const fresh = !this.seeded; this.seeded = true;                 // 페이지 첫 배치는 걷지 않고 바로 앉힌다
      let stagger = 0;
      for (const room of this.rooms) {
        const list = byStage[room.stage]; this.counts[room.stage] = { n: list.length, over: Math.max(0, list.length - room.spots.length) };
        list.slice(0, room.spots.length).forEach((issue, idx) => {
          const spot = room.spots[idx]; keep.add(issue.key);
          let c = this.chars.get(issue.key);
          if (!c) {
            c = new Char(issue, this); this.chars.set(issue.key, c);
            const prevStage = this.prev[issue.key];
            if (!fresh && prevStage && prevStage !== issue.stage) { // 지난 수집 때와 단계가 다르면 그 방에서 걸어온다
              const pr = this.rooms[STAGES.indexOf(prevStage)]; c.x = (pr.x0 + 6) * T; c.y = (WALL_H + 3) * T; c.setTarget(spot, issue.stage);
            } else if (!fresh && !prevStage) { c.enterFrom(spot, issue.stage, -T - stagger * 12); stagger++; } // 새 이슈는 왼쪽 문으로 들어온다
            else c.setTarget(spot, issue.stage);
          } else { c.issue = issue; c.leaving = false; if (c.spot !== spot) c.setTarget(spot, issue.stage); }
          this.prev[issue.key] = issue.stage;
        });
      }
      for (const [k, c] of this.chars) if (!keep.has(k) && !c.leaving) c.leave(WORLD_W * T + T);
      try { localStorage.setItem("px:" + this.repo, JSON.stringify(this.prev)); } catch {}
    }
    drawBackground() {
      const c = document.createElement("canvas"); c.width = WORLD_W * T; c.height = WORLD_H * T;
      const g = c.getContext("2d"); g.imageSmoothingEnabled = false;
      g.fillStyle = "#1f2430"; g.fillRect(0, 0, c.width, c.height);
      for (const room of this.rooms) {
        const fl = IMG.floors[FLOOR_OF[room.stage]] || IMG.floors[0];
        for (let y = WALL_H; y < WORLD_H; y++) for (let x = room.x0; x < room.x0 + ROOM_W; x++) fl ? g.drawImage(fl, x * T, y * T) : (g.fillStyle = "#d9d2c5", g.fillRect(x * T, y * T, T, T));
        // 벽(윗면) — 벽 시트의 한 조각을 반복, 방 색 띠
        for (let x = room.x0; x < room.x0 + ROOM_W; x++) IMG.wall ? g.drawImage(IMG.wall, 0, 0, T, 2 * T, x * T, 0, T, 2 * T) : (g.fillStyle = "#8d8679", g.fillRect(x * T, 0, T, 2 * T));
        g.fillStyle = ROOM_COLOR[room.stage]; g.fillRect(room.x0 * T, 2 * T - 3, ROOM_W * T, 3);
        // 벽에 거는 장식
        for (const d of room.deco) if (d.wall && IMG[d.img]) g.drawImage(IMG[d.img], d.x * T, d.y * T);
        // 문(복도 줄, 오른쪽 칸막이)
        g.fillStyle = "#3b3f4a"; g.fillRect((room.x0 + ROOM_W) * T, WALL_H * T, GAP * T, (ROOM_H - 1) * T);
        g.fillStyle = "#c9b68a"; g.fillRect((room.x0 + ROOM_W) * T, CORR * T, GAP * T, T);
      }
      g.fillStyle = "#c9b68a"; g.fillRect(0, CORR * T, GAP * T, T); // 입구
      this.bg = c;
    }
    loop() {
      this.raf = requestAnimationFrame(() => this.loop());
      if (!this.canvas || !this.canvas.isConnected) { this.raf = null; return; }
      const now = performance.now(), dt = Math.min(0.1, (now - this.last) / 1000); this.last = now;
      if (!IMG.chars) return;
      if (!this.bg) this.drawBackground();
      const w = this.wctx; w.drawImage(this.bg, 0, 0);
      // 깊이 정렬: 가구(바닥)·캐릭터
      const items = [];
      for (const room of this.rooms) for (const d of room.deco) if (!d.wall) items.push({ y: d.sort, draw: () => { let im = IMG[d.img]; if (d.anim && Math.floor(now / 400) % 2) im = IMG.PC2 || im; if (im) w.drawImage(im, d.x * T, d.y * T); } });
      for (const [k, c] of [...this.chars]) { c.step(dt); if (c.pose === "gone") { this.chars.delete(k); continue; } items.push({ y: c.sortY, draw: () => c.draw(w), c }); }
      items.sort((a, b) => a.y - b.y);
      for (const it of items) it.draw();
      // 화면으로 확대(픽셀 유지) + 이름표·방 이름
      const ctx = this.ctx, S = this.scale * this.dpr;
      ctx.imageSmoothingEnabled = false; ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
      ctx.drawImage(this.world, 0, 0, this.world.width, this.world.height, 0, 0, this.world.width * S, this.world.height * S);
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      const s = this.scale;
      ctx.font = `bold ${Math.max(12, Math.round(9 * s))}px system-ui, "Malgun Gothic", sans-serif`; ctx.textBaseline = "middle";
      for (const room of this.rooms) {
        const cnt = this.counts[room.stage] || { n: 0, over: 0 };
        const label = `${ROOM_NAME[room.stage]}  ${cnt.n}` + (cnt.over ? `  (+${cnt.over} 자리 없음)` : "");
        const x = (room.x0 + 0.3) * T * s, y = 0.55 * T * s, tw = ctx.measureText(label).width + 8;
        ctx.fillStyle = "rgba(0,0,0,.6)"; ctx.fillRect(x, y - 8 * s, tw, 16 * s);
        ctx.fillStyle = "#fff"; ctx.fillText(label, x + 4, y);
      }
      ctx.font = `${Math.max(9, Math.round(5.5 * s))}px system-ui, "Malgun Gothic", sans-serif`;
      this.hits = [];
      for (const it of items) {
        if (!it.c) continue; const c = it.c;
        const tag = c.key.replace(/^PILLDOC-/, "");
        const cx = (c.x + T / 2) * s, top = (c.y - T) * s, tw = ctx.measureText(tag).width + 6, th = 9 * s;
        ctx.fillStyle = ROOM_COLOR[c.stage] || "#333"; ctx.globalAlpha = .9; ctx.fillRect(cx - tw / 2, top - th - 2, tw, th); ctx.globalAlpha = 1;
        ctx.fillStyle = "#fff"; ctx.textAlign = "center"; ctx.fillText(tag, cx, top - th / 2 - 2); ctx.textAlign = "left";
        this.hits.push({ key: c.key, issue: c.issue, x: cx - 8 * s, y: top - th - 2, w: 16 * s, h: th + 2 * T * s });
      }
    }
  }

  window.PixelOffice = {
    async mount(canvas, repo, issues, onClick) {
      await load();
      let o = offices.get(repo); if (!o) { o = new Office(repo); offices.set(repo, o); }
      o.onClick = onClick; o.attach(canvas); o.update(issues);
      return o;
    },
    resizeAll() { for (const o of offices.values()) if (o.canvas?.isConnected) o.resize(); },
  };
  window.addEventListener("resize", () => window.PixelOffice.resizeAll());
})();
