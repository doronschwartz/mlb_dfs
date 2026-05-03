const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const today = new Date();
$("#date").value = today.toISOString().slice(0, 10);

const state = {
  tab: "slate",
  currentDraftId: null,
  selectedGamePks: new Set(),
  slateGames: [],
  identity: localStorage.getItem("mlb_dfs_identity") || "",
  lastPicksCount: -1,
};

function setIdentity(name) {
  state.identity = name || "";
  if (name) localStorage.setItem("mlb_dfs_identity", name);
  else localStorage.removeItem("mlb_dfs_identity");
}

function isMyTurn(onClock) {
  return !!(state.identity && onClock && onClock[0] === state.identity);
}

// True when the current user is the only drafter with open SP slots — they
// can pick SPs out of snake order. The draft state response sets
// sp_jump_drafter to that drafter's name (or null).
function canJumpForSP() {
  return !!(state.identity && state._spJumpDrafter && state._spJumpDrafter === state.identity);
}

// Fixed roster shape (matches mlb_dfs.draft.SLOTS).
const SLOT_TEMPLATE = [
  { key: "IF", label: "IF" },
  { key: "IF", label: "IF" },
  { key: "IF", label: "IF" },
  { key: "OF", label: "OF" },
  { key: "OF", label: "OF" },
  { key: "OF", label: "OF" },
  { key: "UTIL", label: "UTIL" },
  { key: "BN", label: "BN" },
  { key: "SP", label: "SP" },
  { key: "SP", label: "SP" },
];

function renderBreakdownTooltip(p) {
  if (!p.played) {
    return `<div class="breakdown-tooltip"><div class="bk-empty">No game data yet (${p.game_state || "Pre-Game"})</div></div>`;
  }
  if (!p.breakdown || !p.breakdown.length) {
    return `<div class="breakdown-tooltip"><div class="bk-empty">0.0 — no scoring events</div></div>`;
  }
  const fmt = (n) => (n > 0 ? "+" : "") + n.toFixed(1);
  const lines = p.breakdown
    .map(
      (b) =>
        `<div class="bk-line">
          <span class="bk-label">${b.label}</span>
          <span class="bk-count">${b.count}</span>
          <span class="bk-x">×</span>
          <span class="bk-each">${fmt(b.points_each)}</span>
          <span class="bk-eq">=</span>
          <span class="bk-total ${b.total < 0 ? "neg" : "pos"}">${fmt(b.total)}</span>
        </div>`,
    )
    .join("");
  const total = p.actual !== null ? p.actual : 0;
  return `<div class="breakdown-tooltip">
    <div class="bk-title">${p.name} — score breakdown</div>
    <div class="bk-rows">${lines}</div>
    <div class="bk-grand"><span>Total</span><span>${fmt(total)}</span></div>
  </div>`;
}

function lineupBadge(status) {
  if (!status || status === "pending") return `<span class="lineup-tag pending">TBD</span>`;
  if (status === "in") return `<span class="lineup-tag in">in</span>`;
  if (status === "out") return `<span class="lineup-tag out">OUT</span>`;
  return "";
}

function buildRosterGrid(picks) {
  const byType = { IF: [], OF: [], UTIL: [], BN: [], SP: [] };
  picks.forEach((p) => byType[p.slot]?.push(p));
  const cursor = { IF: 0, OF: 0, UTIL: 0, BN: 0, SP: 0 };
  return SLOT_TEMPLATE.map(({ key, label }) => {
    const pick = byType[key]?.[cursor[key]++] ?? null;
    return { label, pick };
  });
}

