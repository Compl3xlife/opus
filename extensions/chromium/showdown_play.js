function pageShowdownPlay(opts) {
  const enable = !opts || opts.enabled !== false;
  if (!enable) {
    window.__opusShowdown = null;
    window.__opusShowdownTimer = false;
    window.__opusShowdownSearch = 0;
    return { ok: true, playing: false, message: "Showdown bot off." };
  }

  const TYPE_CHART = {
    Normal: { Rock: 0.5, Ghost: 0, Steel: 0.5 },
    Fire: { Fire: 0.5, Water: 0.5, Grass: 2, Ice: 2, Bug: 2, Rock: 0.5, Dragon: 0.5, Steel: 2 },
    Water: { Fire: 2, Water: 0.5, Grass: 0.5, Ground: 2, Rock: 2, Dragon: 0.5 },
    Electric: { Water: 2, Electric: 0.5, Grass: 0.5, Ground: 0, Flying: 2, Dragon: 0.5 },
    Grass: { Fire: 0.5, Water: 2, Grass: 0.5, Poison: 0.5, Ground: 2, Flying: 0.5, Bug: 0.5, Rock: 2, Dragon: 0.5, Steel: 0.5 },
    Ice: { Fire: 0.5, Water: 0.5, Grass: 2, Ice: 0.5, Ground: 2, Flying: 2, Dragon: 2, Steel: 0.5 },
    Fighting: { Normal: 2, Ice: 2, Poison: 0.5, Flying: 0.5, Psychic: 0.5, Bug: 0.5, Rock: 2, Ghost: 0, Dark: 2, Steel: 2, Fairy: 0.5 },
    Poison: { Grass: 2, Poison: 0.5, Ground: 0.5, Rock: 0.5, Ghost: 0.5, Steel: 0, Fairy: 2 },
    Ground: { Fire: 2, Electric: 2, Grass: 0.5, Poison: 2, Flying: 0, Bug: 0.5, Rock: 2, Steel: 2 },
    Flying: { Electric: 0.5, Grass: 2, Fighting: 2, Bug: 2, Rock: 0.5, Steel: 0.5 },
    Psychic: { Fighting: 2, Poison: 2, Psychic: 0.5, Dark: 0, Steel: 0.5 },
    Bug: { Fire: 0.5, Grass: 2, Fighting: 0.5, Poison: 0.5, Flying: 0.5, Psychic: 2, Ghost: 0.5, Dark: 2, Steel: 0.5, Fairy: 0.5 },
    Rock: { Fire: 2, Ice: 2, Fighting: 0.5, Ground: 0.5, Flying: 2, Bug: 2, Steel: 0.5 },
    Ghost: { Normal: 0, Psychic: 2, Ghost: 2, Dark: 0.5 },
    Dragon: { Dragon: 2, Steel: 0.5, Fairy: 0 },
    Dark: { Fighting: 0.5, Psychic: 2, Ghost: 2, Dark: 0.5, Fairy: 0.5 },
    Steel: { Fire: 0.5, Water: 0.5, Electric: 0.5, Ice: 2, Rock: 2, Steel: 0.5, Fairy: 2 },
    Fairy: { Fire: 0.5, Fighting: 2, Poison: 0.5, Dragon: 2, Dark: 2, Steel: 0.5 },
  };

  const PIVOTS = new Set(["uturn", "voltswitch", "flipturn", "partingshot", "teleport", "batonpass", "shedtail"]);
  const HAZARDS = new Set(["stealthrock", "spikes", "toxicspikes", "stickyweb", "ceaselessedge", "stoneaxe"]);
  const HAZARD_CLEAR = new Set(["rapidspin", "defog", "mortalspin", "tidyup", "courtchange"]);
  const SETUP = new Set([
    "swordsdance", "nastyplot", "calmmind", "dragondance", "quiverdance", "shellsmash",
    "bulkup", "coil", "irondefense", "agility", "rockpolish", "tailglow", "workup",
    "growth", "shiftgear", "victorydance", "tidyup", "clangoroussoul",
  ]);
  const RECOVERY = new Set([
    "recover", "softboiled", "roost", "slackoff", "milkdrink", "moonlight", "morningsun",
    "synthesis", "shoreup", "rest", "wish", "healingwish", "strengthsap", "lunarblessing",
    "painsplit", "leechseed", "junglehealing", "lifedew",
  ]);
  const SPEED_CONTROL = new Set(["thunderwave", "nuzzle", "glare", "stunspore", "icywind", "electroweb", "bulldoze", "rocktomb", "lowsweep"]);

  function toID(text) {
    return String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
  }

  function titleType(type) {
    const raw = String(type || "");
    if (!raw) return "";
    return raw.charAt(0).toUpperCase() + raw.slice(1).toLowerCase();
  }

  function dexMove(id) {
    const key = toID(id);
    try {
      if (window.Dex && Dex.moves) return Dex.moves.get(key);
    } catch (err) {
      // ignore
    }
    if (window.BattleMovedex && BattleMovedex[key]) return BattleMovedex[key];
    return { id: key, name: id, type: "Normal", category: "Physical", basePower: 0, accuracy: 100 };
  }

  function dexSpecies(name) {
    const key = toID(name);
    try {
      if (window.Dex && Dex.species) return Dex.species.get(key);
    } catch (err) {
      // ignore
    }
    if (window.BattlePokedex && BattlePokedex[key]) return BattlePokedex[key];
    return null;
  }

  function moveTypeOf(data, moveInfo) {
    const raw = (moveInfo && (moveInfo.type || moveInfo.Type)) || (data && (data.type || data.Type)) || "Normal";
    if (raw && typeof raw === "object") return titleType(raw.name || raw.type || "Normal");
    return titleType(raw);
  }

  function moveCategoryOf(data, moveInfo) {
    const raw = (moveInfo && moveInfo.category) || (data && data.category) || "Status";
    return String(raw);
  }

  function chartTaken(defType, atkType) {
    const table = window.BattleTypeChart;
    if (!table) return null;
    const def = table[defType] || table[defType.toLowerCase()] || table[toID(defType)];
    if (!def || !def.damageTaken) return null;
    const taken = def.damageTaken[atkType];
    if (taken == null) return def.damageTaken[atkType.toLowerCase()];
    return taken;
  }

  function dexTypeMul(atkType, defTypes) {
    const atk = titleType(atkType);
    const defs = (defTypes || []).map(titleType).filter(Boolean);
    if (!atk || !defs.length) return 1;
    if (window.Dex && typeof Dex.getImmunity === "function" && typeof Dex.getEffectiveness === "function") {
      try {
        const target = { types: defs };
        if (!Dex.getImmunity(atk, target)) return 0;
        return Math.pow(2, Dex.getEffectiveness(atk, target));
      } catch (err) {
        // fall through
      }
    }
    let mul = 1;
    for (const def of defs) {
      const taken = chartTaken(def, atk);
      if (taken === 1) mul *= 2;
      else if (taken === 2) mul *= 0.5;
      else if (taken === 3) mul *= 0;
      else if (taken == null) {
        const chart = TYPE_CHART[atk];
        if (chart && chart[def] != null) mul *= chart[def];
      }
    }
    return mul;
  }

  function abilityId(mon) {
    const raw = (mon && (mon.ability || (mon.poke && (mon.poke.ability || mon.poke.baseAbility)))) || "";
    return toID(raw);
  }

  function abilityBlocks(mon, moveType, data) {
    const abil = abilityId(mon);
    const t = titleType(moveType);
    if ((abil === "levitate" || abil === "eartheater") && t === "Ground") return true;
    if ((abil === "lightningrod" || abil === "voltabsorb" || abil === "motordrive") && t === "Electric") return true;
    if ((abil === "flashfire" || abil === "wellbakedbody") && t === "Fire") return true;
    if ((abil === "waterabsorb" || abil === "dryskin" || abil === "stormdrain") && t === "Water") return true;
    if (abil === "sapsipper" && t === "Grass") return true;
    if (abil === "wonderguard" && dexTypeMul(t, typesOf(mon.poke, mon.species)) <= 1) return true;
    if (abil === "soundproof" && data && data.flags && data.flags.sound) return true;
    if (abil === "bulletproof" && data && data.flags && data.flags.bullet) return true;
    const item = toID(mon.item || (mon.poke && mon.poke.item));
    if (item === "airballoon" && t === "Ground") return true;
    const types = typesOf(mon.poke, mon.species);
    if (t === "Ground" && types.includes("Flying")) return true;
    return false;
  }

  function parseHp(condition) {
    const text = String(condition || "");
    if (text === "0 fnt" || text.startsWith("0 ")) return { hp: 0, max: 1, pct: 0, status: "fnt" };
    const match = text.match(/(\d+)\s*\/\s*(\d+)/);
    const status = (text.match(/\b(par|brn|psn|tox|slp|frz)\b/) || [])[1] || "";
    if (!match) {
      const pct = text.includes("%") ? parseFloat(text) / 100 : 1;
      return { hp: Math.round(100 * pct), max: 100, pct: pct || 0, status };
    }
    const hp = Number(match[1]);
    const max = Number(match[2]) || 1;
    return { hp, max, pct: hp / max, status };
  }

  function boostMul(stage) {
    const n = Number(stage) || 0;
    if (n >= 0) return (2 + n) / 2;
    return 2 / (2 - n);
  }

  function typesOf(poke, species) {
    if (poke && poke.terastallized && poke.teraType) return [titleType(poke.teraType)];
    if (poke && Array.isArray(poke.types) && poke.types.length) return poke.types.map(titleType);
    if (species && Array.isArray(species.types)) return species.types.map(titleType);
    return ["Normal"];
  }

  function findRooms() {
    const out = [];
    const seen = new Set();
    const push = (room) => {
      if (!room || seen.has(room)) return;
      seen.add(room);
      out.push(room);
    };
    if (window.app) {
      push(app.curRoom);
      if (app.rooms) Object.keys(app.rooms).forEach((id) => push(app.rooms[id]));
    }
    if (window.PS && PS.rooms) {
      push(PS.room);
      Object.keys(PS.rooms).forEach((id) => push(PS.rooms[id]));
    }
    return out.filter((room) => room && (room.battle || room.request || (room.id && String(room.id).startsWith("battle-"))));
  }

  function roomRequest(room) {
    let req = room.request || (room.battle && room.battle.request) || null;
    if (typeof req === "string") {
      try {
        req = JSON.parse(req);
      } catch (err) {
        req = null;
      }
    }
    return req;
  }

  function realClick(el) {
    if (!el || el.disabled) return false;
    try {
      const opts = { bubbles: true, cancelable: true, view: window, pointerId: 1, pointerType: "mouse", isPrimary: true, buttons: 1 };
      el.dispatchEvent(new PointerEvent("pointerdown", opts));
      el.dispatchEvent(new PointerEvent("pointerup", opts));
      el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
      el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
      el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    } catch (err) {
      // ignore
    }
    if (typeof el.click === "function") el.click();
    return true;
  }

  function normalizeChoice(choice) {
    let body = String(choice || "").replace(/^\/choose\s+/i, "").trim();
    body = body.replace(/\btera\b/gi, "terastallize");
    body = body.replace(/\bterastal\b/gi, "terastallize");
    return body;
  }

  function clickChoice(choice) {
    const body = normalizeChoice(choice);
    const parts = body.split(/\s+/);
    const kind = parts[0];
    const index = parts[1];
    const extra = parts.slice(2).join(" ").toLowerCase();
    if (/terastallize/.test(extra)) {
      document.querySelectorAll('input[name="tera"], input[name="terastallize"], input[name="megaevo"], label.megaevo input[type="checkbox"]').forEach((box) => {
        if (box && !box.checked) realClick(box);
      });
    }
    if (/mega/.test(extra)) {
      document.querySelectorAll('input[name="mega"], input[name="megaevo"], input[name="megax"], input[name="megay"]').forEach((box) => {
        if (box && !box.checked) realClick(box);
      });
    }
    if (/max/.test(extra)) {
      document.querySelectorAll('input[name="max"], input[name="dynamax"]').forEach((box) => {
        if (box && !box.checked) realClick(box);
      });
    }
    const cmd = `/choose ${body}`;
    const cmdEls = document.querySelectorAll(`[data-cmd="${cmd}"], [data-cmd="/choose ${body}"]`);
    for (const el of cmdEls) {
      if (realClick(el)) return true;
    }
    if (kind === "move" || kind === "switch") {
      const name = kind === "move" ? "chooseMove" : "chooseSwitch";
      const menu = kind === "move" ? ".movemenu" : ".switchmenu";
      const selectors = [
        `${menu} button[value="${index}"]`,
        `button[name="${name}"][value="${index}"]`,
        `${menu} button:nth-of-type(${index})`,
        `.movebuttons button:nth-child(${index})`,
      ];
      for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && realClick(el)) return true;
      }
    }
    return false;
  }

  function sendChoice(room, choice, rqid) {
    const body = normalizeChoice(choice);
    const cmd = rqid != null && rqid !== "" ? `/choose ${body}|${rqid}` : `/choose ${body}`;
    const roomid = room.id || (room.battle && (room.battle.roomid || room.battle.id)) || "";
    clickChoice(body);
    const attempts = [];
    if (typeof room.sendDirect === "function") attempts.push(() => room.sendDirect(cmd));
    if (typeof room.send === "function") attempts.push(() => room.send(cmd));
    if (window.app && typeof app.send === "function") {
      attempts.push(() => app.send(cmd, roomid));
    }
    if (window.PS && typeof PS.send === "function") {
      attempts.push(() => PS.send(cmd, roomid));
      if (roomid) attempts.push(() => PS.send(`${roomid}|${cmd}`));
    }
    if (window.PS && PS.connection && typeof PS.connection.send === "function" && roomid) {
      attempts.push(() => PS.connection.send(`${roomid}|${cmd}`));
    }
    if (window.app && app.socket && typeof app.socket.send === "function" && roomid) {
      attempts.push(() => app.socket.send(`${roomid}|${cmd}`));
    }
    let sent = false;
    for (const fn of attempts) {
      try {
        fn();
        sent = true;
      } catch (err) {
        // try the next sender
      }
    }
    return sent;
  }

  function fieldMods(battle) {
    const weather = toID((battle && (battle.weather || (battle.field && battle.field.weather))) || "");
    const terrain = toID((battle && battle.terrain) || "");
    return { weather, terrain };
  }

  function expectedDamage(attacker, defender, move, field) {
    const data = dexMove(move.id || move.move || move);
    const id = toID(data.id || move.id || move.move);
    const cat = moveCategoryOf(data, move);
    const moveType = moveTypeOf(data, move);
    const defTypes = typesOf(defender.poke, defender.species);
    let typeMul = dexTypeMul(moveType, defTypes);
    if (abilityBlocks(defender, moveType, data)) typeMul = 0;

    if (id === "seismictoss" || id === "nightshade") {
      const lv = Number((attacker.poke && attacker.poke.level) || attacker.level || 100);
      const defHp = Math.max(1, (defender.hp && defender.hp.hp) || 300);
      return { dmg: lv, pct: lv / defHp, ko: lv >= defHp, status: false, typeMul, data, moveType, cat };
    }
    if (id === "superfang" || id === "naturesmadness" || id === "ruination") {
      const defHp = Math.max(1, (defender.hp && defender.hp.hp) || 300);
      const dmg = Math.max(1, Math.floor(defHp / 2));
      return { dmg, pct: 0.5, ko: defender.hp && defender.hp.pct <= 0.5, status: false, typeMul, data, moveType, cat };
    }

    if (cat === "Status" || (!(data.basePower || move.bp) && !data.multihit && data.basePower !== undefined && data.basePower === 0)) {
      return { dmg: 0, pct: 0, ko: false, status: true, typeMul, data, moveType, cat };
    }

    let bp = Number(move.bp || data.basePower || 0);
    if (data.multihit) {
      const hits = Array.isArray(data.multihit) ? (data.multihit[0] + data.multihit[1]) / 2 : Number(data.multihit) || 2;
      bp *= hits;
    }
    if (bp <= 0) return { dmg: 0, pct: 0, ko: false, status: true, typeMul, data, moveType, cat };
    if (typeMul === 0) return { dmg: 0, pct: 0, ko: false, status: false, typeMul: 0, data, moveType, cat };

    const atkTypes = typesOf(attacker.poke, attacker.species);
    const level = Number((attacker.poke && attacker.poke.level) || attacker.level || 100);
    const atkBoosts = (attacker.poke && attacker.poke.boosts) || {};
    const defBoosts = (defender.poke && defender.poke.boosts) || {};
    const physical = cat !== "Special";
    let atkStat = physical
      ? (attacker.stats.atk || attacker.stats.Atk || 100) * boostMul(atkBoosts.atk)
      : (attacker.stats.spa || attacker.stats.SpA || 100) * boostMul(atkBoosts.spa);
    let defStat = physical
      ? (defender.stats.def || defender.stats.Def || 100) * boostMul(defBoosts.def)
      : (defender.stats.spd || defender.stats.spd || defender.stats.SpD || 100) * boostMul(defBoosts.spd);
    if (abilityId(defender) === "unaware") {
      atkStat = physical ? attacker.stats.atk || 100 : attacker.stats.spa || 100;
    }
    if (abilityId(attacker) === "unaware") {
      defStat = physical ? defender.stats.def || 100 : defender.stats.spd || 100;
    }

    let stab = atkTypes.includes(moveType) ? 1.5 : 1;
    if (attacker.poke && attacker.poke.terastallized && titleType(attacker.poke.teraType) === moveType) {
      stab = atkTypes.includes(moveType) || (attacker.species && (attacker.species.types || []).map(titleType).includes(moveType)) ? 2 : 1.5;
    }

    let weatherMul = 1;
    if (field.weather === "sunnyday" || field.weather === "desolateland") {
      if (moveType === "Fire") weatherMul = 1.5;
      if (moveType === "Water") weatherMul = 0.5;
    }
    if (field.weather === "raindance" || field.weather === "primordialsea") {
      if (moveType === "Water") weatherMul = 1.5;
      if (moveType === "Fire") weatherMul = 0.5;
    }

    const burn = physical && attacker.hp && attacker.hp.status === "brn" && abilityId(attacker) !== "guts" ? 0.5 : 1;
    const item = toID(attacker.item || (attacker.poke && attacker.poke.item));
    let itemMul = 1;
    if (item === "lifeorb") itemMul = 1.3;
    if (item === "choiceband" && physical) itemMul = 1.5;
    if (item === "choicespecs" && !physical) itemMul = 1.5;
    if (item === "expertbelt" && typeMul >= 2) itemMul = 1.2;

    const base = Math.floor(Math.floor((Math.floor((2 * level) / 5 + 2) * bp * (atkStat / Math.max(defStat, 1))) / 50) + 2);
    const dmg = Math.max(1, Math.floor(base * stab * typeMul * weatherMul * burn * itemMul * 0.925));
    const defHp = Math.max(1, (defender.hp && defender.hp.hp) || Math.round((defender.hp && defender.hp.pct ? defender.hp.pct : 1) * (defender.stats.hp || 300)));
    const pct = dmg / defHp;
    return { dmg, pct, ko: dmg >= defHp, status: false, typeMul, data, moveType, cat };
  }

  function speedOf(mon) {
    const spe = (mon.stats && (mon.stats.spe || mon.stats.Spe)) || 0;
    const boosts = (mon.poke && mon.poke.boosts) || {};
    let val = spe * boostMul(boosts.spe);
    if (mon.hp && mon.hp.status === "par") val *= 0.5;
    return val;
  }

  function revealedMoves(poke) {
    const out = [];
    if (!poke) return out;
    if (Array.isArray(poke.moves)) {
      poke.moves.forEach((m) => {
        const id = toID(typeof m === "string" ? m : m.move || m.id);
        if (id && id !== "hiddenpower") out.push(id);
      });
    }
    if (Array.isArray(poke.moveTrack)) {
      poke.moveTrack.forEach((entry) => {
        const id = toID(Array.isArray(entry) ? entry[0] : entry);
        if (id) out.push(id);
      });
    }
    return Array.from(new Set(out));
  }

  function inferStats(species, level, ivsMax) {
    const bs = (species && (species.baseStats || species.bstats)) || { hp: 80, atk: 80, def: 80, spa: 80, spd: 80, spe: 80 };
    const lv = level || 100;
    const stat = (base, hp) => {
      if (hp) return Math.floor(((2 * base + 31 + 21) * lv) / 100) + lv + 10;
      return Math.floor((((2 * base + 31 + 21) * lv) / 100 + 5) * 1);
    };
    return {
      hp: stat(bs.hp || 80, true),
      atk: stat(bs.atk || 80),
      def: stat(bs.def || 80),
      spa: stat(bs.spa || 80),
      spd: stat(bs.spd || 80),
      spe: stat(bs.spe || 80),
    };
  }

  function syntheticStab(foe, me, field) {
    const types = typesOf(foe.poke, foe.species);
    const physical = (foe.stats.atk || 0) >= (foe.stats.spa || 0);
    let best = 0;
    for (const t of types) {
      const hit = expectedDamage(
        foe,
        me,
        { id: "tackle", type: t, category: physical ? "Physical" : "Special", bp: 90 },
        field
      );
      if (hit.pct > best) best = hit.pct;
    }
    return best;
  }

  function maxStabMul(attacker, defender) {
    let best = 0;
    for (const t of typesOf(attacker.poke, attacker.species)) {
      best = Math.max(best, dexTypeMul(t, typesOf(defender.poke, defender.species)));
    }
    return best || 1;
  }

  function bestDamageHit(me, foe, moves, field) {
    let best = null;
    for (const move of moves || []) {
      if (move.disabled || move.pp === 0) continue;
      const id = toID(move.id || move.move);
      const hit = expectedDamage(me, foe, { id }, field);
      if (hit.status) continue;
      if (!best || hit.pct > best.pct) best = Object.assign({ id }, hit);
    }
    return best;
  }

  function readMatchup(me, foe, field, bestHit) {
    const threat = foeThreatPct(me, foe, field);
    const faster = speedOf(me) >= speedOf(foe);
    const ourKo = !!(bestHit && bestHit.ko);
    const theyKo = threat >= 0.9;
    const foeStab = maxStabMul(foe, me);
    const ourMul = (bestHit && bestHit.typeMul) || 1;
    const weWall = threat < 0.5 && foeStab <= 1;
    const theyResist = ourMul <= 0.5 && !ourKo;
    const dying = theyKo && !(ourKo && faster);
    const badMatch = foeStab >= 2 && !(ourKo && faster);
    let plan = "attack";
    if (ourKo && (faster || !theyKo)) plan = "kill";
    else if (dying || (badMatch && !weWall)) plan = "switch";
    else if (weWall && me.hp.pct < 0.7) plan = "stall";
    else if (weWall && !(foe.hp && foe.hp.status)) plan = "status";
    else if (theyResist && !weWall) plan = "switch";
    else if (ourKo && theyKo && !faster) plan = "trade";
    return {
      threat,
      faster,
      ourKo,
      theyKo,
      foeStab,
      ourMul,
      weWall,
      theyResist,
      dying,
      badMatch,
      plan,
    };
  }

  function foeThreatPct(me, foe, field) {
    const moves = revealedMoves(foe.poke);
    let best = 0;
    if (moves.length) {
      for (const id of moves) {
        const hit = expectedDamage(foe, me, { id }, field);
        if (hit.pct > best) best = hit.pct;
      }
    } else {
      best = syntheticStab(foe, me, field);
    }
    return best;
  }

  function scoreMove(me, foe, moveInfo, field, options) {
    const id = toID(moveInfo.id || moveInfo.move);
    const data = dexMove(id);
    const disabled = !!(moveInfo.disabled || moveInfo.pp === 0);
    if (disabled) return { score: -1e9, why: "disabled", plan: (options && options.matchup && options.matchup.plan) || "attack" };
    const hit = expectedDamage(me, foe, { id }, field);
    const m = options.matchup || {};
    const plan = m.plan || "attack";
    const faster = m.faster != null ? m.faster : speedOf(me) >= speedOf(foe);
    const prio = Number(data.priority || 0);
    const typeLabel = hit.moveType || moveTypeOf(data, moveInfo);
    const mulLabel = hit.typeMul === 0 ? "0x immune" : `${hit.typeMul}x`;
    let score = 0;
    let why = `${data.name || id} (${typeLabel}, ${mulLabel})`;

    if (id === "focuspunch" || id === "solarbeam" || id === "meteorbeam" || id === "hyperbeam" || id === "gigaimpact") {
      score = plan === "kill" ? hit.pct * 10 : hit.pct * 12 - 50;
      why += " charge/recharge";
      return { score, why, hit, id, plan };
    }
    if (id === "fakeout") {
      const first = !!(me.poke && (me.poke.newlySwitched || me.poke.switchedInThisTurn));
      score = first && hit.typeMul > 0 ? 75 : -90;
      why += first ? " flinch" : " fails after turn 1";
      return { score, why, hit, id, plan };
    }

    if (hit.status) {
      const already = !!(foe.hp && foe.hp.status);
      if (RECOVERY.has(id) || id === "painsplit" || id === "leechseed") {
        if (plan === "kill") score = -20;
        else if (m.dying) score = 6;
        else if (plan === "stall" || (m.weWall && me.hp.pct < 0.8)) score = 80 + (1 - me.hp.pct) * 45;
        else if (me.hp.pct < 0.45 && m.threat < 0.85) score = 68;
        else if (me.hp.pct < 0.65 && m.threat < 0.6) score = 42;
        else score = 4;
        why = `${data.name} recover [${plan}]`;
      } else if (SETUP.has(id)) {
        score = plan === "stall" && me.hp.pct > 0.7 && faster ? 36 : 2;
        why = `${data.name} setup`;
      } else if (HAZARDS.has(id)) {
        score = plan === "kill" ? 4 : 22;
        why = `${data.name} hazard`;
      } else if (HAZARD_CLEAR.has(id)) {
        score = 18;
        why = `${data.name} hazard clear`;
      } else if (id === "thunderwave" || id === "glare" || id === "stunspore" || id === "nuzzle") {
        const immune = typesOf(foe.poke, foe.species).includes("Ground") && id === "thunderwave";
        const paraOk = !already && !immune && !faster && !m.dying;
        score = plan === "status" && paraOk ? 72 : paraOk ? 40 : 5;
        why = `${data.name} para [${plan}]`;
      } else if (id === "willowisp") {
        const physical = (foe.stats.atk || 0) >= (foe.stats.spa || 0);
        const ok = !already && physical && !m.dying;
        score = (plan === "status" || plan === "stall") && ok ? 70 : ok ? 36 : 6;
        why = `${data.name} burn [${plan}]`;
      } else if (id === "toxic" || id === "toxicthread") {
        const ok = !already && !m.dying && (plan === "stall" || plan === "status" || m.theyResist);
        score = ok ? 64 : 8;
        why = `${data.name} poison [${plan}]`;
      } else if (id === "protect" || id === "detect" || id === "spikyshield" || id === "banefulbunker" || id === "silktrap" || id === "burningbulwark") {
        score = m.dying && !options.hasSwitch ? 55 : plan === "stall" ? 28 : 3;
        why = `${data.name} protect [${plan}]`;
      } else if (id === "taunt") {
        score = plan === "stall" || plan === "status" ? 24 : 10;
      } else {
        score = plan === "status" ? 16 : 6;
        why = `${data.name} (${typeLabel} status) [${plan}]`;
      }
    } else {
      score = hit.pct * 42;
      if (hit.typeMul >= 4) score += 28;
      else if (hit.typeMul >= 2) score += 18;
      else if (hit.typeMul <= 0.5 && hit.typeMul > 0) score -= 28;
      else if (hit.typeMul === 0) score = -130;
      if (plan === "kill" && hit.ko) score += 150;
      else if (hit.ko && faster) score += 90;
      else if (hit.ko && !m.theyKo) score += 70;
      else if (hit.ko && m.theyKo && !faster) score += 12;
      if (plan === "switch" || m.dying) score -= 35;
      if (plan === "stall" && !hit.ko) score -= 18;
      if (PIVOTS.has(id)) {
        if (plan === "switch" || m.dying || m.badMatch) {
          score += 55 + hit.pct * 20;
          why += " pivot";
        } else score += 6;
      }
      if (prio > 0 && m.dying) score += 40 + prio * 8;
      if (id === "knockoff") score += 6;
      if (data.drain && (plan === "stall" || me.hp.pct < 0.55)) score += 16;
      if (data.recoil || data.hasCrashDamage) score -= 8;
      why = `${data.name} ${typeLabel} ${Math.round(hit.pct * 100)}% (${mulLabel}) [${plan}]`;
    }
    if (options.useTera) {
      const teraType = titleType(options.teraType || (me.poke && me.poke.teraType));
      if (teraType && teraType === hit.moveType && (hit.ko || hit.typeMul >= 2) && plan === "kill") score += 12;
      else score -= 12;
    }
    return { score, why, hit, id, plan };
  }

  function scoreSwitch(bench, me, foe, field, matchup) {
    if (!bench || bench.hp.pct <= 0 || bench.active) return { score: -1e9, why: "unusable" };
    const threatIn = foeThreatPct(bench, foe, field);
    const foeTypes = typesOf(foe.poke, foe.species);
    const benchTypes = typesOf(bench.poke, bench.species);
    let resist = 1;
    for (const t of foeTypes) resist *= dexTypeMul(t, benchTypes);
    let bestHit = 0;
    let bestMul = 1;
    for (const id of bench.moves || []) {
      const hit = expectedDamage(bench, foe, { id }, field);
      if (hit.pct > bestHit) {
        bestHit = hit.pct;
        bestMul = hit.typeMul;
      }
    }
    const weLive = threatIn < 0.88;
    const counter = weLive && bestMul >= 2;
    const check = weLive && resist <= 0.5;
    let score = -45;
    if (counter) score += 22;
    if (check) score += 18;
    if (resist === 0) score += 20;
    if (resist >= 2) score -= 70;
    if (bestHit >= 1 && weLive) score += 18;
    else score += bestHit * 10;
    score += (1 - Math.min(threatIn, 1.3)) * 12;

    const m = matchup || {};
    const needOut = m.plan === "switch" || m.dying || m.badMatch;
    if (m.plan === "kill") score -= 90;
    if (!needOut) {
      score -= 80;
    } else if (counter || check || resist === 0) {
      score += 145;
    } else if (weLive && resist <= 1) {
      score += 70;
    } else {
      score -= 25;
    }
    return {
      score,
      why: `switch ${bench.name} (${benchTypes.join("/")}) vs ${foeTypes.join("/")} live ${Math.round((1 - Math.min(threatIn, 1)) * 100)}% hit ${Math.round(bestHit * 100)}% (${bestMul}x) [${m.plan || "switch"}]`,
      threatIn,
      bestHit,
      resist,
      counter,
      check,
    };
  }

  function pickAction(room, req, battle) {
    const side = req.side || {};
    const mine = (side.pokemon || []).map((p, idx) => {
      const ident = String(p.ident || p.details || "");
      const nickname = ident.split(": ").slice(1).join(": ");
      const speciesName = (p.details || "").split(",")[0] || nickname;
      const species = dexSpecies(speciesName);
      const hp = parseHp(p.condition);
      const liveSide = battle && (battle[side.id] || battle.mySide);
      const liveList = (liveSide && liveSide.pokemon) || [];
      const live = liveList.find((b) => b && (b.ident === p.ident || b.speciesForme === speciesName)) ||
        (p.active && liveSide && liveSide.active && liveSide.active[0]) || p;
      const stats = p.stats ? { hp: hp.max, ...p.stats } : inferStats(species, live.level || 100);
      stats.hp = hp.max;
      return {
        index: idx + 1,
        name: speciesName,
        ident,
        species,
        poke: live,
        hp,
        stats,
        moves: (p.moves || []).map(toID),
        active: !!p.active,
        item: p.item,
        ability: p.ability,
        teraType: p.teraType,
        level: live.level || 100,
      };
    });
    const activeMine = mine.find((p) => p.active) || mine[0];
    const mySideObj = battle && (battle[side.id] || battle.mySide);
    const foeSideObj = (mySideObj && mySideObj.foe) || (battle && (side.id === "p1" ? battle.p2 : battle.p1)) || (battle && battle.yourSide);
    const foePoke = foeSideObj && foeSideObj.active && foeSideObj.active[0];
    const foeName = foePoke ? foePoke.speciesForme || foePoke.name || foePoke.ident : "";
    const foeSpecies = dexSpecies(foeName);
    const foeHp = foePoke ? { hp: foePoke.hp, max: foePoke.maxhp, pct: foePoke.maxhp ? foePoke.hp / foePoke.maxhp : 1, status: foePoke.status || "" } : { hp: 100, max: 100, pct: 1, status: "" };
    const foe = {
      name: foeName,
      species: foeSpecies,
      poke: foePoke,
      hp: foeHp,
      stats: inferStats(foeSpecies, (foePoke && foePoke.level) || 100),
    };
    if (foePoke && foePoke.maxhp) foe.stats.hp = foePoke.maxhp;
    const field = fieldMods(battle);
    const turn = Number((battle && battle.turn) || 0);
    const forceOut = { plan: "switch", dying: true, badMatch: true };

    if (req.teamPreview || req.requestType === "team") {
      const ranked = mine.slice().sort((a, b) => {
        const bulkA = (a.stats.hp || 0) + (a.stats.def || 0) + (a.stats.spd || 0);
        const bulkB = (b.stats.hp || 0) + (b.stats.def || 0) + (b.stats.spd || 0);
        return bulkB - bulkA;
      });
      const order = ranked.map((p) => p.index).join("");
      return { choice: `team ${order}`, reason: `Lead ${ranked[0] && ranked[0].name}` };
    }

    const force = req.forceSwitch || req.requestType === "switch";
    if (force && (force === true || (Array.isArray(force) && force.some(Boolean)))) {
      let best = null;
      for (const bench of mine) {
        if (bench.active || bench.hp.pct <= 0) continue;
        const scored = scoreSwitch(bench, activeMine || bench, foe, field, forceOut);
        if (!best || scored.score > best.scored.score) best = { bench, scored };
      }
      if (best) return { choice: `switch ${best.bench.index}`, reason: best.scored.why };
    }

    if (req.wait || req.requestType === "wait") {
      return { choice: "", reason: "waiting" };
    }

    const activeReq = (req.active && req.active[0]) || {};
    const trapped = !!(activeReq.trapped || activeReq.maybeTrapped);
    const canTera = !!(activeReq.canTerastallize || activeReq.canTerastallise);
    const canMega = !!(activeReq.canMegaEvo || activeReq.canMegaEvoX || activeReq.canMegaEvoY);
    const canMax = !!activeReq.canDynamax;
    const moves = activeReq.moves || [];
    const rawHit = bestDamageHit(activeMine, foe, moves, field);
    const matchup = readMatchup(activeMine, foe, field, rawHit);
    let hasSwitch = false;
    if (!trapped) {
      for (const bench of mine) {
        if (bench.active || bench.hp.pct <= 0) continue;
        const preview = scoreSwitch(bench, activeMine, foe, field, matchup);
        if (preview.counter || preview.check || preview.resist === 0) hasSwitch = true;
      }
    }

    const candidates = [];
    for (let i = 0; i < moves.length; i++) {
      const move = moves[i];
      const scored = scoreMove(activeMine, foe, move, field, {
        matchup,
        hasSwitch,
        useTera: false,
        turn,
      });
      scored.index = i + 1;
      scored.flags = "";
      candidates.push({
        kind: "move",
        score: scored.score,
        choice: `move ${scored.index}`,
        reason: scored.why,
        scored,
      });
      if (canTera && activeMine.teraType) {
        const teraMine = {
          ...activeMine,
          poke: Object.assign({}, activeMine.poke, {
            terastallized: true,
            teraType: activeMine.teraType,
            types: [activeMine.teraType],
          }),
        };
        const teraScored = scoreMove(teraMine, foe, move, field, {
          matchup,
          hasSwitch,
          useTera: true,
          teraType: activeMine.teraType,
          turn,
        });
        teraScored.index = i + 1;
        teraScored.flags = " terastallize";
        const teraHelps =
          (teraScored.hit && teraScored.hit.ko && !(scored.hit && scored.hit.ko)) ||
          (teraScored.hit && scored.hit && teraScored.hit.typeMul > scored.hit.typeMul);
        if (teraHelps) {
          candidates.push({
            kind: "move",
            score: teraScored.score,
            choice: `move ${teraScored.index} terastallize`,
            reason: teraScored.why,
            scored: teraScored,
          });
        }
      }
    }
    if (canMega) {
      candidates.forEach((c) => {
        if (c.kind === "move" && c.scored && c.scored.hit && !c.choice.includes("terastallize")) {
          c.choice += " mega";
          c.score += 8;
        }
      });
    }
    if (canMax) {
      candidates.forEach((c) => {
        if (c.kind === "move" && c.scored && c.scored.hit && c.scored.hit.pct > 0.45) {
          c.choice += " max";
          c.score += 6;
        }
      });
    }
    if (!trapped) {
      for (const bench of mine) {
        if (bench.active || bench.hp.pct <= 0) continue;
        const scored = scoreSwitch(bench, activeMine, foe, field, matchup);
        candidates.push({
          kind: "switch",
          score: scored.score,
          choice: `switch ${bench.index}`,
          reason: scored.why,
          scored,
        });
      }
    }

    candidates.sort((a, b) => b.score - a.score);
    const best = candidates[0];
    if (best && best.choice) {
      return { choice: best.choice, reason: `[${matchup.plan}] ${best.reason}` };
    }
    return { choice: "default", reason: "fallback" };
  }

  const rooms = findRooms();
  const room = rooms.find((r) => roomRequest(r) && r.battle) || rooms.find((r) => r.battle) || rooms[0];
  if (!room || !room.battle) {
    if (!window.__opusShowdownSearch) {
      window.__opusShowdownSearch = Date.now();
      try {
        if (window.app && typeof app.send === "function") app.send("/search gen9randombattle");
        else if (window.PS && typeof PS.send === "function") PS.send("/search gen9randombattle");
      } catch (err) {
        // ignore
      }
      return { ok: true, waiting: true, message: "Queued a Gen 9 random battle." };
    }
    return {
      ok: true,
      waiting: true,
      message: "Open a Pokémon Showdown battle, then I'll take over.",
    };
  }
  const req = roomRequest(room);
  if (!req) {
    return { ok: true, waiting: true, message: "Waiting for a turn.", battle: room.id || room.battle.id };
  }
  if (req.wait || req.requestType === "wait") {
    return { ok: true, waiting: true, message: "Waiting for opponent.", battle: room.id };
  }
  const rqid = req.rqid;
  const prev = window.__opusShowdown;
  if (prev && prev.rqid === rqid && prev.sent && Date.now() - (prev.at || 0) < 2500) {
    return { ok: true, waiting: true, message: "Sent this turn.", choice: prev.choice, battle: room.id };
  }

  if (!window.__opusShowdownTimer) {
    window.__opusShowdownTimer = true;
    try {
      if (typeof room.sendDirect === "function") room.sendDirect("/timer on");
      else if (typeof room.send === "function") room.send("/timer on");
    } catch (err) {
      // ignore
    }
  }

  const picked = pickAction(room, req, room.battle);
  if (!picked.choice) {
    return { ok: true, waiting: true, message: picked.reason || "Waiting.", battle: room.id };
  }

  const sent = sendChoice(room, picked.choice, rqid);
  window.__opusShowdown = { rqid, choice: picked.choice, reason: picked.reason, sent: true, at: Date.now() };
  return {
    ok: true,
    playing: true,
    choice: picked.choice,
    reason: picked.reason,
    battle: room.id || (room.battle && room.battle.roomid) || "",
    message: picked.reason,
  };
}

try {
  globalThis.pageShowdownPlay = pageShowdownPlay;
} catch (err) {
  // service worker and page both get a global
}