// ---------- tab switching ----------
$$("nav button").forEach((b) => {
  b.addEventListener("click", () => {
    $$("nav button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $(`#tab-${b.dataset.tab}`).classList.add("active");
    state.tab = b.dataset.tab;
    // When entering the Draft tab, reset to today's date + auto-load today's
    // draft if one exists on the volume.
    if (b.dataset.tab === "draft") {
      const today = new Date().toISOString().slice(0, 10);
      if ($("#date").value !== today) {
        $("#date").value = today;
        state._slateDate = null;
        state.slateGames = [];
      }
      const sel = $("#draft-id");
      if (sel && Array.from(sel.options).some((o) => o.value === today)) {
        sel.value = today;
        state.currentDraftId = today;
        syncToLoadedDraft().catch(() => {});
      } else {
        state.currentDraftId = null;
      }
    }
    refresh();
  });
});

$("#refresh").addEventListener("click", refresh);

$("#lineup-go")?.addEventListener("click", async () => {
  const names = ($("#lineup-names").value || "").split("\n").map(s => s.trim()).filter(Boolean);
  if (!names.length) return alert("Paste at least one player name.");
  localStorage.setItem("mlb_dfs_lineup_names", $("#lineup-names").value);
  $("#lineup-out").innerHTML = `<div class="muted">Projecting ${names.length} players…</div>`;
  const data = await api(`/api/lineup`, {
    method: "POST",
    body: JSON.stringify({ date: $("#date").value, names }),
  });
  const renderTable = (title, rows, emptyMsg) => {
    if (!rows.length) return `<h3>${title}</h3><div class="muted">${emptyMsg}</div>`;
    const trs = rows.map(r => {
      const recCls = r.recommendation === "START" ? "edge-pos" : (r.recommendation === "SIT" ? "edge-neg" : "muted");
      const c = r.components || {};
      const stat = r.role === "hitter" && (c.barrel_pct != null || c.hardhit_pct != null)
        ? `brl ${(c.barrel_pct ?? 0).toFixed(1)}% · hh ${(c.hardhit_pct ?? 0).toFixed(0)}%`
        : (r.role === "pitcher" && c.xera != null ? `xERA ${c.xera.toFixed(2)}` : "—");
      return `<tr><td class="${recCls}"><b>${r.recommendation}</b></td><td>${r.input}${r.matched_name && r.matched_name !== r.input ? ` <span class="muted">(${r.matched_name})</span>` : ""}</td><td>${r.position ?? "—"}</td><td>${r.projection.toFixed(2)}</td><td class="muted" style="font-size:11px;">${stat}</td></tr>`;
    }).join("");
    return `<h3>${title}</h3><table><thead><tr><th>Rec</th><th>Player</th><th>Pos</th><th>Proj</th><th>Statcast</th></tr></thead><tbody>${trs}</tbody></table>`;
  };
  let html = renderTable("Hitters", data.hitters, "No hitters matched.");
  html += renderTable("Pitchers", data.pitchers, "No pitchers matched.");
  if (data.unmatched.length) {
    html += `<h3>Not playing today / unmatched</h3><div class="muted">${data.unmatched.map(r => r.input).join(", ")}</div>`;
  }
  $("#lineup-out").innerHTML = html;
});

// Restore last roster + Fantrax IDs
window.addEventListener("DOMContentLoaded", () => {
  const saved = localStorage.getItem("mlb_dfs_lineup_names");
  if (saved && $("#lineup-names")) $("#lineup-names").value = saved;
  const lg = localStorage.getItem("mlb_dfs_ftx_league");
  const tm = localStorage.getItem("mlb_dfs_ftx_team");
  if (lg && $("#ftx-league")) $("#ftx-league").value = lg;
  if (tm && $("#ftx-team")) $("#ftx-team").value = tm;
});

$("#ftx-pull")?.addEventListener("click", async () => {
  const lg = $("#ftx-league").value.trim();
  const tm = $("#ftx-team").value.trim();
  if (!lg) return alert("Enter your Fantrax league_id (visible in any league URL).");
  localStorage.setItem("mlb_dfs_ftx_league", lg);
  if (tm) localStorage.setItem("mlb_dfs_ftx_team", tm);
  $("#ftx-status").textContent = "Pulling roster…";
  try {
    const url = `/api/fantrax/roster?league_id=${encodeURIComponent(lg)}` + (tm ? `&team_id=${encodeURIComponent(tm)}` : "");
    const data = await api(url);
    if (data.error) {
      const teamList = (data.teams || []).map(t => `${t.team_id}: ${t.name}`).join("\n");
      $("#ftx-status").textContent = "Multiple teams — paste a team_id:";
      alert(data.error + "\n\nTeams in this league:\n" + teamList);
      return;
    }
    const names = (data.players || []).map(p => p.name).filter(Boolean);
    $("#lineup-names").value = names.join("\n");
    localStorage.setItem("mlb_dfs_lineup_names", $("#lineup-names").value);
    $("#ftx-status").textContent = `✓ Pulled ${names.length} from ${data.team_name || "team"}. Click Project lineup.`;
  } catch (e) {
    $("#ftx-status").textContent = `Error: ${e.message}`;
  }
});
$("#date").addEventListener("change", refresh);

// ---------- API ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// ---------- slate ----------
let _slatePollHandle = null;

async function loadSlate() {
  const d = $("#date").value;
  const out = $("#slate-out");
  out.innerHTML = `<div class="muted">Loading slate for ${d}…</div>`;
  try {
    const data = await api(`/api/slate?date=${d}`);
    state.slateGames = data.games;
    out.innerHTML = data.games.length
      ? data.games.map(renderGame).join("")
      : `<div class="muted">No games scheduled.</div>`;
    renderGamePicker();
    // Auto-refresh every 30s if any game is live, while the user is on this tab.
    const anyLive = data.games.some((g) => g.live && !g.live.isFinal);
    if (_slatePollHandle) { clearInterval(_slatePollHandle); _slatePollHandle = null; }
    if (anyLive && state.tab === "slate") {
      _slatePollHandle = setInterval(() => {
        if (state.tab !== "slate") { clearInterval(_slatePollHandle); _slatePollHandle = null; return; }
        loadSlate();
      }, 30000);
    }
  } catch (e) {
    out.innerHTML = `<div class="muted">${e.message}</div>`;
  }
}

function renderGame(g) {
  const ap = g.away.probablePitcher?.name ?? "TBD";
  const hp = g.home.probablePitcher?.name ?? "TBD";
  const live = g.live;
  const isLive = live && !live.isFinal;
  const isFinal = live && live.isFinal;
  const awayScore = g.away.score ?? (live ? 0 : null);
  const homeScore = g.home.score ?? (live ? 0 : null);

  // Live status header — inning + half indicator, or FINAL
  let topBar = `<div class="status">${g.detailedStatus ?? ""}</div>`;
  if (isLive) {
    const half = live.inningHalf || "";
    const arrow = half === "Top" ? "▲" : half === "Bottom" ? "▼" : "•";
    topBar = `<div class="status live">${arrow} ${live.inningOrdinal ?? ""} ${half}</div>`;
  } else if (isFinal) {
    topBar = `<div class="status final">FINAL</div>`;
  }

  // Score line
  let scoreLine = "";
  if (awayScore !== null && homeScore !== null && (isLive || isFinal)) {
    const aw = isLive && live.inningHalf === "Top" ? "active" : "";
    const hw = isLive && live.inningHalf === "Bottom" ? "active" : "";
    scoreLine = `
      <div class="live-score">
        <div class="line ${aw}"><span class="abbr">${g.away.abbr}</span><span class="runs">${awayScore}</span></div>
        <div class="line ${hw}"><span class="abbr">${g.home.abbr}</span><span class="runs">${homeScore}</span></div>
      </div>`;
  }

  // Diamond + count + current batter (live only)
  let liveBlock = "";
  if (isLive) {
    const r = live.runners || {};
    liveBlock = `
      <div class="live-block">
        <div class="diamond">
          <div class="base second ${r.second ? "on" : ""}"></div>
          <div class="base third ${r.third ? "on" : ""}"></div>
          <div class="base first ${r.first ? "on" : ""}"></div>
        </div>
        <div class="count-and-ab">
          <div class="count">
            <span><b>${live.balls ?? 0}</b>-<b>${live.strikes ?? 0}</b></span>
            <span class="muted">${live.outs ?? 0} out${(live.outs ?? 0) === 1 ? "" : "s"}</span>
          </div>
          ${live.batter ? `<div class="ab"><span class="muted">AB:</span> ${live.batter}</div>` : ""}
          ${live.pitcher ? `<div class="ab"><span class="muted">P:</span> ${live.pitcher}</div>` : ""}
          ${live.onDeck ? `<div class="ab muted">On deck: ${live.onDeck}</div>` : ""}
        </div>
      </div>`;
  }

  return `
    <div class="game ${isLive ? "is-live" : ""} ${isFinal ? "is-final" : ""}">
      ${topBar}
      <div class="matchup">${g.away.abbr ?? g.away.name} @ ${g.home.abbr ?? g.home.name}</div>
      ${scoreLine}
      ${liveBlock}
      <div class="sps">SP: ${ap} vs ${hp}</div>
      <div class="muted" style="font-size:11px;margin-top:4px;">${g.venue ?? ""}</div>
    </div>`;
}

function renderGamePicker() {
  const host = $("#game-picker");
  if (!host) return;
  if (!state.slateGames.length) {
    host.innerHTML = `<div class="muted">No games today — switch to a date with games.</div>`;
    $("#game-count").textContent = "";
    return;
  }
  host.innerHTML = state.slateGames
    .map((g) => {
      const sel = state.selectedGamePks.has(g.gamePk) ? "selected" : "";
      const ap = g.away.probablePitcher?.name ?? "TBD";
      const hp = g.home.probablePitcher?.name ?? "TBD";
      return `
        <div class="game-card ${sel}" data-pk="${g.gamePk}">
          <div class="status">${g.detailedStatus ?? ""}</div>
          <div class="matchup">${g.away.abbr ?? g.away.name} @ ${g.home.abbr ?? g.home.name}</div>
          <div class="sps">SP: ${ap} vs ${hp}</div>
        </div>`;
    })
    .join("");
  $$("#game-picker .game-card").forEach((el) => {
    el.addEventListener("click", async () => {
      const pk = Number(el.dataset.pk);
      if (state.selectedGamePks.has(pk)) state.selectedGamePks.delete(pk);
      else state.selectedGamePks.add(pk);
      renderGamePicker();
      // If a draft is loaded, persist the new selection to the server so
      // the pool / recommendations / scoring update live.
      if (state.currentDraftId) {
        try {
          await api(`/api/drafts/${state.currentDraftId}/games`, {
            method: "POST",
            body: JSON.stringify({ game_pks: Array.from(state.selectedGamePks) }),
          });
          poolCache = { draftId: null, pool: [] };
          await renderDraft();
        } catch (e) {
          alert(e.message);
        }
      }
    });
  });
  const n = state.selectedGamePks.size;
  let suffix = n ? `· ${n} selected` : `· ${state.slateGames.length} total`;
  if (state.currentDraftId) {
    suffix += ` · ✏️ live-editing slate for ${state.currentDraftId}`;
  }
  $("#game-count").textContent = suffix;
  $("#game-picker").classList.toggle("editing-draft", !!state.currentDraftId);
}

// ---------- projections ----------
let projCache = { date: null, data: [] };

async function loadProjections() {
  const d = $("#date").value;
  if (projCache.date !== d || projCache.data.length === 0) {
    $("#proj-out").innerHTML = `<div class="muted">Crunching projections for ${d}… (this fetches recent stats per player and can take 10-30s)</div>`;
    const data = await api(`/api/projections?date=${d}`);
    projCache = { date: d, data: data.projections };
  }
  renderProjectionsTable();
}

function formBadge(tag) {
  if (!tag) return "";
  const styles = {
    HOT:    "background:rgba(239,68,68,0.25);color:#fca5a5;",
    COLD:   "background:rgba(59,130,246,0.25);color:#93c5fd;",
    ELITE:  "background:rgba(52,211,153,0.25);color:var(--accent-2);",
    STEADY: "background:rgba(148,163,184,0.25);color:var(--text);",
  };
  const labels = { HOT: "🔥 HOT", COLD: "🧊 COLD", ELITE: "⭐ ELITE", STEADY: "📊 STEADY" };
  return `<span class="bench-tag" style="${styles[tag] || ""}">${labels[tag] || tag}</span>`;
}

function projTooltip(p) {
  const c = p.components || {};
  const tier = c.qoc_tier && c.qoc_tier !== "—" ? c.qoc_tier : null;
  const tierClass = tier === "ELITE" ? "pos" : tier === "POOR" ? "neg" : "";
  const factorRow = (label, val, signed = false) => {
    if (val == null) return "";
    const fmt = signed ? (val > 0 ? "+" : "") + Number(val).toFixed(2) : Number(val).toFixed(3);
    const cls = signed ? (val > 0 ? "pos" : val < 0 ? "neg" : "") : "";
    return `<div class="bk-row"><span class="bk-label">${label}</span><span class="bk-total ${cls}">${fmt}</span></div>`;
  };
  const rows = [];
  // Rolling windows row — L3 / L7 / L14 side by side, with cell coloring vs L14.
  const cellFor = (val, base) => {
    if (val == null) return `<span class="muted">—</span>`;
    if (base == null) return val.toFixed(1);
    const cls = val >= 1.20 * base ? "pos" : val <= 0.80 * base ? "neg" : "";
    return `<span class="bk-total ${cls}" style="font-weight:600;">${val.toFixed(1)}</span>`;
  };
  if (p.role === "hitter" && (c.pg_l3 != null || c.pg_l7 != null || c.pg_l14 != null)) {
    rows.push(`<div class="bk-row"><span class="bk-label">Form (pts/G)</span><span style="display:flex;gap:8px;">L3: ${cellFor(c.pg_l3, c.pg_l14)} · L7: ${cellFor(c.pg_l7, c.pg_l14)} · L14: ${cellFor(c.pg_l14, c.pg_l14)}</span></div>`);
  }
  if (p.role === "pitcher" && (c.ps_l7 != null || c.ps_l14 != null || c.ps_season != null)) {
    rows.push(`<div class="bk-row"><span class="bk-label">Form (pts/start)</span><span style="display:flex;gap:8px;">L7: ${cellFor(c.ps_l7, c.ps_l14)} · L14: ${cellFor(c.ps_l14, c.ps_l14)} · Szn: ${cellFor(c.ps_season, c.ps_l14)}</span></div>`);
  }
  if (p.role === "hitter") {
    if (c.base_pg != null) rows.push(`<div class="bk-row"><span class="bk-label">Base 14d pts/G</span><span class="bk-total">${c.base_pg.toFixed(2)}</span></div>`);
    if (c.sp_factor != null) rows.push(`<div class="bk-row"><span class="bk-label">Opp SP factor</span><span class="bk-total ${c.sp_factor>1?"pos":"neg"}">×${c.sp_factor.toFixed(2)}</span></div>`);
    if (c.qoc_factor != null) rows.push(`<div class="bk-row"><span class="bk-label">Statcast QoC</span><span class="bk-total ${c.qoc_factor>1?"pos":"neg"}">×${c.qoc_factor.toFixed(2)}</span></div>`);
    if (c.barrel_pct != null) rows.push(`<div class="bk-row"><span class="bk-label">Barrel %</span><span class="bk-total">${c.barrel_pct.toFixed(1)} <span class="muted">(lg 6.5)</span></span></div>`);
    if (c.hardhit_pct != null) rows.push(`<div class="bk-row"><span class="bk-label">Hard-hit %</span><span class="bk-total">${c.hardhit_pct.toFixed(0)} <span class="muted">(lg 38)</span></span></div>`);
  } else {
    if (c.base_per_start != null) rows.push(`<div class="bk-row"><span class="bk-label">Base 14d pts/start</span><span class="bk-total">${c.base_per_start.toFixed(2)}</span></div>`);
    if (c.opp_factor != null) rows.push(`<div class="bk-row"><span class="bk-label">Opp run-env</span><span class="bk-total ${c.opp_factor<1?"pos":"neg"}">×${c.opp_factor.toFixed(2)}</span></div>`);
    if (c.qoc_factor != null) rows.push(`<div class="bk-row"><span class="bk-label">Statcast QoC</span><span class="bk-total ${c.qoc_factor>1?"pos":"neg"}">×${c.qoc_factor.toFixed(2)}</span></div>`);
    if (c.xera != null) rows.push(`<div class="bk-row"><span class="bk-label">xERA</span><span class="bk-total">${c.xera.toFixed(2)}</span></div>`);
    if (c.xwoba_against != null) rows.push(`<div class="bk-row"><span class="bk-label">xwOBA agst</span><span class="bk-total">${c.xwoba_against.toFixed(3)}</span></div>`);
    if (c.barrel_pct_allowed != null) rows.push(`<div class="bk-row"><span class="bk-label">brl-allowed %</span><span class="bk-total">${c.barrel_pct_allowed.toFixed(1)} <span class="muted">(lg 6.5)</span></span></div>`);
  }
  const pitfalls = (c.pitfalls || []).map(s => `<div class="bk-row bk-pitfall">⚠ ${s}</div>`).join("");
  const tierBadge = tier ? `<span class="bench-tag" style="background:${tier==="ELITE"?"rgba(52,211,153,0.25)":tier==="POOR"?"rgba(239,68,68,0.25)":"var(--border)"};color:${tier==="ELITE"?"var(--accent-2)":tier==="POOR"?"var(--bad)":"var(--text)"};">${tier}</span>` : "";
  const formB = formBadge(c.form_tag);
  return `<div class="breakdown-tooltip">
    <div class="bk-title">${p.name} ${formB} ${tierBadge} <span class="muted" style="font-weight:400;font-size:11px;">— projection breakdown</span></div>
    <div class="bk-rows">${rows.join("")}</div>
    <div class="bk-grand"><span>Projection</span><span>${p.projected_points.toFixed(2)} pts</span></div>
    ${pitfalls ? `<div class="bk-rows" style="margin-top:6px;border-top:1px solid var(--border);padding-top:6px;">${pitfalls}</div>` : ""}
    ${(p.notes||[]).length ? `<div class="bk-rows muted" style="margin-top:4px;font-size:10px;">${p.notes.join(" · ")}</div>` : ""}
  </div>`;
}

function renderProjectionsTable() {
  const rows = projCache.data
    .slice(0, 60)
    .map(
      (p) => `
      <tr class="${p.role} score-row">
        <td>${p.projected_points.toFixed(2)}</td>
        <td class="player-cell">${p.name} ${formBadge((p.components||{}).form_tag)}${projTooltip(p)}</td>
        <td>${p.position ?? "-"}</td>
        <td>${p.role}</td>
        <td class="notes">${(p.notes || []).join(" · ")}</td>
      </tr>`,
    )
    .join("");
  $("#proj-out").innerHTML = `
    <table>
      <thead><tr><th>Pts</th><th>Player</th><th>Pos</th><th>Role</th><th>Notes</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ---------- draft ----------
async function loadDraftList() {
  const data = await api(`/api/drafts`);
  for (const sel of [$("#draft-id"), $("#score-draft-id")]) {
    sel.innerHTML = data.drafts
      .map((id) => `<option value="${id}">${id}</option>`)
      .join("");
  }
  // Re-select whatever's currently loaded so the dropdown stays in sync
  // with the rest of the page after refilling.
  if (state.currentDraftId) {
    for (const sel of [$("#draft-id"), $("#score-draft-id")]) {
      if (Array.from(sel.options).some((o) => o.value === state.currentDraftId)) {
        sel.value = state.currentDraftId;
      }
    }
  }
}

$("#new-draft").addEventListener("click", async () => {
  const drafters = $("#drafters").value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (drafters.length < 2) return alert("Need at least 2 drafters.");
  const data = await api(`/api/drafts`, {
    method: "POST",
    body: JSON.stringify({
      date: $("#date").value,
      drafters,
      game_pks: Array.from(state.selectedGamePks),
    }),
  });
  state.currentDraftId = data.draft_id;
  await loadDraftList();
  await renderDraft();
});

$("#games-clear").addEventListener("click", () => {
  state.selectedGamePks.clear();
  renderGamePicker();
});

$("#randomize").addEventListener("click", () => {
  const input = $("#drafters");
  const names = input.value.split(",").map((s) => s.trim()).filter(Boolean);
  if (names.length < 2) return alert("Type at least 2 drafter names first.");
  // Fisher-Yates shuffle
  for (let i = names.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [names[i], names[j]] = [names[j], names[i]];
  }
  input.value = names.join(", ");
});

$("#show-eligibility").addEventListener("click", () => {
  const html = `
    <div class="replace-modal" id="eligibility-modal">
      <div class="panel" style="max-width:560px;">
        <div class="close-row">
          <h3>Slot eligibility rules</h3>
          <button class="close-btn">Close</button>
        </div>
        <table>
          <thead><tr><th>Slot</th><th>Who can fill it</th></tr></thead>
          <tbody>
            <tr><td><b>IF</b></td><td>Position is one of: 1B, 2B, 3B, SS, C</td></tr>
            <tr><td><b>OF</b></td><td>Position is one of: LF, CF, RF</td></tr>
            <tr><td><b>UTIL</b></td><td>Any non-pitcher</td></tr>
            <tr><td><b>BN</b></td><td>Any non-pitcher (bench is hitter-only)</td></tr>
            <tr><td><b>SP</b></td><td>The probable starting pitcher for a slate game</td></tr>
          </tbody>
        </table>
        <p class="muted" style="font-size:12px; margin-top:10px;">
          Bench-swap rule: an IF on BN can promote into IF or UTIL; an OF on BN
          can promote into OF or UTIL. The bench can never replace pitching.
        </p>
        <p class="muted" style="font-size:12px;">
          DH-positioned players (e.g. Shohei Ohtani as a hitter) appear with
          eligibility for UTIL and BN only. If Ohtani is also that day's
          probable SP, he shows up a second time in the pool with an SP-only
          row — pick the role you want him in (one or the other, not both).
        </p>
      </div>
    </div>`;
  document.body.insertAdjacentHTML("beforeend", html);
  const overlay = document.getElementById("eligibility-modal");
  const close = () => overlay.remove();
  overlay.querySelector(".close-btn").addEventListener("click", close);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
});

async function syncToLoadedDraft() {
  if (!state.currentDraftId) return;
  let data;
  try {
    data = await api(`/api/drafts/${state.currentDraftId}`);
  } catch (e) {
    alert(e.message);
    return;
  }
  // Switch the date input + slate to the draft's date.
  if (data.date && $("#date").value !== data.date) {
    $("#date").value = data.date;
    state._slateDate = null;
    state.slateGames = [];
  }
  await ensureSlateLoaded();
  // Pre-check the draft's games in the picker.
  state.selectedGamePks = new Set(data.game_pks || []);
  renderGamePicker();
  await renderDraft();
}

// "Load" button removed in favor of dropdown auto-load; handler kept for
// any older HTML that still has the button.
$("#load-draft")?.addEventListener("click", async () => {
  state.currentDraftId = $("#draft-id").value;
  await syncToLoadedDraft();
});

$("#draft-id").addEventListener("change", async () => {
  // Auto-load on dropdown change (the user no longer has to also press Load).
  if ($("#draft-id").value) {
    state.currentDraftId = $("#draft-id").value;
    await syncToLoadedDraft();
  }
});

$("#undo").addEventListener("click", async () => {
  if (!state.currentDraftId) return;
  await api(`/api/drafts/${state.currentDraftId}/last_pick`, { method: "DELETE" });
  await renderDraft();
});

$("#reset").addEventListener("click", async () => {
  const id = state.currentDraftId || $("#draft-id").value;
  if (!id) return alert("No draft loaded.");
  if (!confirm(`Reset all picks in draft '${id}'? Drafters and games stay; picks are wiped.`)) return;
  state.currentDraftId = id;
  await api(`/api/drafts/${id}/reset`, { method: "POST" });
  await renderDraft();
});

$("#delete").addEventListener("click", async () => {
  const id = state.currentDraftId || $("#draft-id").value;
  if (!id) return alert("No draft loaded.");
  if (!confirm(`Delete draft '${id}' permanently? This removes the file from disk.`)) return;
  await api(`/api/drafts/${id}`, { method: "DELETE" });
  if (state.currentDraftId === id) state.currentDraftId = null;
  poolCache = { draftId: null, pool: [] };
  await loadDraftList();
  await renderDraft();
});

async function renderDraft() {
  if (!state.currentDraftId) {
    $("#draft-state").innerHTML = `<div class="muted">No draft loaded. Start a new one or pick from the dropdown.</div>`;
    $("#pick-log").innerHTML = "";
    $("#recs-out").innerHTML = "";
    $("#pool-out").innerHTML = "";
    $("#identity-bar").hidden = true;
    state.lastPicksCount = -1;
    return;
  }
  const data = await api(`/api/drafts/${state.currentDraftId}`);
  state.lastPicksCount = (data.picks || []).length;
  state._spJumpDrafter = data.sp_jump_drafter || null;
  state._myTurnAtLastRender = isMyTurn(data.on_the_clock);
  renderIdentityBar(data);
  const onClock = data.on_the_clock; // [drafter, suggested_slot] | null  — drafter picks any open slot
  const html = [];
  html.push(`<div class="muted">Draft <b>${data.draft_id}</b> — ${data.is_complete ? "complete" : `On the clock: <b>${onClock?.[0] ?? "-"}</b>`}</div>`);

  // Selected games badges
  if (data.selected_games && data.selected_games.length) {
    html.push(`<div class="selected-games">` +
      data.selected_games
        .map((g) => `<span class="badge">${g.away_abbr} @ ${g.home_abbr}</span>`)
        .join("") +
      `</div>`);
  } else {
    html.push(`<div class="muted" style="font-size:11px;margin-bottom:8px;">Pool: full slate</div>`);
  }

  // Slate-start (earliest game in selected slate, displayed in Eastern Time)
  let earliestIso = null;
  for (const g of (data.selected_games || [])) {
    if (g.gameDate && (!earliestIso || g.gameDate < earliestIso)) earliestIso = g.gameDate;
  }
  if (!earliestIso) {
    for (const g of state.slateGames || []) {
      if (!earliestIso || g.gameDate < earliestIso) earliestIso = g.gameDate;
    }
  }
  const startTime = earliestIso
    ? new Date(earliestIso).toLocaleTimeString("en-US", {
        hour: "numeric", minute: "2-digit", timeZone: "America/New_York"
      }) + " ET"
    : "—";
  // Snake-order: order of THIS round.
  const nDrafters = data.drafters.length;
  const round = Math.floor((data.picks || []).length / nDrafters);
  const order = round % 2 === 0 ? data.drafters : [...data.drafters].reverse();
  const onClockName = data.on_the_clock ? data.on_the_clock[0] : null;
  const orderHtml = order.map((d) => {
    const cls = d === onClockName ? "on-clock" : (data.rosters[d]?.length > round ? "done" : "");
    return `<div class="drafter-strip-row ${cls}">${d}</div>`;
  }).join("");
  html.push(`<div class="draft-strip">
    <div class="strip-time">⏰ ${startTime}</div>
    <div class="strip-round">Round ${data.is_complete ? nDrafters * 10 / nDrafters : round + 1}</div>
    ${orderHtml}
  </div>`);
  html.push(`<div class="rosters">`);
  for (const d of data.drafters) {
    const onC = onClock && onClock[0] === d;
    const picks = data.rosters[d] || [];
    const grid = buildRosterGrid(picks);
    const total = picks.reduce((acc, p) => acc + (p.projected ?? 0), 0);
    const cells = grid.map(({ label, pick }) => {
      if (!pick) {
        return `<div class="slot-cell empty">
          <div class="slot-label">${label}</div>
          <div class="slot-body">
            <div class="slot-line1">
              <span class="slot-name">— open —</span>
            </div>
          </div>
        </div>`;
      }
      const scratched = pick.lineup_status === "out";
      const cls = `filled ${pick.role} ${scratched ? "scratched" : ""}`;
      const canReplace = state.identity && state.identity === pick.drafter;
      const meta = [lineupBadge(pick.lineup_status)];
      if (canReplace) {
        meta.push(
          `<button class="move-btn" data-pick-num="${pick.pick_number}" data-name="${escapeAttr(pick.name)}" data-slot="${pick.slot}">Move</button>`,
          `<button class="replace-btn" data-pick-num="${pick.pick_number}" data-slot="${pick.slot}" data-name="${escapeAttr(pick.name)}">Replace</button>`,
        );
      }
      return `<div class="slot-cell ${cls}">
        <div class="slot-label">${label}</div>
        <div class="slot-body">
          <div class="slot-line1">
            <span class="slot-name">${pick.name}</span>
            <span class="slot-proj">${pick.projected.toFixed(1)}</span>
          </div>
          <div class="slot-line2">${meta.join("")}</div>
        </div>
      </div>`;
    });
    html.push(
      `<div class="roster ${onC ? "on-clock" : ""}">
        <h4>${d} ${onC ? `<span class="muted">← on the clock</span>` : ""}${onC && (data.picks || []).length === data.drafters.length * 10 - 1 ? ` <span class="tuh-badge">TUH 🏠</span>` : ""}
          <span class="muted" style="float:right;font-weight:400;">${picks.length}/10 · ${total.toFixed(1)} pts</span>
        </h4>
        <div class="slot-grid">${cells.join("")}</div>
      </div>`,
    );
  }
  html.push(`</div>`);
  $("#draft-state").innerHTML = html.join("");
  $$("#draft-state .replace-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      openReplaceModal({
        pickNumber: Number(btn.dataset.pickNum),
        slot: btn.dataset.slot,
        oldName: btn.dataset.name,
      });
    });
  });
  $$("#draft-state .move-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      openMoveMenu(btn, {
        pickNumber: Number(btn.dataset.pickNum),
        currentSlot: btn.dataset.slot,
        name: btn.dataset.name,
      });
    });
  });

  renderPickLog(data);

  if (!data.is_complete) {
    await renderRecs();
    await renderPool();
  } else {
    $("#recs-out").innerHTML = `<div class="muted">Draft complete.</div>`;
    $("#pool-out").innerHTML = "";
  }
}

function renderIdentityBar(data) {
  const bar = $("#identity-bar");
  bar.hidden = false;
  const sel = $("#me");
  const drafters = data.drafters || [];
  const onClock = data.on_the_clock;
  const current = state.identity;
  sel.innerHTML =
    `<option value="">— pick your name —</option>` +
    drafters.map((d) => `<option value="${d}" ${d === current ? "selected" : ""}>${d}</option>`).join("") +
    `<option value="__spectator__" ${current === "__spectator__" ? "selected" : ""}>spectator (read-only)</option>`;
  const myTurn = isMyTurn(onClock);
  bar.classList.toggle("your-turn", myTurn);
  if (data.is_complete) {
    $("#turn-status").textContent = "· draft complete";
  } else if (!current) {
    $("#turn-status").textContent = "· choose your name to enable picks";
  } else if (current === "__spectator__") {
    $("#turn-status").textContent = "· spectator — picks disabled";
  } else if (myTurn) {
    $("#turn-status").textContent = "· YOUR TURN — pick away ⚾";
  } else {
    $("#turn-status").textContent = `· waiting for ${onClock?.[0] ?? "—"}`;
  }
}

function escapeAttr(s) {
  return String(s).replace(/"/g, "&quot;");
}

function encodeAttrJSON(obj) {
  return JSON.stringify(obj || []).replace(/"/g, "&quot;");
}

function chooseGameModal(teamGames, playerName) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "replace-modal";
    overlay.innerHTML = `
      <div class="panel" style="max-width:420px;">
        <div class="close-row">
          <h3>Doubleheader — pick which game</h3>
          <button class="close-btn">Cancel</button>
        </div>
        <p class="muted" style="font-size:12px; margin:4px 0 12px;">
          ${playerName}'s team has more than one game in this slate. The pick
          counts in only the game you choose — the other game's stats are
          ignored for this player.
        </p>
        <div style="display:flex; flex-direction:column; gap:8px;">
          ${teamGames.map((g) => `<button class="btn-pick game-choice" data-pk="${g.game_pk}">${g.label}</button>`).join("")}
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const close = (result) => { overlay.remove(); resolve(result); };
    overlay.querySelector(".close-btn").addEventListener("click", () => close(null));
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(null); });
    overlay.querySelectorAll(".game-choice").forEach((b) =>
      b.addEventListener("click", () => close(Number(b.dataset.pk))),
    );
  });
}

async function resolveGamePk(button) {
  let teamGames = [];
  try { teamGames = JSON.parse((button.dataset.teamGames || "[]").replace(/&quot;/g, '"')); } catch {}
  if (!teamGames || teamGames.length <= 1) return undefined;
  const choice = await chooseGameModal(teamGames, button.dataset.name || "Player");
  return choice;
}

async function openMoveMenu(anchorEl, { pickNumber, currentSlot, name }) {
  // Close any existing move menu
  document.querySelectorAll(".move-menu").forEach((m) => m.remove());
  let targets = [];
  try {
    const data = await api(`/api/drafts/${state.currentDraftId}/picks/${pickNumber}/move_targets`);
    targets = data.targets || [];
  } catch (e) {
    return alert(e.message);
  }
  if (!targets.length) {
    return alert(`${name} can't be moved into any other slot (no eligible targets).`);
  }
  const rect = anchorEl.getBoundingClientRect();
  const menu = document.createElement("div");
  menu.className = "move-menu";
  menu.style.top = `${rect.bottom + window.scrollY + 4}px`;
  menu.style.left = `${rect.left + window.scrollX}px`;
  menu.innerHTML = `
    <div class="move-header">Move ${name} to…</div>
    ${targets
      .map(
        (s) =>
          `<button class="move-target" data-slot="${s}">→ ${s}</button>`,
      )
      .join("")}`;
  document.body.appendChild(menu);

  const close = () => menu.remove();
  setTimeout(() => {
    document.addEventListener(
      "click",
      function once(e) {
        if (!menu.contains(e.target)) {
          close();
          document.removeEventListener("click", once);
        }
      },
      { once: false },
    );
  }, 0);

  menu.querySelectorAll(".move-target").forEach((b) => {
    b.addEventListener("click", async () => {
      try {
        await api(`/api/drafts/${state.currentDraftId}/picks/${pickNumber}/move`, {
          method: "POST",
          body: JSON.stringify({ new_slot: b.dataset.slot }),
        });
        close();
        await renderDraft();
      } catch (e) {
        alert(e.message);
      }
    });
  });
}

async function openReplaceModal({ pickNumber, slot, oldName }) {
  const data = await api(`/api/drafts/${state.currentDraftId}/pool`).catch(() => null);
  if (!data) return alert("Couldn't load replacement candidates.");
  const allCandidates = data.pool
    .filter((p) => p.eligible_slots.includes(slot))
    .sort((a, b) => {
      const order = { in: 0, pending: 1, out: 2, undefined: 1 };
      const da = order[a.lineup_status] ?? 1;
      const db = order[b.lineup_status] ?? 1;
      if (da !== db) return da - db;
      return b.projected_points - a.projected_points;
    });

  const overlay = document.createElement("div");
  overlay.className = "replace-modal";
  overlay.innerHTML = `
    <div class="panel">
      <div class="close-row">
        <h3>Replace ${oldName} (${slot})</h3>
        <button class="close-btn">Close</button>
      </div>
      <p class="muted" style="font-size:12px;margin:4px 0 8px;">
        Sorted by lineup status (in lineup first), then projection.
      </p>
      <input id="replace-search" placeholder="Search by name…"
             style="width:100%;margin-bottom:10px;" autofocus />
      <div class="replace-results">
        <table>
          <thead><tr><th>Proj</th><th>Player</th><th>Pos</th><th>Lineup</th><th></th></tr></thead>
          <tbody class="results-body"></tbody>
        </table>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const close = () => overlay.remove();
  overlay.querySelector(".close-btn").addEventListener("click", close);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  document.addEventListener("keydown", function escClose(e) {
    if (e.key === "Escape") { close(); document.removeEventListener("keydown", escClose); }
  });

  const tbody = overlay.querySelector(".results-body");
  const search = overlay.querySelector("#replace-search");

  function paint() {
    const q = (search.value || "").toLowerCase().trim();
    const rows = q
      ? allCandidates.filter((p) => p.name.toLowerCase().includes(q))
      : allCandidates;
    tbody.innerHTML = rows
      .slice(0, 200)
      .map(
        (p) => `
        <tr class="${p.role}">
          <td>${p.projected_points.toFixed(2)}</td>
          <td>${p.name}</td>
          <td>${p.position ?? "-"}</td>
          <td>${lineupBadge(p.lineup_status)}</td>
          <td><button class="btn-pick swap-btn" data-pid="${p.player_id}" data-name="${escapeAttr(p.name)}" data-team-games="${encodeAttrJSON(p.team_games_in_slate)}">Swap in</button></td>
        </tr>`,
      )
      .join("");
    overlay.querySelectorAll(".swap-btn").forEach((b) => {
      b.addEventListener("click", async () => {
        const gamePk = await resolveGamePk(b);
        if (gamePk === null) return;
        try {
          await api(`/api/drafts/${state.currentDraftId}/picks/${pickNumber}/replace`, {
            method: "POST",
            body: JSON.stringify({
              player_id: Number(b.dataset.pid),
              game_pk: gamePk,
            }),
          });
          close();
          if (state.tab === "score") $("#score-load").click();
          else await renderDraft();
        } catch (e) {
          alert(e.message);
        }
      });
    });
  }

  search.addEventListener("input", paint);
  paint();
}

$("#me").addEventListener("change", (e) => {
  setIdentity(e.target.value);
  renderDraft();
});

function renderPickLog(data) {
  if (!data.picks.length) {
    $("#pick-log").innerHTML = "";
    return;
  }
  const nDrafters = data.drafters.length;
  const rounds = [];
  for (let i = 0; i < data.picks.length; i += nDrafters) {
    rounds.push(data.picks.slice(i, i + nDrafters));
  }
  const html = [`<div class="pick-log"><h3>Draft board</h3>`];
  rounds.forEach((round, idx) => {
    html.push(`<div class="round-label">Round ${idx + 1}</div>`);
    html.push(`<div class="round">`);
    round.forEach((p) => {
      html.push(
        `<div class="pick ${p.role}">
          <span class="num">#${p.pick_number}</span>
          <span class="who">${p.drafter}</span>
          <span class="name">${p.name}</span>
          <span class="slot">${p.slot}</span>
        </div>`,
      );
    });
    html.push(`</div>`);
  });
  html.push(`</div>`);
  $("#pick-log").innerHTML = html.join("");
}

async function renderRecs() {
  $("#recs-out").innerHTML = `<div class="muted">Loading recommendations…</div>`;
  try {
    const data = await api(`/api/drafts/${state.currentDraftId}/recommend?top_n=10`);
    const myTurn = isMyTurn(data.on_the_clock);
    const spJump = canJumpForSP();
    const lockFor = (slot) => (myTurn || (spJump && slot === "SP")) ? "" : "locked";
    const rows = data.recommendations
      .map(
        (r) => {
          const slots = r.eligible_slots && r.eligible_slots.length
            ? r.eligible_slots
            : [r.recommend_slot];
          const tgAttr = encodeAttrJSON(r.team_games_in_slate);
          const pills = slots
            .map((s) => {
              const recommended = s === r.recommend_slot ? "recommended" : "";
              return `<span class="slot-pill ${recommended} ${lockFor(s)}" data-pid="${r.player_id}" data-slot="${s}" data-name="${escapeAttr(r.name)}" data-team-games="${tgAttr}">${s}</span>`;
            })
            .join("");
          return `
          <tr class="${r.role} score-row">
            <td>${r.projected_points.toFixed(2)}</td>
            <td class="player-cell">${r.name} ${formBadge((r.components||{}).form_tag)}${projTooltip(r)}</td>
            <td>${r.position ?? "-"}</td>
            <td>${pills}</td>
          </tr>`;
        },
      )
      .join("");
    $("#recs-out").innerHTML = `
      <table>
        <thead><tr><th>Proj</th><th>Player</th><th>Pos</th><th>Pick into…</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="muted" style="margin-top:6px;font-size:11px;">Click any slot to draft into it. The starred slot is the recommender's default — but pick whatever you want.</div>`;
    $$("#recs-out .slot-pill").forEach((b) => {
      if (b.classList.contains("locked")) return;
      b.addEventListener("click", async () => {
        const gamePk = await resolveGamePk(b);
        if (gamePk === null) return;  // user cancelled DH chooser
        try {
          await api(`/api/drafts/${state.currentDraftId}/pick`, {
            method: "POST",
            body: JSON.stringify({
              draft_id: state.currentDraftId,
              player_id: Number(b.dataset.pid),
              slot: b.dataset.slot,
              game_pk: gamePk,
              drafter_override: (b.dataset.slot === "SP" && canJumpForSP()) ? state.identity : undefined,
            }),
          });
        } catch (e) {
          alert(e.message);
        }
        await renderDraft();
      });
    });
  } catch (e) {
    $("#recs-out").innerHTML = `<div class="muted">${e.message}</div>`;
  }
}

// ---------- all available players ----------

let poolCache = { draftId: null, pool: [] };

async function renderPool() {
  if (!state.currentDraftId) return;
  if (poolCache.draftId !== state.currentDraftId) poolCache = { draftId: null, pool: [] };
  $("#pool-out").innerHTML = `<div class="muted" style="padding:12px;">Loading available players…</div>`;
  try {
    const data = await api(`/api/drafts/${state.currentDraftId}/pool`);
    poolCache = { draftId: state.currentDraftId, pool: data.pool };
    drawPool();
  } catch (e) {
    $("#pool-out").innerHTML = `<div class="muted" style="padding:12px;">${e.message}</div>`;
  }
}

function drawPool() {
  const search = ($("#pool-search").value || "").toLowerCase().trim();
  const filter = $("#pool-filter").value;
  const myTurn = state._myTurnAtLastRender;
  const spOnly = canJumpForSP();
  // SP free-for-all: SP pills unlock; non-SP pills stay locked unless it's our turn.
  const lockFor = (slot) => (myTurn || (spOnly && slot === "SP")) ? "" : "locked";
  const rows = poolCache.pool.filter((p) => {
    if (search && !p.name.toLowerCase().includes(search)) return false;
    if (filter === "hitter") return p.role === "hitter";
    if (filter === "pitcher") return p.role === "pitcher";
    if (filter === "IF" || filter === "OF") return p.eligible_slots.includes(filter);
    return true;
  });
  $("#pool-count").textContent = `${rows.length} available`;
  if (!rows.length) {
    $("#pool-out").innerHTML = `<div class="muted" style="padding:12px;">No matches.</div>`;
    return;
  }
  const html = `
    <table>
      <thead>
        <tr><th>Proj</th><th>Player</th><th>Pos</th><th>Role</th><th>Statcast</th><th>Pick into…</th><th>Notes</th></tr>
      </thead>
      <tbody>${rows
        .map(
          (p) => {
            const c = p.components || {};
            let stat = "—";
            if (p.role === "hitter" && (c.barrel_pct != null || c.hardhit_pct != null)) {
              const brl = c.barrel_pct != null ? c.barrel_pct.toFixed(1) : "—";
              const hh = c.hardhit_pct != null ? c.hardhit_pct.toFixed(0) : "—";
              stat = `<span title="barrel ${brl}% · hard-hit ${hh}% · qoc x${(c.qoc_factor||1).toFixed(2)}">brl ${brl}% · hh ${hh}%</span>`;
            } else if (p.role === "pitcher" && (c.xera != null || c.xwoba_against != null)) {
              const xe = c.xera != null ? c.xera.toFixed(2) : "—";
              const xw = c.xwoba_against != null ? c.xwoba_against.toFixed(3) : "—";
              stat = `<span title="xERA ${xe} · xwOBA ${xw} · qoc x${(c.qoc_factor||1).toFixed(2)}">xERA ${xe}</span>`;
            }
            return `
        <tr class="${p.role} score-row">
          <td>${p.projected_points.toFixed(2)}</td>
          <td class="player-cell">${p.name} ${formBadge((p.components||{}).form_tag)}${projTooltip(p)}</td>
          <td>${p.position ?? "-"}</td>
          <td>${p.role}</td>
          <td class="muted" style="font-size:11px;">${stat}</td>
          <td>${
            p.eligible_slots.length
              ? (() => {
                  const tg = encodeAttrJSON(p.team_games_in_slate);
                  return p.eligible_slots
                    .map(
                      (s) =>
                        `<span class="slot-pill ${lockFor(s)}" data-pid="${p.player_id}" data-slot="${s}" data-name="${escapeAttr(p.name)}" data-team-games="${tg}">${s}</span>`,
                    )
                    .join("");
                })()
              : `<span class="slot-pill disabled">no slot left</span>`
          }</td>
          <td class="notes">${(p.notes || []).join(" · ")}</td>
        </tr>`;
          },
        )
        .join("")}</tbody>
    </table>`;
  $("#pool-out").innerHTML = html;
  $$("#pool-out .slot-pill").forEach((el) => {
    if (el.classList.contains("disabled") || el.classList.contains("locked")) return;
    el.addEventListener("click", async () => {
      const gamePk = await resolveGamePk(el);
      if (gamePk === null) return;
      try {
        await api(`/api/drafts/${state.currentDraftId}/pick`, {
          method: "POST",
          body: JSON.stringify({
            draft_id: state.currentDraftId,
            player_id: Number(el.dataset.pid),
            slot: el.dataset.slot,
            game_pk: gamePk,
            drafter_override: (el.dataset.slot === "SP" && canJumpForSP()) ? state.identity : undefined,
          }),
        });
      } catch (e) {
        alert(e.message);
      }
      await renderDraft();
    });
  });
}

$("#pool-search").addEventListener("input", () => poolCache.pool.length && drawPool());
$("#pool-filter").addEventListener("change", () => poolCache.pool.length && drawPool());

// ---------- live scoring ----------
$("#score-load").addEventListener("click", async () => {
  const id = $("#score-draft-id").value;
  if (!id) return;
  // Set this so the Replace modal (which lives on /pool) targets the right draft.
  state.currentDraftId = id;
  $("#score-out").innerHTML = `<div class="muted">Pulling box scores…</div>`;
  const data = await api(`/api/drafts/${id}/score`);
  const standings = data.standings;
  const top = `
    <div class="standings">
      <h3 style="margin-top:0">Standings</h3>
      ${standings
        .map(
          (s) => {
            const rankClass = s.rank === 1 ? "rank-1" : s.rank === 2 ? "rank-2" : s.rank === 3 ? "rank-3" : "";
            return `<div class="row ${rankClass}"><span>${s.rank}. <b>${s.drafter}</b></span><span class="total">${s.total.toFixed(2)} <span class="muted">(full ${s.full_total.toFixed(2)})</span></span></div>`;
          },
        )
        .join("")}
    </div>`;
  const cards = standings
    .map((s) => {
      // Sort picks by slot order (hitters first, SP at the bottom) instead
      // of draft order, so each team card reads like a starting lineup.
      const SLOT_DISPLAY_ORDER = { IF: 0, OF: 1, UTIL: 2, BN: 3, SP: 4 };
      const rows = [...s.picks]
        .sort((a, b) => {
          const da = SLOT_DISPLAY_ORDER[a.slot] ?? 99;
          const db = SLOT_DISPLAY_ORDER[b.slot] ?? 99;
          if (da !== db) return da - db;
          return (a.pick_number ?? 0) - (b.pick_number ?? 0);
        })
        .map(
          (p) => {
            // A row is "benched-out" when its score didn't count toward the Total.
            // That happens in two cases:
            //   1. starter is OOL and a bench player got promoted into their slot
            //   2. game is over and the bench outscored them
            // Pre-game picks who are in/pending lineup render normally.
            const showBenched = !p.counted && (p.played || p.lineup_status === "out");
            const cls = showBenched ? "benched-out" : "counted";
            const tag = showBenched ? `<span class="bench-tag">benched</span>` : "";
            const promoted = p.promoted ? `<span class="promoted-tag">PROMOTED</span>` : "";
            const lineupTag = p.lineup_status && p.lineup_status !== "pending"
              ? `<span class="lineup-tag ${p.lineup_status}">${p.lineup_status === "out" ? "OUT" : "in"}</span>`
              : "";
            const canReplace = state.identity && state.identity === p.drafter;
            const replaceCell = canReplace
              ? `<td><button class="replace-btn score-replace-btn"
                    data-pick-num="${p.pick_number}"
                    data-slot="${p.slot}"
                    data-name="${escapeAttr(p.name)}">Replace</button></td>`
              : `<td></td>`;
            const tooltip = renderBreakdownTooltip(p);
            return `
          <tr class="${cls} score-row">
            <td>${p.slot}</td>
            <td class="player-cell">${p.name} ${lineupTag} ${tag} ${promoted}${tooltip}</td>
            <td>${p.projected.toFixed(1)}</td>
            <td>${p.actual === null ? "-" : p.actual.toFixed(1)}</td>
            <td class="muted">${p.game_state ?? ""}</td>
            ${replaceCell}
          </tr>`;
          },
        )
        .join("");
      return `
        <div class="standings">
          <h4 style="margin:0 0 6px;">${s.drafter} — ${s.total.toFixed(2)}</h4>
          <table>
            <thead><tr><th>Slot</th><th>Player</th><th>Proj</th><th>Actual</th><th>State</th><th></th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>`;
    })
    .join("");
  $("#score-out").innerHTML = top + `<div class="score-grid">${cards}</div>`;
  $$("#score-out .score-replace-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      openReplaceModal({
        pickNumber: Number(btn.dataset.pickNum),
        slot: btn.dataset.slot,
        oldName: btn.dataset.name,
      });
    });
  });
});

// ---------- bootstrap + polling ----------
async function ensureSlateLoaded() {
  const d = $("#date").value;
  if (state.slateGames.length && state._slateDate === d) return;
  try {
    const data = await api(`/api/slate?date=${d}`);
    state.slateGames = data.games;
    state._slateDate = d;
  } catch {}
}

let pollHandle = null;

async function pollDraft() {
  if (!state.currentDraftId) return;
  if (state.tab !== "draft") return;
  try {
    const data = await api(`/api/drafts/${state.currentDraftId}`);
    const newCount = (data.picks || []).length;
    const myTurn = isMyTurn(data.on_the_clock) || canJumpForSP();
    // Re-render fully if a pick happened or the turn flipped to/from us.
    if (newCount !== state.lastPicksCount || myTurn !== state._myTurnAtLastRender) {
      await renderDraft();
    } else {
      // Cheap update: identity bar / status text only.
      renderIdentityBar(data);
    }
  } catch {}
}

function startPolling() {
  if (pollHandle) return;
  pollHandle = setInterval(pollDraft, 4000);
}
function stopPolling() {
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = null;
}

// ---- K Prop math (ported from Yaakov's Colab notebook) ----
function estimateOverProbability(prediction, line, stdDev = 1.5) {
  const z = (line + 0.5 - prediction) / stdDev;
  if (z > 3) return 0.01;
  if (z < -3) return 0.99;
  return 1 / (1 + Math.exp(1.7 * z));
}
function americanToImplied(odds) {
  return odds > 0 ? 100 / (odds + 100) : Math.abs(odds) / (Math.abs(odds) + 100);
}
function americanToDecimal(odds) {
  return odds > 0 ? odds / 100 + 1 : 100 / Math.abs(odds) + 1;
}
function calcEV(winProb, odds) {
  return (winProb * (americanToDecimal(odds) - 1) - (1 - winProb)) * 100;
}
function fmtPct(p) { return (p * 100).toFixed(1) + "%"; }
function fmtSigned(n, digits = 1) { return (n > 0 ? "+" : "") + n.toFixed(digits); }
function kpropsKey(date, pid) { return `kprops:${date}:${pid}`; }
function loadKpropEntry(date, pid) {
  try { return JSON.parse(localStorage.getItem(kpropsKey(date, pid))) || {}; }
  catch { return {}; }
}
function saveKpropEntry(date, pid, entry) {
  localStorage.setItem(kpropsKey(date, pid), JSON.stringify(entry));
}

async function fetchKPropsOdds(forceRefresh = false) {
  const d = $("#date").value;
  $("#kprops-odds-status").textContent = forceRefresh ? "Force-refreshing from books…" : "Loading odds…";
  try {
    const data = await api(`/api/k_props/odds?date=${d}${forceRefresh ? "&refresh=true" : ""}`);
    if (!data.configured) {
      $("#kprops-odds-status").innerHTML =
        `Not configured. Sign up at <a href="https://the-odds-api.com" target="_blank" style="color:var(--accent);">the-odds-api.com</a> (free 500 req/mo), then run: <code>fly secrets set ODDS_API_KEY=&lt;your_key&gt; --app mlb-dfs-doron</code>`;
      return;
    }
    const pitchers = data.pitchers || {};
    let matched = 0;
    const norm = (s) => (s || "").normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase().trim();
    const normIdx = {};
    for (const [k, v] of Object.entries(pitchers)) normIdx[norm(k)] = v;
    for (const r of (state._kpropsRows || [])) {
      const odds = pitchers[r.pitcher_name] || normIdx[norm(r.pitcher_name)];
      if (!odds) continue;
      const entry = {
        line: String(odds.line),
        over: odds.over_odds != null ? String(odds.over_odds) : "",
        under: odds.under_odds != null ? String(odds.under_odds) : "",
      };
      saveKpropEntry(d, r.pitcher_id, entry);
      matched += 1;
    }
    const total = (state._kpropsRows || []).length;
    const fetchedTime = data.fetched_at
      ? new Date(data.fetched_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
      : "?";
    const src = data.cached ? `📁 cached from ${fetchedTime}` : `🆕 fresh from books at ${fetchedTime}`;
    $("#kprops-odds-status").innerHTML = `${src}, matched ${matched}/${total}. <span class="muted">(One save per day; yesterday's auto-deleted.)</span>`;
    redrawKProps();
  } catch (e) {
    $("#kprops-odds-status").textContent = `Error: ${e.message}`;
  }
}

$("#kprops-fetch-odds")?.addEventListener("click", () => fetchKPropsOdds(false));
$("#kprops-refresh-odds")?.addEventListener("click", () => fetchKPropsOdds(true));

async function loadInsights() {
  const d = $("#date").value;
  $("#insights-out").innerHTML = `<div class="muted">Loading insights for ${d}…</div>`;
  let data;
  try {
    data = await api(`/api/insights?date=${d}`);
  } catch (e) {
    $("#insights-out").innerHTML = `<div class="muted">${e.message}</div>`;
    return;
  }
  if (!data.games.length) {
    $("#insights-out").innerHTML = `<div class="muted">No games for ${d}.</div>`;
    return;
  }
  const fmt = (v, d=1) => v == null ? "—" : Number(v).toFixed(d);
  const fmtPlusMinus = (v, d=2) => v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(d);
  const colorFactor = (f) => {
    if (f == null || f === 1) return "";
    if (f > 1.05) return "color:var(--accent-2);font-weight:600;";
    if (f < 0.95) return "color:var(--bad);font-weight:600;";
    return "color:var(--muted);";
  };
  const rows = data.games.map(g => {
    const wx = g.weather || {};
    const ump = g.ump || {};
    const wxStr = wx.dome
      ? "🏟️ dome"
      : wx.wind_mph != null ? `${wx.temp_f ?? "?"}°F · wind ${wx.wind_mph}mph ${wx.wind_dir}` : "—";
    return `<tr>
      <td><b>${g.matchup}</b><br/><span class="muted" style="font-size:11px;">${
        g.gameDate ? new Date(g.gameDate).toLocaleTimeString("en-US",{hour:"numeric",minute:"2-digit",timeZone:"America/New_York"})+" ET" : ""
      }</span></td>
      <td>${fmt(g.away_total, 2)}</td>
      <td>${fmt(g.home_total, 2)}</td>
      <td>${fmt((g.away_total||0)+(g.home_total||0), 2)}</td>
      <td>${wxStr}</td>
      <td style="${colorFactor(wx.hr_factor)}">${fmt(wx.hr_factor, 3)}</td>
      <td>${ump.ump || "—"}</td>
      <td>${ump.season ? fmtPlusMinus(ump.season.favor, 2) : "—"}</td>
      <td style="${colorFactor(ump.k_factor)}">${fmt(ump.k_factor, 3)}</td>
    </tr>`;
  }).join("");
  $("#insights-out").innerHTML = `
    <table>
      <thead><tr>
        <th>Matchup</th>
        <th>Away total</th><th>Home total</th><th>Game total</th>
        <th>Weather</th><th>HR factor</th>
        <th>HP Ump</th><th>Favor</th><th>K factor</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function loadKProps() {
  const d = $("#date").value;
  $("#kprops-out").innerHTML = `<div class="muted">Predicting Ks for ${d}…</div>`;
  let data;
  try {
    data = await api(`/api/k_props?date=${d}`);
  } catch (e) {
    $("#kprops-out").innerHTML = `<div class="muted">${e.message}</div>`;
    return;
  }
  if (!data.rows.length) {
    $("#kprops-out").innerHTML = `<div class="muted">No probable SPs announced for ${d}.</div>`;
    return;
  }

  state._kpropsRows = data.rows;
  state._kpropsDate = d;

  $("#kprops-out").innerHTML = `
    <p class="muted" style="font-size:12px;">
      Type the sportsbook line + American odds for each pitcher.
      "Our %" is from the prediction with σ=1.5 (logistic approx of normal CDF,
      same formula as the Colab). "Edge" is Our − Implied. "EV" is per $100 bet.
      Positive-EV picks are highlighted in green.
    </p>
    <table id="kprops-table">
      <thead><tr>
        <th>Pitcher</th>
        <th>Pred Ks</th>
        <th>Line</th>
        <th>Over odds</th>
        <th>Under odds</th>
        <th>Implied O%</th>
        <th>Our O%</th>
        <th>Edge</th>
        <th>Over EV</th>
        <th>Under EV</th>
      </tr></thead>
      <tbody></tbody>
    </table>`;
  redrawKProps();
}

function redrawKProps() {
  const rows = state._kpropsRows || [];
  const date = state._kpropsDate;
  const tbody = document.querySelector("#kprops-table tbody");
  if (!tbody) return;
  const html = rows.map((r) => {
    const saved = loadKpropEntry(date, r.pitcher_id);
    const line = saved.line ?? "";
    const over = saved.over ?? "";
    const under = saved.under ?? "";
    const note = r.lineup_posted ? "" : `<br/><span class="muted" style="font-size:10px;">no lineup yet — rookie K%</span>`;

    let calc = { ours: null, implied: null, edge: null, evO: null, evU: null };
    if (line !== "" && !isNaN(parseFloat(line))) {
      calc.ours = estimateOverProbability(r.predicted_ks, parseFloat(line));
      if (over !== "" && !isNaN(parseFloat(over))) {
        calc.implied = americanToImplied(parseFloat(over));
        calc.edge = calc.ours - calc.implied;
        calc.evO = calcEV(calc.ours, parseFloat(over));
      }
      if (under !== "" && !isNaN(parseFloat(under))) {
        calc.evU = calcEV(1 - calc.ours, parseFloat(under));
      }
    }

    const goodEV = (calc.evO != null && calc.evO >= 3) || (calc.evU != null && calc.evU >= 3);
    return `
      <tr class="${goodEV ? "kprops-good" : ""}" data-pid="${r.pitcher_id}">
        <td><b>${r.pitcher_name}</b><span class="muted" style="font-size:11px;"> · ${r.matchup || (r.pitcher_team + "@" + r.home_team)} (${r.is_home ? "home" : "away"}) · K%${(r.pitcher_k_pct*100).toFixed(0)} · park ${r.park_factor.toFixed(2)}</span>${note}</td>
        <td><b>${r.predicted_ks.toFixed(2)}</b></td>
        <td><input class="kp-line" type="number" step="0.5" value="${line}" placeholder="6.5" style="width:60px;" /></td>
        <td><input class="kp-over" type="number" step="5" value="${over}" placeholder="-110" style="width:70px;" /></td>
        <td><input class="kp-under" type="number" step="5" value="${under}" placeholder="-110" style="width:70px;" /></td>
        <td>${calc.implied != null ? fmtPct(calc.implied) : "—"}</td>
        <td>${calc.ours != null ? fmtPct(calc.ours) : "—"}</td>
        <td class="${calc.edge != null ? (calc.edge > 0 ? "edge-pos" : "edge-neg") : ""}">${calc.edge != null ? fmtSigned(calc.edge * 100) + "%" : "—"}</td>
        <td class="${calc.evO != null ? (calc.evO > 0 ? "edge-pos" : "edge-neg") : ""}">${calc.evO != null ? fmtSigned(calc.evO) : "—"}</td>
        <td class="${calc.evU != null ? (calc.evU > 0 ? "edge-pos" : "edge-neg") : ""}">${calc.evU != null ? fmtSigned(calc.evU) : "—"}</td>
      </tr>`;
  }).join("");
  tbody.innerHTML = html;

  // Wire input listeners — auto-save + recompute on blur/change.
  tbody.querySelectorAll("tr").forEach((tr) => {
    const pid = Number(tr.dataset.pid);
    const onChange = () => {
      const entry = {
        line:  tr.querySelector(".kp-line").value,
        over:  tr.querySelector(".kp-over").value,
        under: tr.querySelector(".kp-under").value,
      };
      saveKpropEntry(date, pid, entry);
      redrawKProps();
    };
    tr.querySelectorAll("input").forEach((inp) => inp.addEventListener("change", onChange));
  });
}

async function refresh() {
  await loadDraftList().catch(() => {});
  if (state.tab === "slate") await loadSlate();
  if (state.tab === "project") await loadProjections();
  if (state.tab === "kprops") await loadKProps();
  if (state.tab === "insights") await loadInsights();
  if (state.tab === "lineup") {/* nothing to load — user-driven */}
  if (state.tab === "draft") {
    await ensureSlateLoaded();
    renderGamePicker();
    await renderDraft();
    startPolling();
  } else {
    stopPolling();
  }
  if (state.tab === "schedule") initScheduleTab();
  if (state.tab === "stats") loadStats();
  if (state.tab === "score") {
    const today = new Date().toISOString().slice(0, 10);
    const sel = $("#score-draft-id");
    if (sel && sel.options.length) {
      const opts = Array.from(sel.options).map((o) => o.value);
      if (opts.includes(today)) sel.value = today;
      else if (state.currentDraftId && opts.includes(state.currentDraftId)) sel.value = state.currentDraftId;
      else if (!sel.value) sel.value = opts[0];
      if (sel.value) $("#score-load").click();
    }
  }
}

// ---------- Stats tab ----------

async function loadStats() {
  $("#stats-out").innerHTML = `<div class="muted">Loading…</div>`;
  try {
    const [stand, players] = await Promise.all([
      api(`/api/stats/standings`),
      api(`/api/stats/players?top_n=80`),
    ]);
    renderStats(stand, players);
  } catch (e) {
    $("#stats-out").innerHTML = `<div class="muted">${e.message}</div>`;
  }
}

function renderStats(stand, players) {
  const recHeader = `<tr><th>Drafter</th><th>1st</th><th>2nd</th><th>3rd</th><th>Days</th><th>Total Pts</th><th>Avg/d</th><th>Max</th><th>Min</th></tr>`;
  const recRows = stand.records
    .map(
      (r) => `<tr>
        <td><b>${r.drafter}</b></td>
        <td>${r.first}</td>
        <td>${r.second}</td>
        <td>${r.third}</td>
        <td>${r.days}</td>
        <td>${r.total_points.toFixed(2)}</td>
        <td>${r.avg_points.toFixed(2)}</td>
        <td>${r.max_points.toFixed(2)}</td>
        <td>${r.min_points.toFixed(2)}</td>
      </tr>`,
    )
    .join("");
  const recordsTbl = `
    <h3>All-time records</h3>
    <table>
      <thead>${recHeader}</thead>
      <tbody>${recRows}</tbody>
    </table>`;

  // Per-day table
  const drafters = stand.records.map((r) => r.drafter);
  const dayHeader = `<tr><th>Date</th>` +
    drafters.map((d) => `<th colspan="2">${d}</th>`).join("") +
    `</tr><tr><th></th>` +
    drafters.map(() => `<th>Rank</th><th>Total</th>`).join("") +
    `</tr>`;
  const dayRows = stand.per_day
    .slice()
    .reverse()
    .map((d) => {
      const cells = drafters
        .map((dr) => {
          const s = d.standings.find((x) => x.drafter === dr);
          if (!s) return `<td></td><td></td>`;
          const rankClass = s.rank === 1 ? "rank-1" : s.rank === 3 ? "rank-3" : "";
          return `<td class="${rankClass}">${s.rank}</td><td>${s.total.toFixed(2)}</td>`;
        })
        .join("");
      const src = d.source === "historic" ? `<span class="muted">·</span>` : "";
      return `<tr><td>${d.date} ${src}</td>${cells}</tr>`;
    })
    .join("");
  const perDayTbl = `
    <h3 style="margin-top:24px;">Per-day standings <span class="muted" style="font-weight:400;font-size:12px;">(· = imported from spreadsheet)</span></h3>
    <div style="max-height:420px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;">
      <table><thead>${dayHeader}</thead><tbody>${dayRows}</tbody></table>
    </div>`;

  function renderPlayerTbl(title, list) {
    const cols = drafters;
    const head = `<tr><th>${title}</th><th>Pos</th>` +
      cols.map((d) => `<th>${d}</th>`).join("") +
      `<th>Total</th><th>Avg</th>` +
      cols.map((d) => `<th>${d} Avg</th>`).join("") +
      `</tr>`;
    const body = list
      .map(
        (p) => `<tr>
          <td>${p.name}</td>
          <td>${p.position ?? "-"}</td>
          ${cols.map((d) => `<td>${p.picks_by_drafter[d] || 0}</td>`).join("")}
          <td><b>${p.total_picks}</b></td>
          <td>${p.avg_per_pick.toFixed(2)}</td>
          ${cols.map((d) => {
            const v = p.avg_per_drafter[d];
            return `<td>${v == null ? "-" : v.toFixed(2)}</td>`;
          }).join("")}
        </tr>`,
      )
      .join("");
    return `<h3 style="margin-top:24px;">${title} — top ${list.length} by pick volume</h3>
      <div style="max-height:420px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;">
        <table><thead>${head}</thead><tbody>${body}</tbody></table>
      </div>`;
  }

  $("#stats-out").innerHTML =
    recordsTbl +
    perDayTbl +
    renderPlayerTbl("Hitters", players.hitters) +
    renderPlayerTbl("Pitchers", players.pitchers);
}

// ---------- Schedule tab ----------

let scheduleResult = null;

function initScheduleTab() {
  const today = $("#date").value;
  if (!$("#sched-start").value) $("#sched-start").value = today;
  if (!$("#sched-end").value) {
    const d = new Date(today);
    d.setDate(d.getDate() + 6);
    $("#sched-end").value = d.toISOString().slice(0, 10);
  }
}

async function setDefaultScheduleRange() {
  // Default start = day after the latest already-saved draft (so we don't
  // propose dates that are already locked in). End = start + 6 days.
  try {
    const data = await api(`/api/drafts`);
    const ids = data.drafts || [];
    let latest = null;
    for (const d of ids) {
      if (/^\d{4}-\d{2}-\d{2}$/.test(d) && (latest === null || d > latest)) {
        latest = d;
      }
    }
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const baseline = latest ? new Date(latest) : new Date(today);
    const start = new Date(baseline);
    start.setDate(start.getDate() + 1);
    if (start < today) start.setTime(today.getTime());
    // League plays Sun-Thu only — skip Fri (5) and Sat (6) for the start date.
    while (start.getDay() === 5 || start.getDay() === 6) {
      start.setDate(start.getDate() + 1);
    }
    const end = new Date(start);
    end.setDate(end.getDate() + 6);
    $("#sched-start").value = start.toISOString().slice(0, 10);
    $("#sched-end").value = end.toISOString().slice(0, 10);
  } catch {}
}

$("#sched-build").addEventListener("click", async () => {
  const start = $("#sched-start").value;
  const end = $("#sched-end").value;
  const size = $("#sched-size").value || "5";
  if (!start || !end) return alert("Pick a start and end date.");
  $("#sched-out").innerHTML = `<div class="muted">Building (this fetches each day's slate from MLB)…</div>`;
  try {
    const data = await api(`/api/schedule_builder?start=${start}&end=${end}&slate_size=${size}`);
    scheduleResult = data;
    renderSchedule(data);
    $("#sched-apply-row").hidden = false;
  } catch (e) {
    $("#sched-out").innerHTML = `<div class="muted">${e.message}</div>`;
  }
});

function renderSchedule(data) {
  const days = data.days
    .map((day) => {
      const chips = day.selected_games
        .map((g) => `<span class="matchup-chip">${g.away_abbr} @ ${g.home_abbr}</span>`)
        .join("");
      return `<div class="sched-day">
        <h4>${day.date} <span class="muted" style="font-weight:400;">— ${day.selected_games.length} games</span></h4>
        <div class="matchups">${chips}</div>
      </div>`;
    })
    .join("");
  const min = data.min_count;
  const max = data.max_count;
  const teamRow = Object.entries(data.team_counts)
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([t, c]) => {
      const cls = c === min ? "low" : c === max && min !== max ? "high" : "";
      return `<span class="team ${cls}">${t}: ${c}</span>`;
    })
    .join("");
  $("#sched-out").innerHTML = `
    ${days}
    <div class="team-counts">
      <div class="muted" style="margin-bottom:6px;">Team appearances after applying this schedule (green = lowest, orange = highest, range ${min}–${max}):</div>
      <div class="row">${teamRow}</div>
    </div>`;
}

$("#sched-apply").addEventListener("click", async () => {
  if (!scheduleResult) return alert("Build a schedule first.");
  const drafters = $("#sched-drafters").value
    .split(",").map((s) => s.trim()).filter(Boolean);
  if (drafters.length < 2) return alert("Need at least 2 drafters.");
  const randomize = $("#sched-randomize").checked;
  const days = scheduleResult.days.map((d) => ({
    date: d.date,
    game_pks: d.selected_games.map((g) => g.gamePk),
  }));
  const out = $("#sched-apply-out");
  out.textContent = "Creating drafts…";
  try {
    const data = await api(`/api/schedule_builder/apply`, {
      method: "POST",
      body: JSON.stringify({ drafters, days, randomize_order: randomize }),
    });
    out.innerHTML =
      `Created ${data.created.length} drafts, skipped ${data.skipped.length}. ` +
      `<a href="#" id="sched-go-draft">Switch to Draft tab</a> to load any of them.`;
    $("#sched-go-draft").addEventListener("click", (e) => {
      e.preventDefault();
      document.querySelector('nav button[data-tab="draft"]').click();
    });
    await loadDraftList();
  } catch (e) {
    out.textContent = e.message;
  }
});
refresh();
