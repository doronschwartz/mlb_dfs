const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

// Tap-to-toggle for projection tooltips on touch devices (mobile-friendly).
// On hover-capable devices CSS handles it; this just enables tap on touch.
document.addEventListener("click", (e) => {
  const cell = e.target.closest(".player-cell");
  // Don't toggle if the user clicked an interactive child (button, pill, link).
  if (cell && !e.target.closest("button, a, .slot-pill, .move-btn, .replace-btn")) {
    const wasShown = cell.classList.contains("show-tooltip");
    document.querySelectorAll(".player-cell.show-tooltip").forEach((el) => el.classList.remove("show-tooltip"));
    if (!wasShown) cell.classList.add("show-tooltip");
    return;
  }
  // Click outside any tooltip closes it.
  if (!e.target.closest(".breakdown-tooltip")) {
    document.querySelectorAll(".player-cell.show-tooltip").forEach((el) => el.classList.remove("show-tooltip"));
  }
});

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
// During non-SP free-for-all, snake order still applies: only the next
// drafter in the snake (skipping the held-up lone SP-needer) gets the OOO
// non-SP pick at any one moment. Backend computes that drafter as
// next_ooo_drafter — when it's me, I can jump.
// Inverse case: when every OTHER drafter only needs SPs, I'm the lone
// non-SP-needer and can grab my remaining hitter/UTIL/BN picks freely.
function canJumpForNonSP() {
  if (!state.identity) return false;
  if (state._nextOooDrafter && state._nextOooDrafter === state.identity) return true;
  if (state._hitterFreeDrafter && state._hitterFreeDrafter === state.identity) return true;
  return false;
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

// Injury badge — surfaces ESPN injury-report data attached to projections.
// Day-To-Day (still active, banged up) and IL placements are both flagged so
// users see at a glance if a player has a recent injury concern. Hover gives
// the type + return date + news comment.
function injuryBadge(inj) {
  if (!inj || !inj.status) return "";
  const s = (inj.status || "").toLowerCase();
  let label, cls;
  if (s.includes("day-to-day")) { label = "D2D"; cls = "d2d"; }
  else if (s.includes("10-day")) { label = "10-IL"; cls = "il"; }
  else if (s.includes("15-day")) { label = "15-IL"; cls = "il"; }
  else if (s.includes("60-day")) { label = "60-IL"; cls = "il"; }
  else return "";
  const detail = [inj.type, inj.return_date ? `back ~${inj.return_date}` : null]
    .filter(Boolean).join(" · ");
  const tip = `${inj.status}${detail ? " — " + detail : ""}${inj.comment ? "\n\n" + inj.comment : ""}`;
  return `<span class="injury-tag ${cls}" title="${escapeAttr(tip)}">🤕 ${label}</span>`;
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
      // Lazy-load today's trivia panel. Failure is silent (panel stays hidden).
      loadTrivia().catch(() => {});
      // Lazy-load the dynasty-rank map (only the draft pool uses it). Was
      // fetched eagerly on every page load; now only when you actually draft.
      // Re-draw the pool once it lands so ranks fill in.
      if (!state._dynastyMap) {
        _loadDynasty().then(() => { if (poolCache.pool.length) drawPool(); }).catch(() => {});
      }
      // Auto-load today's draft if one exists — else the next upcoming, else
      // the most recent past one. This logic used to fire here, then accidentally
      // got moved into the HOF branch in an earlier refactor (where `today` was
      // out of scope and it failed silently). Restoring to the Draft branch
      // means clicking the Draft tab no longer leaves a 2-week-old draft loaded.
      const sel = $("#draft-id");
      const opts = sel ? Array.from(sel.options).map((o) => o.value).filter(Boolean) : [];
      let pick = null;
      if (opts.includes(today)) {
        pick = today;
      } else if (opts.length) {
        const upcoming = opts.filter((d) => d >= today).sort();
        const past = opts.filter((d) => d < today).sort().reverse();
        pick = upcoming[0] || past[0];
      }
      if (sel && pick) {
        sel.value = pick;
        state.currentDraftId = pick;
        if ($("#date").value !== pick) {
          $("#date").value = pick;
          state._slateDate = null;
          state.slateGames = [];
        }
        syncToLoadedDraft().catch(() => {});
      } else {
        state.currentDraftId = null;
      }
    }
    if (b.dataset.tab === "deadline") {
      loadDeadline();
    }
    if (b.dataset.tab === "farm" && !window._farmLoaded) {
      loadFarm();
    }
    if (b.dataset.tab === "hof") {
      loadHallOfFame().catch(() => {});
    }
    if (b.dataset.tab === "dynasty") {
      loadDynasty().catch(() => {});
    }
    refresh();
  });
});

$("#refresh").addEventListener("click", async () => {
  // Bust the server-side projections cache so updated probable SPs / lineups show up.
  // Refresh is slow (20-30s server recompute), so give the user explicit
  // progress feedback: disable the button, swap label, show ✓ when done.
  const btn = $("#refresh");
  const originalLabel = btn.textContent;
  btn.disabled = true;
  btn.classList.add("loading");
  btn.textContent = "⏳ Refreshing…";
  const d = $("#date").value;
  const t0 = performance.now();
  let ok = true;
  try {
    await api(`/api/projections?date=${d}&refresh=true`);
  } catch (e) {
    ok = false;
    console.error("refresh failed:", e);
  }
  // Reset client-side caches for the same reason.
  projCache = { date: null, data: [] };
  poolCache = { draftId: null, pool: [] };
  state._slateDate = null;
  state.slateGames = [];
  try {
    await refresh();
  } catch (e) {
    ok = false;
    console.error("post-refresh render failed:", e);
  }
  const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
  btn.disabled = false;
  btn.classList.remove("loading");
  btn.classList.add(ok ? "done" : "failed");
  btn.textContent = ok ? `✓ Done (${elapsed}s)` : `⚠ Failed — try again`;
  // After a moment, revert to the original label so the button is ready
  // for the next press without a stale state.
  setTimeout(() => {
    btn.classList.remove("done", "failed");
    btn.textContent = originalLabel;
  }, ok ? 2500 : 5000);
});

// Changelog modal — fetches /api/changelog and renders it in a modal overlay.
async function openChangelog() {
  const modal = $("#changelog-modal");
  const body = $("#changelog-body");
  body.innerHTML = '<div class="muted">Loading…</div>';
  modal.style.display = "flex";
  try {
    const data = await api("/api/changelog");
    const current = data.current || "";
    const html = (data.entries || []).map((e, i) => {
      const isCurrent = i === 0;
      const items = (e.changes || []).map(c => `<li>${c}</li>`).join("");
      return `<div class="changelog-entry${isCurrent ? " current" : ""}">
        <h3>${e.version}${isCurrent ? ' <span class="bench-tag" style="background:rgba(52,211,153,0.25);color:var(--accent-2);">CURRENT</span>' : ""}</h3>
        <div class="changelog-title">${e.title || ""}</div>
        <ul>${items}</ul>
      </div>`;
    }).join("");
    body.innerHTML = `<p class="muted" style="margin-top:0;">Live model version: <code>${current}</code></p>${html}`;
  } catch (err) {
    body.innerHTML = `<div class="muted">Failed to load: ${err.message || err}</div>`;
  }
}
$("#changelog-btn")?.addEventListener("click", openChangelog);
$("#changelog-close")?.addEventListener("click", () => { $("#changelog-modal").style.display = "none"; });
$("#changelog-modal")?.addEventListener("click", (e) => {
  if (e.target.id === "changelog-modal") $("#changelog-modal").style.display = "none";
});

// ---------- Daily trivia ----------
// Tied to today's slate. Each drafter on the draft answers 3 quick questions.
// Score saved to backend; season-long leaderboard via "🏆 Season" button.
//
// Spoiler-safe flow:
//   1. Panel opens with ONLY a "Who are you?" picker — no questions visible.
//   2. User picks themselves → if they've already answered today, fetch
//      their stored result (with full reveal). Otherwise, render fresh
//      questions ready to answer. Either way, other drafters' answers /
//      explainers are NEVER shown.
//   3. Submit → reveal with hints + explainers shown for their picks.
let triviaState = { date: null, data: null, picked: {}, submitted: false, drafterSelected: null };

async function loadTrivia() {
  const panel = $("#trivia-panel");
  if (!panel) return;
  const date = $("#date").value;
  if (!date) { panel.hidden = true; return; }
  let data;
  try { data = await api(`/api/trivia/${date}`); } catch (e) { panel.hidden = true; return; }
  if (!data || !data.questions || data.questions.length === 0) { panel.hidden = true; return; }
  triviaState = { date, data, picked: {}, submitted: false, drafterSelected: null, lastResult: null };
  panel.hidden = false;
  populateTriviaDrafters();
  renderTrivia();
}

// Default drafter pool when no draft is loaded — the regular crew.
const DEFAULT_TRIVIA_DRAFTERS = ["Meech", "Stock", "JL"];

function populateTriviaDrafters() {
  const sel = $("#trivia-drafter");
  if (!sel) return;
  // Prefer drafters from the current draft state; fall back to the regular
  // crew (Meech / Stock / JL) so trivia is playable even with no draft loaded.
  const draftDrafters = (state.lastDraftState && state.lastDraftState.drafters) || [];
  const opts = draftDrafters.length ? draftDrafters : DEFAULT_TRIVIA_DRAFTERS.slice();
  const submissions = (triviaState.data && triviaState.data.submissions) || [];
  const submittedSet = new Set(submissions.map(s => s.drafter));
  // First option is the "who are you?" placeholder. Real drafters follow. We
  // mark ✓ next to anyone who's already answered (no spoilers — just shows
  // they've played), but we DO NOT show their score until they re-select
  // themselves (then we fetch their result endpoint).
  const placeholder = `<option value="" selected>— Who are you? —</option>`;
  const drafterOpts = opts.map(d => {
    const marker = submittedSet.has(d) ? " ✓" : "";
    return `<option value="${d}">${d}${marker}</option>`;
  }).join("");
  sel.innerHTML = placeholder + drafterOpts;
  sel.value = "";  // Always start unselected — never auto-pick from localStorage
                   // because that would leak the previously-answered state to
                   // whoever opens the page next.
  sel.onchange = async () => {
    const drafter = sel.value;
    triviaState.drafterSelected = drafter || null;
    triviaState.picked = {};
    triviaState.submitted = false;
    triviaState.lastResult = null;
    if (!drafter) { renderTrivia(); return; }
    // If this drafter has already submitted, fetch their stored result so
    // they can review their own answers. The reveal stays private to them
    // because the dropdown resets to the placeholder on next page load.
    if (submittedSet.has(drafter)) {
      try {
        const r = await api(`/api/trivia/${triviaState.date}/result/${encodeURIComponent(drafter)}`);
        // If the question set was regenerated since they answered
        // (from_gen_version stamped), their per-question picks are no longer
        // valid against the current questions. Let them play the new set
        // fresh — the old score still counts for the season leaderboard.
        if (r && r.score != null && !r.from_gen_version && Object.keys(r.answers || {}).length) {
          triviaState.lastResult = r;
          triviaState.submitted = true;
          triviaState.picked = r.answers || {};
        }
      } catch {}
    }
    renderTrivia();
  };
}

function renderTrivia() {
  const out = $("#trivia-questions");
  const subOut = $("#trivia-submissions");
  if (!out || !triviaState.data) return;
  // Future-date slate isn't finalized yet (no probable pitchers / Vegas /
  // lineups). Show a clear "come back" message instead of generating a
  // quiz against stale signals.
  if (triviaState.data.not_yet_available) {
    const reason = triviaState.data.reason || "Slate isn't finalized yet — come back on the day of the slate.";
    out.innerHTML = `<div class="trivia-q-prompt muted" style="text-align:center;padding:18px 12px;line-height:1.5;">🗓️ <strong>Quiz not ready yet for ${triviaState.data.date}</strong><br><span style="font-size:12px;">${reason}</span></div>`;
    if (subOut) subOut.innerHTML = "";
    // Also hide the drafter picker since there's nothing to do.
    const sel = $("#trivia-drafter");
    if (sel) sel.disabled = true;
    return;
  }
  const qs = triviaState.data.questions;
  const drafter = triviaState.drafterSelected;
  const submissions = (triviaState.data && triviaState.data.submissions) || [];
  // Pre-selection: hide everything except the picker. No question prompts,
  // no submissions list, no answers — nothing that could spoil even passively.
  if (!drafter) {
    out.innerHTML = `<div class="trivia-q-prompt muted" style="text-align:center;padding:14px 0;">Pick yourself from the dropdown above to start today's trivia (${qs.length} questions about tonight's slate).</div>`;
    if (subOut) subOut.innerHTML = "";
    return;
  }
  const subMap = new Map(submissions.map(s => [s.drafter, s.score]));
  const alreadySubmitted = drafter && subMap.has(drafter);
  out.innerHTML = qs.map((q, i) => {
    const picked = triviaState.picked[q.id];
    const correct = triviaState.lastResult && triviaState.lastResult.correct ? triviaState.lastResult.correct[q.id] : null;
    const explainer = triviaState.lastResult && triviaState.lastResult.explainers ? triviaState.lastResult.explainers[q.id] : null;
    const perQ = triviaState.lastResult && triviaState.lastResult.per_q ? triviaState.lastResult.per_q[q.id] : null;
    // Numeric guess question (v3): single number input, partial credit on submit.
    const isNumeric = q.input === "number" || (q.kind || "").startsWith("numeric_");
    if (isNumeric) {
      const userVal = picked;
      let resultLine = "";
      if (triviaState.submitted) {
        const pct = perQ != null ? Math.round(perQ * 100) : 0;
        const cls = pct >= 100 ? "correct" : pct >= 50 ? "picked" : "wrong";
        resultLine = `<div class="trivia-numeric-result ${cls}">You: ${userVal ?? "—"} · Actual: <strong>${correct}</strong> · ${pct}% credit</div>`;
      }
      return `<div class="trivia-q">
        <div class="trivia-q-prompt">${i+1}. ${q.prompt} <span class="muted" style="font-size:11px;">(close counts — partial credit)</span></div>
        <div class="trivia-numeric-wrap">
          <input type="number" inputmode="numeric" class="trivia-numeric" data-qid="${q.id}"
                 value="${userVal ?? ""}" ${triviaState.submitted ? "disabled" : ""}
                 placeholder="Enter your best guess">
        </div>
        ${resultLine}
        ${explainer ? `<div class="trivia-explainer">${explainer}</div>` : ""}
      </div>`;
    }
    return `<div class="trivia-q">
      <div class="trivia-q-prompt">${i+1}. ${q.prompt}</div>
      <div class="trivia-options">
        ${q.options.map((opt, idx) => {
          let cls = "trivia-option";
          if (triviaState.submitted) {
            if (idx === correct) cls += " correct";
            else if (idx === picked) cls += " wrong";
          } else if (idx === picked) cls += " picked";
          // Reveal the per-option hint (e.g. '15 HR') only AFTER submission.
          // Pre-submit, hints would give away the answer (leader = correct).
          const revealedHint = triviaState.submitted && triviaState.lastResult && triviaState.lastResult.hints
            ? (triviaState.lastResult.hints[q.id] || [])[idx] : null;
          const hintHtml = revealedHint ? `<span class="hint">${revealedHint}</span>` : "";
          return `<button class="${cls}" data-qid="${q.id}" data-idx="${idx}" ${triviaState.submitted ? "disabled" : ""}>
            <strong>${opt.label}</strong>${hintHtml}
          </button>`;
        }).join("")}
      </div>
      ${explainer ? `<div class="trivia-explainer">${explainer}</div>` : ""}
    </div>`;
  }).join("");
  // Submit button or already-submitted notice
  const scoreLine = document.createElement("div");
  scoreLine.className = "trivia-submit";
  if (triviaState.submitted && triviaState.lastResult) {
    // Score may be fractional thanks to numeric-guess partial credit.
    const sc = Number(triviaState.lastResult.score || 0);
    const tot = triviaState.lastResult.total || qs.length;
    const scStr = Math.abs(sc - Math.round(sc)) < 0.01 ? sc.toFixed(0) : sc.toFixed(2);
    scoreLine.innerHTML = `<div class="trivia-score">${drafter} scored ${scStr}/${tot}</div>`;
  } else {
    // Allow submit when every question has SOME answer (numeric: a number, MC: an index).
    const allPicked = qs.every(q => {
      const v = triviaState.picked[q.id];
      if (v == null || v === "") return false;
      return true;
    });
    scoreLine.innerHTML = `<button id="trivia-submit-btn" class="btn-pick" ${allPicked ? "" : "disabled"}>Submit answers</button>`;
  }
  out.appendChild(scoreLine);
  // Wire option clicks (MC questions)
  out.querySelectorAll(".trivia-option").forEach(btn => {
    btn.addEventListener("click", () => {
      if (triviaState.submitted) return;
      const qid = btn.dataset.qid, idx = parseInt(btn.dataset.idx, 10);
      triviaState.picked[qid] = idx;
      renderTrivia();
    });
  });
  // Wire numeric input changes — no re-render on every keystroke (would
  // re-focus and lose the cursor), just sync state on input.
  out.querySelectorAll(".trivia-numeric").forEach(inp => {
    inp.addEventListener("input", () => {
      if (triviaState.submitted) return;
      const qid = inp.dataset.qid;
      const raw = inp.value.trim();
      triviaState.picked[qid] = raw === "" ? null : Number(raw);
      // Just re-enable/disable the submit button — no full re-render.
      const sub = document.getElementById("trivia-submit-btn");
      if (sub) {
        const allPicked = qs.every(q => {
          const v = triviaState.picked[q.id];
          return v != null && v !== "";
        });
        sub.disabled = !allPicked;
      }
    });
  });
  // Wire submit
  const sub = $("#trivia-submit-btn");
  if (sub) sub.addEventListener("click", submitTrivia);
  // Show only that other drafters have played (not their scores). Showing
  // scores here would be a mild spoiler signal — if "Stock got 3/3" appears
  // and you're playing now, that nudges you toward second-guessing your picks.
  if (subOut) {
    const others = submissions.filter(s => s.drafter !== drafter);
    if (others.length) {
      subOut.innerHTML = `Already played today: ${others.map(s => s.drafter).join(", ")}`;
    } else {
      subOut.innerHTML = "";
    }
  }
}

async function submitTrivia() {
  const drafter = $("#trivia-drafter")?.value;
  if (!drafter) { alert("Pick a drafter first"); return; }
  try {
    const result = await api(`/api/trivia/${triviaState.date}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ drafter, answers: triviaState.picked }),
    });
    triviaState.lastResult = result;
    triviaState.submitted = true;
    renderTrivia();
    // Refresh the submission list
    try { triviaState.data = await api(`/api/trivia/${triviaState.date}`); } catch {}
  } catch (e) {
    alert("Submit failed: " + (e.message || e));
  }
}

async function showTriviaLeaderboard() {
  const modal = $("#changelog-modal");
  const body = $("#changelog-body");
  $("#changelog-modal .modal-header h2").textContent = "🏆 Trivia Leaderboard";
  body.innerHTML = '<div class="muted">Loading…</div>';
  modal.style.display = "flex";
  try {
    const data = await api("/api/trivia/leaderboard/season");
    const rows = data.leaderboard || [];
    if (!rows.length) {
      body.innerHTML = '<div class="muted">No trivia answers yet this season — be the first.</div>';
      return;
    }
    body.innerHTML = `
      <table class="recs-table">
        <thead><tr><th>#</th><th>Drafter</th><th>Total</th><th>Days</th><th>Perfect days</th></tr></thead>
        <tbody>${rows.map((r, i) => `
          <tr><td>${i+1}</td><td>${r.drafter}</td><td><b>${r.score}</b></td><td>${r.answered_days}</td><td>${r.perfect_days}</td></tr>
        `).join("")}</tbody>
      </table>`;
  } catch (e) {
    body.innerHTML = `<div class="muted">Failed: ${e.message || e}</div>`;
  }
}
$("#trivia-leaderboard-btn")?.addEventListener("click", showTriviaLeaderboard);

// ---------- Dynasty ----------
// Our rankings (consensus re-shaped by age curve / scarcity / Statcast luck),
// multi-year value, and the trade analyzer. Loads on first tab entry.
let dynastyCache = null;

async function loadDynasty(force = false) {
  const out = $("#dynasty-out");
  if (!out) return;
  if (dynastyCache && !force) { renderDynasty(); return; }
  out.innerHTML = '<div class="muted">Loading our dynasty board…</div>';
  try {
    const data = await api(`/api/dynasty/rankings?limit=500`);
    dynastyCache = data.rankings || [];
    renderDynasty();
  } catch (e) {
    out.innerHTML = `<div class="muted">${e.message}</div>`;
  }
}

function renderDynasty() {
  const out = $("#dynasty-out");
  if (!out || !dynastyCache) return;
  const q = ($("#dyn-search")?.value || "").toLowerCase().trim();
  const rows = dynastyCache.filter(v => !q || v.name.toLowerCase().includes(q));
  const body = rows.slice(0, 500).map((v, i) => {
    const d = v.rank_delta;
    const deltaCls = d > 0 ? "edge-pos" : d < 0 ? "edge-neg" : "muted";
    const deltaStr = d === 0 ? "—" : (d > 0 ? `▲${d}` : `▼${-d}`);
    const luck = v.components.luck_note;
    const luckCls = luck.startsWith("buy") ? "edge-pos" : luck.startsWith("sell") ? "edge-neg" : "muted";
    const ageStr = v.age != null ? v.age : "?";
    return `<tr class="score-row dyn-row" data-idx="${i}" style="cursor:pointer;" title="Click for the full why-they're-here breakdown">
      <td style="font-weight:700;">${v.our_rank}</td>
      <td class="player-cell">${v.name}</td>
      <td>${v.pos}</td>
      <td style="text-align:right;">${ageStr}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums;font-weight:600;">${v.dynasty_score.toFixed(0)}</td>
      <td style="text-align:right;" class="muted">#${v.consensus_rank}</td>
      <td style="text-align:right;" class="${deltaCls}">${deltaStr}</td>
      <td style="font-size:11px;" class="${luckCls}">${luck || ""}</td>
    </tr>
    <tr class="dyn-detail" data-detail="${i}" hidden><td colspan="8" style="background:var(--panel-2);padding:0;"></td></tr>`;
  }).join("");
  // Stash the filtered rows so click handlers can read the breakdown.
  out._dynRows = rows.slice(0, 500);
  out.innerHTML = `<div class="dyn-scroll"><table style="width:100%;font-size:13px;">
    <thead><tr>
      <th>#</th><th>Player</th><th>Pos</th><th style="text-align:right;">Age</th>
      <th style="text-align:right;" title="Our dynasty value: multi-year, age-adjusted, scarcity- and luck-weighted">Dyn value</th>
      <th style="text-align:right;" title="FantraxHQ consensus Roto rank">Cons</th>
      <th style="text-align:right;" title="Spots we differ from consensus (▲ = we're higher on them)">Δ</th>
      <th>Statcast read</th>
    </tr></thead>
    <tbody>${body}</tbody>
  </table></div>
  <div class="muted" style="font-size:11px;margin-top:6px;">Showing ${Math.min(rows.length,500)} of ${rows.length}. Click any row for the breakdown. Search to narrow.</div>`;
  // Wire row clicks → toggle the inline breakdown panel.
  out.querySelectorAll(".dyn-row").forEach(tr => {
    tr.addEventListener("click", () => {
      const idx = tr.dataset.idx;
      const detail = out.querySelector(`.dyn-detail[data-detail="${idx}"]`);
      if (!detail) return;
      if (!detail.hidden) { detail.hidden = true; return; }
      // Collapse any other open panel first.
      out.querySelectorAll(".dyn-detail").forEach(d => { d.hidden = true; });
      const v = out._dynRows[idx];
      detail.querySelector("td").innerHTML = dynastyBreakdownHtml(v);
      detail.hidden = false;
    });
  });
}

function dynastyBreakdownHtml(v) {
  const c = v.components;
  const sk = c.skill;  // {skill_rank, skill_z, talent_value, comps} | null
  const sectionTitle = (t) => `<div style="font-weight:600;font-size:12px;margin:10px 0 4px;color:var(--accent-2);">${t}</div>`;

  // 1) Base value composition — consensus prior blended with OUR skill rank.
  let baseHtml;
  if (sk) {
    const blend = Math.round(c.skill_blend * 100);
    baseHtml = `<div style="font-size:12px;line-height:1.7;">
      <div><b>Base value ${c.rank_base.toFixed(0)}</b> = ${100 - blend}% consensus + ${blend}% our skill model:</div>
      <div style="margin-left:12px;">• Consensus rank <b>#${v.consensus_rank}</b> → value <b>${c.consensus_value.toFixed(0)}</b> <span class="muted">(market prior, exp-decay of rank)</span></div>
      <div style="margin-left:12px;">• Our skill rank <b>#${sk.skill_rank}</b> (z ${sk.skill_z >= 0 ? "+" : ""}${sk.skill_z.toFixed(2)}) → value <b>${sk.talent_value.toFixed(0)}</b> <span class="muted">(from ${sk.is_prospect ? "MiLB production" : "Statcast"} below)</span></div>
    </div>`;
  } else {
    baseHtml = `<div style="font-size:12px;line-height:1.7;">
      <div><b>Base value ${c.rank_base.toFixed(0)}</b> = 100% consensus (no qualifying Statcast sample — prospect / low-PA).</div>
      <div style="margin-left:12px;">• Consensus rank <b>#${v.consensus_rank}</b> → value ${c.consensus_value.toFixed(0)} <span class="muted">(1000·e<sup>−0.0108·(rank−1)</sup>)</span></div>
    </div>`;
  }

  // 2) Skill table. Prospects (MiLB) show production + age-vs-level; MLB
  //    players show the full Statcast slew with z-scores.
  let skillHtml = "";
  if (sk && sk.is_prospect && sk.comps) {
    const m = sk.comps;
    const avl = m.age_vs_level;
    const avlStr = avl == null ? "" : (avl > 0 ? `<span class="edge-pos">${avl} yrs young for level</span>` : avl < 0 ? `<span class="edge-neg">${-avl} yrs old for level</span>` : "age-typical");
    if (v.role === "pitcher") {
      skillHtml = sectionTitle(`Minor-league production (${m.level})`) + `<table style="font-size:12px;">
        <tr><td class="muted" style="padding-right:10px;">Level</td><td style="font-weight:600;">${m.level}</td><td class="muted" style="padding-left:10px;">${avlStr}</td></tr>
        <tr><td class="muted">K-BB%</td><td style="text-align:right;font-weight:600;">${m.kbb_pct != null ? m.kbb_pct + "%" : "—"}</td><td></td></tr>
        <tr><td class="muted">ERA</td><td style="text-align:right;">${m.era != null ? m.era.toFixed(2) : "—"}</td><td></td></tr>
        <tr><td class="muted">sample</td><td style="text-align:right;" class="muted">${m.bf} BF</td><td></td></tr>
      </table>`;
    } else {
      skillHtml = sectionTitle(`Minor-league production (${m.level})`) + `<table style="font-size:12px;">
        <tr><td class="muted" style="padding-right:10px;">Level</td><td style="font-weight:600;">${m.level}</td><td class="muted" style="padding-left:10px;">${avlStr}</td></tr>
        <tr><td class="muted">OPS</td><td style="text-align:right;font-weight:600;">${m.ops != null ? m.ops.toFixed(3) : "—"}</td><td class="muted" style="padding-left:10px;font-size:11px;">MiLB avg ~.700</td></tr>
        <tr><td class="muted">AVG / HR</td><td style="text-align:right;">${m.avg ?? "—"} / ${m.hr ?? "—"}</td><td></td></tr>
        <tr><td class="muted">sample</td><td style="text-align:right;" class="muted">${m.pa} PA</td><td></td></tr>
      </table>
      <div class="muted" style="font-size:11px;margin-top:2px;">Production haircut to an MLB-equivalent by level, then boosted for being young-for-level.</div>`;
    }
  } else if (sk && sk.comps) {
    const m = sk.comps;
    const row = (label, val, fmt, lg, z) => {
      if (val == null) return "";
      const cls = z == null ? "muted" : z > 0.5 ? "edge-pos" : z < -0.5 ? "edge-neg" : "muted";
      const zStr = z == null ? "" : ` <span class="${cls}">(z ${z>=0?"+":""}${z.toFixed(1)})</span>`;
      return `<tr><td class="muted" style="padding:1px 10px 1px 0;">${label}</td><td style="text-align:right;font-weight:600;">${fmt(val)}</td><td class="muted" style="padding-left:10px;font-size:11px;">lg ${lg}${zStr}</td></tr>`;
    };
    const f3 = x => x.toFixed(3), f1 = x => x.toFixed(1);
    const Z = (x, mean, sd) => x == null ? null : (x - mean) / sd;
    if (v.role === "pitcher") {
      skillHtml = sectionTitle("Underlying skill (Statcast, 3-yr weighted)") + `<table style="font-size:12px;">
        ${row("xERA", m.xera, f1, "4.20", m.xera!=null?-Z(m.xera,4.20,0.85):null)}
        ${row("xwOBA-against", m.xwoba_against, f3, ".315", m.xwoba_against!=null?-Z(m.xwoba_against,0.315,0.035):null)}
        ${row("Barrel%-allowed", m.barrel_allowed, f1, "8.0", m.barrel_allowed!=null?-Z(m.barrel_allowed,8.0,3.0):null)}
        ${row("Hard-hit%-allowed", m.hardhit_allowed, f1, "39", m.hardhit_allowed!=null?-Z(m.hardhit_allowed,39,5):null)}
        <tr><td class="muted" style="padding-top:3px;">sample</td><td style="text-align:right;" class="muted">${m.pa} BF</td><td></td></tr>
      </table>`;
    } else {
      skillHtml = sectionTitle("Underlying skill (Statcast, 3-yr weighted)") + `<table style="font-size:12px;">
        ${row("xwOBA", m.xwoba, f3, ".315", Z(m.xwoba,0.315,0.040))}
        ${row("xSLG", m.xslg, f3, ".410", Z(m.xslg,0.410,0.075))}
        ${row("xBA", m.xba, f3, ".245", Z(m.xba,0.245,0.025))}
        ${row("Barrel%", m.barrel, f1, "8.5", Z(m.barrel,8.5,4.2))}
        ${row("Hard-hit%", m.hardhit, f1, "40", Z(m.hardhit,40,6.5))}
        ${row("Sweet-spot%", m.sweetspot, f1, "33", Z(m.sweetspot,33,4.5))}
        <tr><td class="muted" style="padding-top:3px;">sample</td><td style="text-align:right;" class="muted">${m.pa} PA</td><td></td></tr>
      </table>`;
    }
  }

  // 3) Multi-year projection path.
  const curve = (v.projection_curve || []).map(p =>
    `<td style="text-align:center;padding:2px 8px;"><div style="font-size:10px;" class="muted">${p.year}${p.age!=null?` (${p.age})`:""}</div><div style="font-weight:600;">${p.value.toFixed(0)}</div></td>`
  ).join("");

  // 4) Modifiers + plain-English why.
  const why = [];
  if (sk && v.rank_delta > 0) why.push(`We're <b>${v.rank_delta} spots higher</b> than the market — our Statcast skill (rank #${sk.skill_rank}) sees more than the consensus #${v.consensus_rank}.`);
  else if (sk && v.rank_delta < 0) why.push(`We're <b>${-v.rank_delta} spots lower</b> than the market — current Statcast skill (rank #${sk.skill_rank}) trails the consensus #${v.consensus_rank}.`);
  else if (v.rank_delta > 0) why.push(`We're <b>${v.rank_delta} higher</b> than consensus (age-curve / scarcity driven; no Statcast sample).`);
  else if (v.rank_delta < 0) why.push(`We're <b>${-v.rank_delta} lower</b> than consensus.`);
  else why.push(`In line with the consensus (#${v.consensus_rank}).`);
  if (v.age != null) {
    const af = c.age_factor;
    if (v.age <= 25) why.push(`Age ${v.age}: long runway — the 6-yr curve credits prime seasons still ahead (current age factor ×${af.toFixed(2)}).`);
    else if (af >= 0.98) why.push(`Age ${v.age}: at peak (×${af.toFixed(2)}).`);
    else why.push(`Age ${v.age}: past peak (×${af.toFixed(2)}), future years discounted on the aging curve.`);
  }
  if (c.pos_scarcity > 1.0) why.push(`Position <b>${v.pos}</b> scarce → ×${c.pos_scarcity.toFixed(2)} premium.`);
  else if (c.pos_scarcity < 1.0) why.push(`Position <b>${v.pos}</b> replaceable → ×${c.pos_scarcity.toFixed(2)}.`);
  if (c.luck_note) why.push(`Regression read: ${c.luck_note} → ×${c.luck_mult.toFixed(2)}.`);
  if (c.eta_note) why.push(`Prospect timing: ${c.eta_note}.`);
  if (c.traj_note) why.push(`Trajectory (multi-yr): ${c.traj_note} → ×${(c.traj_mult ?? 1).toFixed(2)}.`);
  if (c.young_note) why.push(`Upside: ${c.young_note}.`);
  if (c.multipos_note) why.push(`Flexibility: ${c.multipos_note}.`);
  if (c.durability_note) why.push(`Durability (multi-yr): ${c.durability_note} → ×${(c.durability_mult ?? 1).toFixed(2)}.`);
  if (c.injury_note) why.push(`Injury (now): ${c.injury_note} → ×${(c.injury_mult ?? 1).toFixed(2)}.`);

  return `<div style="padding:12px 14px;">
    <div style="font-weight:700;margin-bottom:8px;">${v.name} — dynasty value ${v.dynasty_score.toFixed(0)}
      <span class="muted" style="font-weight:400;">(our #${v.our_rank} · consensus #${v.consensus_rank}${sk?` · skill #${sk.skill_rank}`:""})</span></div>
    ${sectionTitle("Base value (market prior × our skill)")}
    ${baseHtml}
    ${skillHtml}
    ${sectionTitle("Multi-year projection (base × age curve × scarcity × regression, 10%/yr discount)")}
    <table style="margin:2px 0 6px;border-collapse:collapse;"><tr><td class="muted" style="font-size:11px;padding-right:8px;">value path:</td>${curve}</tr></table>
    ${sectionTitle("Why they're here")}
    <ul style="margin:0 0 0 16px;padding:0;font-size:12px;line-height:1.6;">${why.map(w=>`<li>${w}</li>`).join("")}</ul>
  </div>`;
}

async function evaluateDynastyTrade() {
  const out = $("#dyn-trade-out");
  const parse = (id) => ($(id)?.value || "").split("\n").map(s => s.trim()).filter(Boolean);
  const a = parse("#dyn-side-a"), b = parse("#dyn-side-b");
  if (!a.length || !b.length) { out.innerHTML = `<div class="muted">Add at least one player to each side.</div>`; return; }
  out.innerHTML = `<div class="muted">Evaluating…</div>`;
  try {
    const r = await api(`/api/dynasty/trade`, { method: "POST", body: JSON.stringify({ side_a: a, side_b: b }) });
    const sideHtml = (label, s) => {
      const players = s.players.map(p => `<li>${p.name} <span class="muted">(${p.pos}, ${p.age ?? "?"}) — ${p.dynasty_score.toFixed(0)}</span></li>`).join("");
      const miss = s.missing.length ? `<div class="muted" style="font-size:11px;">⚠ not in top-500: ${s.missing.join(", ")}</div>` : "";
      return `<div><b>Side ${label}</b> — total <b>${s.total.toFixed(0)}</b>${s.avg_age ? ` · avg age ${s.avg_age}` : ""}
        <ul style="margin:4px 0 0 16px;padding:0;font-size:12px;">${players}</ul>${miss}</div>`;
    };
    const ctx = r.context.map(c => `<li>${c}</li>`).join("");
    const bal = r.balancer;
    const balHtml = bal && bal.suggestions && bal.suggestions.length ? `
      <div class="trade-balancer">
        <b>⚖️ To even it:</b> side ${bal.side_to_add} adds ~<b>${bal.gap.toFixed(0)}</b> of value
        (≈ a ${bal.target_value.toFixed(0)}-value player). Closest fits on the board:
        <ul style="margin:5px 0 0 16px;padding:0;font-size:12px;">
          ${bal.suggestions.map(s => `<li><b>${s.name}</b> <span class="muted">(${s.pos}, ${s.age ?? "?"}) — ${s.dynasty_score.toFixed(0)}, our #${s.our_rank}</span></li>`).join("")}
        </ul>
      </div>` : "";
    out.innerHTML = `
      <div style="font-size:15px;font-weight:700;margin-bottom:6px;">${r.verdict} <span class="muted" style="font-weight:400;font-size:12px;">(value gap ${Math.abs(r.diff).toFixed(0)})</span></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">${sideHtml("A", r.side_a)}${sideHtml("B", r.side_b)}</div>
      ${ctx ? `<ul style="margin:8px 0 0 16px;padding:0;font-size:12px;color:var(--muted);">${ctx}</ul>` : ""}
      ${balHtml}`;
  } catch (e) {
    out.innerHTML = `<div class="muted">${e.message}</div>`;
  }
}

// Free-agent pickups + MiLB recon for the user's league.
async function loadDynastyPickups() {
  const out = $("#dyn-pickups-out");
  const status = $("#dyn-pickups-status");
  const lg = ($("#dyn-league")?.value || "").trim();
  if (!lg) { out.innerHTML = `<div class="muted">Enter your Fantrax league_id first.</div>`; return; }
  localStorage.setItem("mlb_dfs_ftx_league", lg);
  status.textContent = "Scanning league rosters + AAA/AA leaderboards…";
  out.innerHTML = "";
  try {
    const r = await api(`/api/dynasty/pickups?league_id=${encodeURIComponent(lg)}`);
    status.textContent = `${r.rostered_count} rostered across ${r.teams_scanned > 0 ? r.teams_scanned + " teams" : "the league"}`;
    out.innerHTML = renderPickups(r);
  } catch (e) {
    status.textContent = "";
    out.innerHTML = `<div class="muted">${e.message}${/401/.test(e.message) ? " — set your Fantrax cookie on the Lineup tab first (private leagues)." : ""}</div>`;
  }
}

function pickupFormBadge(form) {
  if (!form || !form.tag) return "";
  const map = {
    HOT: ["🔥 HOT", "edge-pos"], ELITE: ["⭐ ELITE", "edge-pos"],
    COLD: ["🧊 COLD", "edge-neg"], STEADY: ["STEADY", "muted"],
  };
  const [label, cls] = map[form.tag] || [form.tag, "muted"];
  const pg = form.recent_pg != null ? ` ${form.recent_pg}` : "";
  const vs = (form.recent_pg != null && form.season_pg != null)
    ? ` <span class="muted" style="font-size:10px;">(szn ${form.season_pg})</span>` : "";
  return `<span class="${cls}" style="font-size:11px;font-weight:600;">${label}${pg}</span>${vs}`;
}

function renderPickups(r) {
  const esc = (s) => escapeAttr(String(s ?? ""));
  const avail = r.available || [];
  const risers = r.milb_risers || [];
  const hot = r.hot || [];
  // Best-available from the consensus board (incl. MLB free agents).
  const availRows = avail.map(v => {
    const d = v.rank_delta;
    const deltaCls = d > 0 ? "edge-pos" : d < 0 ? "edge-neg" : "muted";
    const deltaStr = d === 0 ? "—" : (d > 0 ? `▲${d}` : `▼${-d}`);
    const lvl = v.level && v.level.toUpperCase() !== "MLB"
      ? `<span class="lvl-badge">${esc(v.level)}${v.eta ? " · ETA " + esc(v.eta) : ""}</span>` : "";
    return `<tr>
      <td style="font-weight:700;">${v.our_rank}</td>
      <td>${esc(v.name)}${lvl}</td>
      <td>${esc(v.pos)}</td>
      <td style="text-align:right;">${v.age ?? "?"}</td>
      <td>${pickupFormBadge(v.form)}</td>
      <td style="text-align:right;font-weight:600;font-variant-numeric:tabular-nums;">${v.dynasty_score.toFixed(0)}</td>
      <td style="text-align:right;" class="muted">#${v.consensus_rank}</td>
      <td style="text-align:right;" class="${deltaCls}">${deltaStr}</td>
    </tr>`;
  }).join("");
  // Hot streamers — available players running hot right now, regardless of
  // their long-term dynasty value (e.g. a low-value bat on a heater).
  const hotHtml = hot.length ? `
    <div class="pickup-section">
      <h4 style="margin:0 0 4px;">🔥 Hot &amp; available (stream now)</h4>
      <p class="muted" style="font-size:11px;margin:0 0 4px;">Unrostered players running hot on recent form — worth a grab even if dynasty value is modest.</p>
      <table><thead><tr><th>Player</th><th>Pos</th><th>Form (recent pts/G)</th><th style="text-align:right;">Dyn value</th></tr></thead>
        <tbody>${hot.map(v => `<tr>
          <td>${esc(v.name)}</td><td>${esc(v.pos)}</td>
          <td>${pickupFormBadge(v.form)}</td>
          <td style="text-align:right;" class="muted">${v.dynasty_score.toFixed(0)}</td>
        </tr>`).join("")}</tbody></table>
    </div>` : "";
  // Rising prospects from the live AAA/AA scan.
  const riserRows = risers.map(p => {
    const m = p.milb || {};
    const line = p.role === "pitcher"
      ? `${m.kbb_pct != null ? m.kbb_pct + "% K-BB" : ""}${m.era != null ? " · " + m.era + " ERA" : ""} <span class="muted">(${m.bf ?? "?"} BF)</span>`
      : `${m.ops != null ? m.ops.toFixed(3) + " OPS" : ""}${m.hr != null ? " · " + m.hr + " HR" : ""} <span class="muted">(${m.pa ?? "?"} PA)</span>`;
    const young = m.age_vs_level != null && m.age_vs_level > 0
      ? `<span class="edge-pos">+${m.age_vs_level}y young</span>` : (m.age_vs_level === 0 ? "on-age" : "");
    return `<tr>
      <td>${esc(p.name)}</td>
      <td><span class="lvl-badge">${esc(p.level)}</span></td>
      <td>${esc(p.pos)}</td>
      <td style="text-align:right;">${p.age ?? "?"}</td>
      <td class="muted">${esc(p.team)}</td>
      <td style="font-size:12px;">${line}</td>
      <td style="font-size:11px;">${young}</td>
      <td style="text-align:right;font-weight:600;">${p.recon_score.toFixed(0)}</td>
    </tr>`;
  }).join("");
  const availSection = avail.length ? `
    <div class="pickup-section">
      <h4 style="margin:0 0 4px;">💎 Best available (free agents on our board)</h4>
      <table><thead><tr><th>#</th><th>Player</th><th>Pos</th><th style="text-align:right;">Age</th>
        <th>Form</th><th style="text-align:right;">Dyn value</th><th style="text-align:right;">Cons</th><th style="text-align:right;">Δ</th></tr></thead>
        <tbody>${availRows}</tbody></table>
    </div>` : `<div class="muted" style="margin-bottom:10px;">No top-500 board players are unrostered — deep league.</div>`;
  const riserSection = risers.length ? `
    <div class="pickup-section">
      <h4 style="margin:0 0 4px;">🌱 Rising minor leaguers (live AAA/AA recon, not on the consensus board)</h4>
      <p class="muted" style="font-size:11px;margin:0 0 4px;">Young-for-level breakouts off the current leaderboards, scored by our MLB-equivalent prospect model. Higher score = closer + younger + better line.</p>
      <table><thead><tr><th>Player</th><th>Lvl</th><th>Pos</th><th style="text-align:right;">Age</th><th>Org</th><th>Line</th><th>Age/lvl</th><th style="text-align:right;">Recon</th></tr></thead>
        <tbody>${riserRows}</tbody></table>
    </div>` : "";
  return hotHtml + availSection + riserSection;
}

// Fuzzy name match against the dynasty board: prefix > substring > subsequence.
function fuzzyNameMatch(q, name) {
  q = q.toLowerCase(); const n = name.toLowerCase();
  if (n.startsWith(q)) return 1000 - n.length;
  const idx = n.indexOf(q);
  if (idx >= 0) return 600 - idx;
  let qi = 0;                              // subsequence (handles typos/initials)
  for (let i = 0; i < n.length && qi < q.length; i++) if (n[i] === q[qi]) qi++;
  return qi === q.length ? 200 - n.length : -1;
}

// Attach a lightweight fuzzy autocomplete to a one-name-per-line textarea so
// you don't have to type full names (and so they match the board exactly).
function attachNameAutocomplete(ta) {
  if (!ta || ta._acAttached) return;
  ta._acAttached = true;
  let box = null, items = [], active = -1, lineStart = 0, lineEnd = 0;
  const close = () => { if (box) { box.remove(); box = null; } items = []; active = -1; };
  const currentLine = () => {
    const pos = ta.selectionStart, val = ta.value;
    lineStart = val.lastIndexOf("\n", pos - 1) + 1;
    const le = val.indexOf("\n", pos);
    lineEnd = le === -1 ? val.length : le;
    return val.slice(lineStart, lineEnd).trim();
  };
  const choose = (name) => {
    const val = ta.value, before = val.slice(0, lineStart), after = val.slice(lineEnd);
    ta.value = before + name + after;
    const caret = (before + name).length;
    ta.focus(); ta.setSelectionRange(caret, caret);
    close();
  };
  const paint = () => box && box.querySelectorAll(".name-ac-item")
    .forEach((el, i) => el.classList.toggle("active", i === active));
  const render = (q) => {
    const names = dynastyCache || [];
    if (q.length < 2 || !names.length) { close(); return; }
    const scored = [];
    for (const v of names) { const s = fuzzyNameMatch(q, v.name); if (s > 0) scored.push([s, v]); }
    scored.sort((a, b) => b[0] - a[0]);
    items = scored.slice(0, 8).map(x => x[1]);
    if (!items.length) { close(); return; }
    if (!box) { box = document.createElement("div"); box.className = "name-ac"; document.body.appendChild(box); }
    const rect = ta.getBoundingClientRect();
    box.style.left = rect.left + "px";
    box.style.top = (rect.bottom + 2) + "px";
    box.style.minWidth = rect.width + "px";
    active = 0;
    box.innerHTML = items.map((v, i) =>
      `<div class="name-ac-item ${i === 0 ? "active" : ""}" data-i="${i}"><span>${escapeAttr(v.name)}</span><span class="meta">${escapeAttr(v.pos || "")} · #${v.our_rank}</span></div>`).join("");
    box.querySelectorAll(".name-ac-item").forEach(el =>
      el.addEventListener("mousedown", (e) => { e.preventDefault(); choose(items[+el.dataset.i].name); }));
  };
  ta.addEventListener("input", () => render(currentLine()));
  ta.addEventListener("focus", () => { if (!dynastyCache) loadDynasty().catch(() => {}); });
  ta.addEventListener("keydown", (e) => {
    if (!box || !items.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); active = (active + 1) % items.length; paint(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = (active - 1 + items.length) % items.length; paint(); }
    else if (e.key === "Enter") { e.preventDefault(); choose(items[active].name); }
    else if (e.key === "Escape") { close(); }
  });
  ta.addEventListener("blur", () => setTimeout(close, 150));
  window.addEventListener("scroll", close, true);
}

document.addEventListener("DOMContentLoaded", () => {
  $("#dyn-refresh")?.addEventListener("click", () => loadDynasty(true));
  $("#dyn-search")?.addEventListener("input", () => dynastyCache && renderDynasty());
  $("#dyn-trade-btn")?.addEventListener("click", evaluateDynastyTrade);
  $("#dyn-pickups-btn")?.addEventListener("click", loadDynastyPickups);
  attachNameAutocomplete($("#dyn-side-a"));
  attachNameAutocomplete($("#dyn-side-b"));
  const savedLg = localStorage.getItem("mlb_dfs_ftx_league");
  if (savedLg && $("#dyn-league")) $("#dyn-league").value = savedLg;
});

// ---------- Hall of Fame ----------
// All-time records, per-drafter aggregates, head-to-head, season titles.
// Loads on first entry to the tab; refresh button bursts the local cache.
let hofCache = { season: null, data: null };

async function loadHallOfFame(forceSeason = undefined) {
  const out = $("#hof-out");
  if (!out) return;
  const sel = $("#hof-season-filter");
  const seasonRaw = forceSeason !== undefined ? forceSeason : (sel ? sel.value : "");
  const season = seasonRaw ? parseInt(seasonRaw, 10) : null;
  if (hofCache.season === seasonRaw && hofCache.data && forceSeason === undefined) {
    renderHallOfFame(hofCache.data, season);
    return;
  }
  out.innerHTML = '<div class="muted">Loading…</div>';
  try {
    const url = season ? `/api/records?season=${season}&top_n=10` : `/api/records?top_n=10`;
    const data = await api(url);
    hofCache = { season: seasonRaw, data };
    // Populate season filter dropdown on first load
    if (sel && sel.options.length <= 1 && data.seasons) {
      sel.innerHTML = '<option value="">All seasons</option>' +
        data.seasons.map(s => `<option value="${s}">${s}</option>`).join("");
    }
    renderHallOfFame(data, season);
  } catch (e) {
    out.innerHTML = `<div class="muted">Failed: ${e.message || e}</div>`;
  }
}

function renderHallOfFame(d, season) {
  const out = $("#hof-out");
  if (!out) return;
  const parts = [];

  // 1. Record board — MLB-style headline records
  if (d.league_records && d.league_records.length) {
    parts.push(`<div class="hof-record-board">
      <h3>📜 The Record Book ${season ? `<span class="muted">${season}</span>` : `<span class="muted">all seasons</span>`}</h3>
      <table class="hof-table">
        <thead><tr><th>Stat</th><th>Name</th><th class="value">Value</th><th>Date</th><th>Notes</th></tr></thead>
        <tbody>
          ${d.league_records.map(r => `
            <tr>
              <td><b>${r.stat}</b></td>
              <td class="drafter-cell">${r.name}</td>
              <td class="value">${typeof r.value === "number" ? r.value.toFixed(2).replace(/\.00$/, "") : r.value}</td>
              <td class="date">${r.date}</td>
              <td class="extra">${r.extra || ""}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>`);
  }

  // 2. Drafter all-time + Season titles, side by side
  const drafterTable = !d.drafter_alltime || !d.drafter_alltime.length ? `<div class="hof-empty">No drafter data yet.</div>` :
    `<table class="hof-table">
      <thead><tr><th>#</th><th>Drafter</th><th class="value">Wins</th><th>Days</th><th class="value">Win %</th><th class="value">Total Pts</th><th class="value">Avg/day</th><th>Best Day</th><th>Streak</th></tr></thead>
      <tbody>${d.drafter_alltime.map((r, i) => `
        <tr>
          <td class="rank">${i+1}</td><td class="drafter-cell">${r.drafter}</td>
          <td class="value">${r.wins}</td><td>${r.days_played}</td>
          <td class="value">${(r.win_pct*100).toFixed(1)}%</td>
          <td class="value">${r.total_points.toFixed(1)}</td>
          <td class="value">${r.avg_points.toFixed(1)}</td>
          <td>${r.best_day ? `${r.best_day.total.toFixed(1)} <span class="date">(${r.best_day.date})</span>` : "—"}</td>
          <td class="value">${r.longest_win_streak}</td>
        </tr>`).join("")}</tbody></table>`;

  const titlesTable = !d.season_titles || !d.season_titles.length ? `<div class="hof-empty">No champions yet.</div>` :
    `<table class="hof-table">
      <thead><tr><th>Season</th><th>Champion</th><th class="value">Total Pts</th><th>Runner-up</th></tr></thead>
      <tbody>${d.season_titles.map(t => `
        <tr>
          <td><b>${t.season}</b></td>
          <td class="drafter-cell">🏆 ${t.champion}</td>
          <td class="value">${t.winner_total.toFixed(1)}</td>
          <td>${t.standings[1] ? `${t.standings[1].drafter} (${t.standings[1].total.toFixed(1)})` : "—"}</td>
        </tr>`).join("")}</tbody></table>`;

  parts.push(`<div class="hof-section">
    <h3>👑 Drafter All-Time</h3>
    ${drafterTable}
  </div>`);

  parts.push(`<div class="hof-section">
    <h3>🥇 Season Champions</h3>
    ${titlesTable}
  </div>`);

  // 3. Head-to-head
  const h2h = d.head_to_head || {};
  const drafters = Object.keys(h2h).sort();
  if (drafters.length >= 2) {
    parts.push(`<div class="hof-section">
      <h3>🥊 Head-to-Head <span class="muted">wins-losses-ties on shared days</span></h3>
      <table class="hof-table hof-h2h">
        <thead><tr><th></th>${drafters.map(d => `<th>vs ${d}</th>`).join("")}</tr></thead>
        <tbody>
          ${drafters.map(a => `<tr>
            <td class="drafter-cell">${a}</td>
            ${drafters.map(b => {
              if (a === b) return `<td class="muted">—</td>`;
              const r = h2h[a][b] || {wins:0,losses:0,ties:0};
              return `<td class="h2h-cell"><span class="w">${r.wins}</span>-<span class="l">${r.losses}</span>${r.ties ? `-${r.ties}` : ""}</td>`;
            }).join("")}
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`);
  }

  // 4. Top single-game records (hitter / pitcher side by side)
  parts.push(`<div class="hof-grid">
    <div class="hof-section">
      <h3>💣 Best Hitter Games</h3>
      ${_hofGameTable(d.top_hitter_games)}
    </div>
    <div class="hof-section">
      <h3>🔥 Best Pitcher Games</h3>
      ${_hofGameTable(d.top_pitcher_games)}
    </div>
  </div>`);

  // 5. Biggest blowouts + slate totals
  parts.push(`<div class="hof-grid">
    <div class="hof-section">
      <h3>💥 Biggest Blowouts <span class="muted">largest 1-day margins</span></h3>
      ${!d.biggest_blowouts || !d.biggest_blowouts.length ? '<div class="hof-empty">—</div>' :
        `<table class="hof-table">
          <thead><tr><th>#</th><th>Winner</th><th class="value">Margin</th><th>Score</th><th>Date</th></tr></thead>
          <tbody>${d.biggest_blowouts.map(r => `
            <tr><td class="rank">${r.rank}</td><td class="drafter-cell">${r.winner} over ${r.runnerup}</td>
            <td class="value">${r.margin.toFixed(1)}</td>
            <td>${r.winner_total.toFixed(1)} – ${r.runnerup_total.toFixed(1)}</td>
            <td class="date">${r.date}</td></tr>`).join("")}</tbody>
        </table>`}
    </div>
    <div class="hof-section">
      <h3>📈 Highest Team Days <span class="muted">single-day team total</span></h3>
      ${!d.highest_team_totals || !d.highest_team_totals.length ? '<div class="hof-empty">—</div>' :
        `<table class="hof-table">
          <thead><tr><th>#</th><th>Drafter</th><th class="value">Total</th><th>Date</th></tr></thead>
          <tbody>${d.highest_team_totals.map(r => `
            <tr><td class="rank">${r.rank}</td><td class="drafter-cell">${r.drafter}</td>
            <td class="value">${r.total.toFixed(1)}</td><td class="date">${r.date}</td></tr>`).join("")}</tbody>
        </table>`}
    </div>
  </div>`);

  // 6. Most picked + worst picks
  parts.push(`<div class="hof-grid">
    <div class="hof-section">
      <h3>📌 Most-Picked Hitters</h3>
      ${_hofMostPickedTable(d.most_picked_hitters)}
    </div>
    <div class="hof-section">
      <h3>📌 Most-Picked Pitchers</h3>
      ${_hofMostPickedTable(d.most_picked_pitchers)}
    </div>
  </div>`);

  parts.push(`<div class="hof-section">
    <h3>💀 Worst Single Picks <span class="muted">the bad beats</span></h3>
    ${!d.worst_picks || !d.worst_picks.length ? '<div class="hof-empty">—</div>' :
      `<table class="hof-table">
        <thead><tr><th>#</th><th>Player</th><th class="value">Score</th><th>Drafter</th><th>Role</th><th>Date</th></tr></thead>
        <tbody>${d.worst_picks.map(r => `
          <tr><td class="rank">${r.rank}</td><td><b>${r.player}</b></td>
          <td class="value" style="color:var(--bad);">${r.score.toFixed(2)}</td>
          <td class="drafter-cell">${r.drafter}</td><td class="muted">${r.role}</td>
          <td class="date">${r.date}</td></tr>`).join("")}</tbody>
      </table>`}
  </div>`);

  out.innerHTML = parts.join("");
}

function _hofGameTable(rows) {
  if (!rows || !rows.length) return '<div class="hof-empty">No games on record yet.</div>';
  return `<table class="hof-table">
    <thead><tr><th>#</th><th>Player</th><th class="value">Score</th><th>Drafter</th><th>Date</th></tr></thead>
    <tbody>${rows.map(r => `
      <tr><td class="rank">${r.rank}</td><td><b>${r.player}</b></td>
      <td class="value">${r.score.toFixed(2)}</td>
      <td class="drafter-cell">${r.drafter}</td>
      <td class="date">${r.date}</td></tr>`).join("")}</tbody>
  </table>`;
}

function _hofMostPickedTable(rows) {
  if (!rows || !rows.length) return '<div class="hof-empty">No data yet.</div>';
  return `<table class="hof-table">
    <thead><tr><th>#</th><th>Player</th><th class="value">Times</th><th class="value">Avg</th><th class="value">Best</th></tr></thead>
    <tbody>${rows.slice(0, 12).map((r, i) => `
      <tr><td class="rank">${i+1}</td><td><b>${r.player}</b></td>
      <td class="value">${r.times_picked}</td>
      <td class="value">${r.avg_score.toFixed(1)}</td>
      <td class="value">${r.best_score.toFixed(1)}</td></tr>`).join("")}</tbody>
  </table>`;
}

$("#hof-refresh")?.addEventListener("click", () => loadHallOfFame());
$("#hof-season-filter")?.addEventListener("change", () => loadHallOfFame());

// ---------- Ask the Algo ----------
// Paste any list of player names, get ranked projections + matchup context.

async function runAskAlgo() {
  const namesRaw = ($("#ask-names")?.value || "").trim();
  const dateInput = $("#ask-date")?.value;
  const out = $("#ask-out");
  if (!out) return;
  const names = namesRaw.split(/\n+/).map(s => s.trim()).filter(Boolean);
  if (!names.length) {
    out.innerHTML = '<div class="muted">Paste at least one name above.</div>';
    return;
  }
  out.innerHTML = '<div class="muted">Running through the algo…</div>';
  try {
    const body = { names };
    if (dateInput) body.date = dateInput;
    const r = await api("/api/ask_algo", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderAskAlgo(r);
  } catch (e) {
    out.innerHTML = `<div class="muted">Failed: ${e.message || e}</div>`;
  }
}

function _askRecommend(role, proj) {
  // Simple heuristic: well-defined buckets that match the existing form/QoC
  // tier thinking. Hitters peak ~25 pts, pitchers ~25 pts; bottom is 0/negative.
  if (proj == null) return ["OFF", "No game today"];
  if (role === "pitcher") {
    if (proj >= 16) return ["START", "Strong play"];
    if (proj >= 11) return ["CONSIDER", "Acceptable arm"];
    return ["SIT", "Risky — low projection"];
  }
  if (proj >= 12) return ["START", "Strong play"];
  if (proj >= 8) return ["CONSIDER", "Mid-tier"];
  return ["SIT", "Low projection"];
}

function renderAskAlgo(r) {
  const out = $("#ask-out");
  if (!out) return;
  const matched = r.matched || [];
  const missing = r.missing || [];
  if (!matched.length && !missing.length) {
    out.innerHTML = '<div class="muted">No matches found.</div>';
    return;
  }
  const rows = matched.map((p, i) => {
    const c = p.components || {};
    const role = p.role || "";
    const proj = p.projected_points;
    const [rec, recHint] = _askRecommend(role, proj);
    // Context fields differ by role
    let context = "";
    if (role === "pitcher") {
      const bits = [];
      if (c.form_tag) bits.push(c.form_tag);
      if (c.qoc_tier && c.qoc_tier !== "—") bits.push(c.qoc_tier);
      if (c.ip_per_start != null) bits.push(`${c.ip_per_start} IP/start`);
      if (c.k9_season != null) bits.push(`${c.k9_season.toFixed(1)} K/9`);
      if (c.is_opener) bits.push("OPENER");
      if (c.opp_abbr) bits.push(`@${c.opp_abbr}`);
      context = bits.join(" · ");
    } else {
      const bits = [];
      if (c.form_tag) bits.push(c.form_tag);
      if (c.qoc_tier && c.qoc_tier !== "—") bits.push(c.qoc_tier);
      if (c.batting_order) bits.push(`#${c.batting_order}`);
      if (c.implied_team_total) bits.push(`${c.implied_team_total.toFixed(1)}R team`);
      if (c.opp_abbr && c.opp_sp_name) bits.push(`vs ${c.opp_sp_name}`);
      context = bits.join(" · ");
    }
    // Use the same .player-cell + .name-trigger pattern the rest of the app
    // uses so the existing CSS (`.name-trigger:hover ~ .breakdown-tooltip`)
    // and mobile tap-to-toggle handler both apply. Hovering the projection
    // number shows the full multi-row breakdown (factors + pitfalls + range).
    const tooltipHTML = projTooltip({
      name: p.name,
      projected_points: proj,
      role,
      components: c,
      cat_proj: p.cat_proj,
    });
    const projCell = proj != null ?
      `<td class="proj player-cell"><span class="name-trigger" style="cursor:help;">${proj.toFixed(2)}</span>${tooltipHTML}</td>` :
      `<td class="proj">—</td>`;
    return `<tr>
      <td class="rank">${i+1}</td>
      <td class="player">${p.name}<div class="meta">${role || ""} ${p.position ? "· " + p.position : ""}</div></td>
      <td class="meta">${context || "—"}</td>
      <td><span class="ask-rec-${rec}" title="${recHint}">${rec}</span></td>
      ${projCell}
    </tr>`;
  }).join("");
  let html = `<div class="ask-results">
    <table class="ask-table">
      <thead><tr><th>#</th><th>Player</th><th>Context</th><th>Reco</th><th class="proj">Proj</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
  if (missing.length) {
    html += `<div class="ask-missing">
      <b>Not in today's slate:</b> ${missing.join(", ")}
      <div class="muted" style="font-size:11px;margin-top:4px;">
        These players either aren't probable today, are on IL, or are bullpen arms (RPs don't get daily projections — try the K Props tab or check their next start).
      </div>
    </div>`;
  }
  out.innerHTML = html;
}

$("#ask-go")?.addEventListener("click", runAskAlgo);
$("#ask-names")?.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.key === "Enter") runAskAlgo();
});

// Viewport-aware tooltip positioner.
// Used by both the Ask Algo tab (.ask-table) and the Live Score tab
// (.score-row) — anywhere the breakdown tooltip's default position:absolute
// puts it off-screen or in a weird spot relative to the trigger. Computes
// the trigger's bounding rect on mouseenter and sets --tt-top / --tt-left
// CSS vars; the matching CSS rule swaps position:fixed in for those cells.
document.addEventListener("mouseenter", (e) => {
  const tt = e.target && e.target.closest && e.target.closest(
    ".ask-table .player-cell .name-trigger, .score-row .player-cell .name-trigger, #lineup-out .player-cell .name-trigger"
  );
  if (!tt) return;
  const cell = tt.closest(".player-cell");
  const tooltip = cell && cell.querySelector(".breakdown-tooltip");
  if (!tooltip) return;
  const rect = tt.getBoundingClientRect();
  // Measure tooltip's natural size by temporarily showing it offscreen.
  tooltip.style.visibility = "hidden";
  tooltip.style.display = "block";
  const ttRect = tooltip.getBoundingClientRect();
  tooltip.style.display = "";
  tooltip.style.visibility = "";
  const vh = window.innerHeight;
  const vw = window.innerWidth;
  const margin = 8;
  // Prefer below the trigger; flip above if there's not enough room
  let top = rect.bottom + 4;
  if (top + ttRect.height > vh - margin) {
    top = Math.max(margin, rect.top - ttRect.height - 4);
  }
  // Anchor right edge of tooltip to right edge of trigger (right-aligned cell)
  let left = rect.right - ttRect.width;
  if (left < margin) left = margin;
  if (left + ttRect.width > vw - margin) left = vw - margin - ttRect.width;
  tooltip.style.setProperty("--tt-top", top + "px");
  tooltip.style.setProperty("--tt-left", left + "px");
}, true);

// Default date to today
(function initAskDate() {
  const el = $("#ask-date");
  if (el && !el.value) el.value = new Date().toISOString().slice(0, 10);
})();

// League baselines footer — surfaces the live Statcast averages the algo
// uses, so it's visible at a glance whether they're current. 24h auto-refresh
// on the server; click "refresh" to force a fresh pull from Statcast.
async function loadLeagueBaselines(force = false) {
  const el = $("#lg-baselines");
  if (!el) return;
  try {
    const r = await api(`/api/league_averages${force ? "?refresh=true" : ""}`);
    const a = r.averages || {};
    const ageH = r.age_seconds != null ? (r.age_seconds / 3600).toFixed(1) : "?";
    const fresh = r.age_seconds != null && r.age_seconds < 24 * 3600;
    const cls = fresh ? "fresh" : "stale";
    el.innerHTML = `
      <b>League baselines (${r.season})</b> · live from Statcast,
      <span class="${cls}">${ageH}h old</span>
      (<a id="lg-refresh">refresh</a>) ·
      <b>hitter</b> brl ${a.brl_pct_hitter?.toFixed(2)}% · hh ${a.hh_pct_hitter?.toFixed(2)}% ·
      xwOBA ${a.xwoba_hitter?.toFixed(3)} ·
      <b>pitcher allowed</b> brl ${a.brl_pct_allowed?.toFixed(2)}% · hh ${a.hh_pct_allowed?.toFixed(2)}% ·
      xERA ${a.xera?.toFixed(2)} · xwOBA-agst ${a.xwoba_against?.toFixed(3)}
    `;
    $("#lg-refresh")?.addEventListener("click", (e) => {
      e.preventDefault();
      loadLeagueBaselines(true);
    });
  } catch (e) {
    el.innerHTML = `<span class="stale">League baselines unavailable: ${e.message || e}</span>`;
  }
}
window.addEventListener("DOMContentLoaded", () => loadLeagueBaselines().catch(() => {}));

// Decide the action label given current Fantrax slot vs recommendation.
// Returns {label, cls, action} where action ∈ KEEP / PROMOTE / BENCH / SIT / OFF.
const _BENCH_SLOTS = new Set(["BN", "Res", "Reserve", "IR", "InjRes", "Inj Res", ""]);
function _actionLabel(r) {
  // Action-forward labels: tell the user what to do, don't rely on current_slot.
  // Fantrax's roster API returns the period-default lineup, not today's active
  // slots, so KEEP/MOVE comparisons against current_slot were misleading. We
  // only show "(currently X)" as a subtle hint — the action itself stands alone.
  const cur = r.current_slot ?? null;
  const isStart = r.recommendation === "START";
  const slotPart = r.slot_assignment ? ` <span class="muted" style="font-weight:400;">(${r.slot_assignment})</span>` : "";
  const scratchTag = r.scratched ? ` <span class="muted" style="font-weight:400;font-size:10px;">(scratched)</span>` : "";
  // Show current slot as a subtle hint when meaningfully different. Force-bench
  // and force-minors overrides set cur explicitly so we trust those.
  const curHint = cur ? ` <span class="muted" style="font-weight:400;font-size:10px;">cur:${cur}</span>` : "";
  if (r.recommendation === "OFF") return { label: "OFF", cls: "muted" };
  if (isStart) {
    return { label: `START${slotPart}${curHint}`, cls: "edge-pos" };
  }
  // Recommendation is BN / SIT
  return { label: `BENCH${scratchTag}${curHint}`, cls: "edge-neg" };
}

// Per-cat z-score breakdown (text, used in Cat val cell title= tooltip).
const _LG_HITTER = { R: [0.56, 0.18], HR: [0.14, 0.07], RBI: [0.55, 0.20], SB: [0.06, 0.10], OPS: [0.730, 0.080] };
const _LG_PITCHER = { QS: [0.45, 0.20], K: [5.5, 1.5], ERA: [4.20, 0.80], WHIP: [1.30, 0.13], SVH: [0.0, 0.30] };
function _catValBreakdown(r, leverage) {
  const cp = r.cat_proj || {};
  const isHitter = r.role === "hitter";
  const lg = isHitter ? _LG_HITTER : _LG_PITCHER;
  const lines = [];
  for (const [k, [mean, stdev]] of Object.entries(lg)) {
    if (cp[k] == null) continue;
    let z = (cp[k] - mean) / stdev;
    if (k === "ERA" || k === "WHIP") z = -z;   // lower is better
    const lev = leverage[k] ?? 1.0;
    const contrib = z * lev;
    const sign = contrib >= 0 ? "+" : "";
    lines.push(`${k}: ${sign}${contrib.toFixed(2)} (z=${z.toFixed(2)} × lev ${lev.toFixed(1)})`);
  }
  return `Per-cat contributions to Cat val (z-score × leverage):\n` + lines.join("\n");
}

// Helper: render the opponent line for a lineup row.
function _oppCell(r) {
  if (!r.opp_abbr) return "";
  const prefix = r.is_home === false ? "@" : "vs ";
  if (r.role === "hitter") {
    const sp = r.opp_sp_name ? ` <span class="muted" style="font-size:10px;">vs ${r.opp_sp_name}</span>` : "";
    return `<br><span class="muted" style="font-size:11px;">${prefix}${r.opp_abbr}${sp}</span>`;
  }
  const c = r.components || {};
  const oppRuns = c.opp_implied_total ? ` <span class="muted" style="font-size:10px;">${c.opp_implied_total.toFixed(1)} R impl</span>` : "";
  return `<br><span class="muted" style="font-size:11px;">${prefix}${r.opp_abbr}${oppRuns}</span>`;
}

// Lineup-tab player cell with the same hover breakdown the Ask Algo tab uses —
// name-trigger wraps just the name so hovering it pops the factor breakdown
// (positioned by the shared viewport-aware positioner; #lineup-out is in its
// selector list + CSS). Used by both lineup render paths.
function _lineupPlayerCell(r) {
  const matched = r.matched_name && r.matched_name !== r.input
    ? ` <span class="muted">(${r.matched_name})</span>` : "";
  const tt = projTooltip({
    name: r.matched_name || r.input,
    projected_points: r.projection,
    role: r.role,
    components: r.components,
    cat_proj: r.cat_proj,
  });
  return `<td class="player-cell"><span class="name-trigger" style="cursor:help;">${r.input}${matched}</span>${_oppCell(r)}${tt}</td>`;
}

// Same idea but takes a Projection-style object (components.opp_abbr / opp_sp_name / is_home).
function _oppFromComponents(p) {
  const c = p.components || {};
  if (!c.opp_abbr) return "";
  const prefix = c.is_home === false ? "@" : "vs ";
  if (p.role === "hitter") {
    const sp = c.opp_sp_name ? `, ${c.opp_sp_name}` : "";
    return `<div class="opp-line muted">${prefix}${c.opp_abbr}${sp}</div>`;
  }
  const oppRuns = c.opp_implied_total ? `, ${c.opp_implied_total.toFixed(1)} R` : "";
  return `<div class="opp-line muted">${prefix}${c.opp_abbr}${oppRuns}</div>`;
}

// Cached Fantrax payload from last Pull (so /api/lineup gets position eligibility).
let _lastFantraxPlayers = null;
let _lastFantraxSlotCounts = null;
let _lineupSort = { hitter: "cat_value", pitcher: "cat_value" };

$("#lineup-go")?.addEventListener("click", async () => {
  const names = ($("#lineup-names").value || "").split("\n").map(s => s.trim()).filter(Boolean);
  if (!names.length) return alert("Paste at least one player name.");
  localStorage.setItem("mlb_dfs_lineup_names", $("#lineup-names").value);
  $("#lineup-out").innerHTML = `<div class="muted">Projecting ${names.length} players…</div>`;
  const body = { date: $("#date").value, names };
  // If we have league_id + team_id + cached Fantrax players, send them so the
  // backend can do slot-aware optimization with real position eligibility.
  const lg = ($("#ftx-league")?.value || "").trim();
  const tm = ($("#ftx-team")?.value || "").trim();
  if (lg) body.league_id = lg;
  if (tm) body.team_id = tm;
  if (_lastFantraxPlayers) body.fantrax_players = _lastFantraxPlayers;
  if (_lastFantraxSlotCounts) body.fantrax_slot_counts = _lastFantraxSlotCounts;
  body.allow_call_ups = !!$("#lineup-allow-callups")?.checked;
  const forceMinRaw = ($("#lineup-force-minors")?.value || "").trim();
  if (forceMinRaw) {
    body.force_minors = forceMinRaw.split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
    localStorage.setItem("mlb_dfs_force_minors", forceMinRaw);
  } else {
    localStorage.removeItem("mlb_dfs_force_minors");
  }
  const forceBenchRaw = ($("#lineup-force-bench")?.value || "").trim();
  if (forceBenchRaw) {
    body.force_bench = forceBenchRaw.split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
    localStorage.setItem("mlb_dfs_force_bench", forceBenchRaw);
  } else {
    localStorage.removeItem("mlb_dfs_force_bench");
  }
  const data = await api(`/api/lineup`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  const renderTable = (title, rows, emptyMsg, sortKey = "cat_value") => {
    if (!rows.length) return `<h3>${title}</h3><div class="muted">${emptyMsg}</div>`;
    const isHitter = rows[0]?.role === "hitter";
    // Apply sort
    const SLOT_ORDER = { C: 0, "1B": 1, "2B": 2, SS: 3, "3B": 4, MI: 5, CI: 6, OF: 7, UT: 8, SP: 0, RP: 1, P: 2, BN: 99 };
    rows = [...rows].sort((a, b) => {
      if (sortKey === "fp") return (b.projection || 0) - (a.projection || 0);
      if (sortKey === "slot") {
        const av = SLOT_ORDER[a.slot_assignment ?? "BN"] ?? 99;
        const bv = SLOT_ORDER[b.slot_assignment ?? "BN"] ?? 99;
        if (av !== bv) return av - bv;
        return (b.cat_value || 0) - (a.cat_value || 0);
      }
      return (b.cat_value || 0) - (a.cat_value || 0);
    });
    const trs = rows.map(r => {
      const action = _actionLabel(r);
      const rpTag = r.is_rp ? ` <span class="bench-tag" style="background:rgba(148,163,184,0.25);font-size:9px;">RP?</span>` : "";
      const recCls = action.cls;
      const recLabel = `${action.label}${rpTag}`;
      const cp = r.cat_proj || {};
      const catCols = isHitter
        ? `<td>${(cp.R ?? 0).toFixed(2)}</td><td>${(cp.HR ?? 0).toFixed(2)}</td><td>${(cp.RBI ?? 0).toFixed(2)}</td><td>${(cp.SB ?? 0).toFixed(2)}</td><td>${((cp.OPS ?? 0)).toFixed(3)}</td>`
        : `<td>${(cp.QS ?? 0).toFixed(2)}</td><td>${(cp.K ?? 0).toFixed(1)}</td><td>${(cp.ERA ?? 0).toFixed(2)}</td><td>${(cp.WHIP ?? 0).toFixed(2)}</td><td>${(cp.SVH ?? 0).toFixed(2)}</td>`;
      const cvCls = r.cat_value > 1.0 ? "edge-pos" : r.cat_value < -1.0 ? "edge-neg" : "";
      const cvSign = r.cat_value >= 0 ? "+" : "";
      const cvTitle = _catValBreakdown(r, data?.leverage || {});
      return `<tr>
        <td class="${recCls}"><b>${recLabel}</b></td>
        ${_lineupPlayerCell(r)}
        <td>${r.position ?? "—"}</td>
        <td class="${cvCls}" title="${cvTitle}" style="cursor:help;"><b>${cvSign}${r.cat_value.toFixed(2)}</b></td>
        ${catCols}
        <td class="muted" style="font-size:11px;">${r.projection.toFixed(2)}</td>
      </tr>`;
    }).join("");
    const headerCats = isHitter
      ? `<th>R</th><th>HR</th><th>RBI</th><th>SB</th><th>OPS</th>`
      : `<th>QS</th><th>K</th><th>ERA</th><th>WHIP</th>`;
    const tableId = isHitter ? "lineup-hitters" : "lineup-pitchers";
    return `<h3>${title}
      <select class="lineup-sort" data-target="${tableId}" style="margin-left:8px;font-size:12px;">
        <option value="cat_value" ${sortKey === "cat_value" ? "selected" : ""}>Sort: Cat val</option>
        <option value="fp" ${sortKey === "fp" ? "selected" : ""}>Sort: FP</option>
        <option value="slot" ${sortKey === "slot" ? "selected" : ""}>Sort: Slot</option>
      </select></h3>
      <table id="${tableId}"><thead><tr><th>Rec</th><th>Player</th><th>Pos</th><th>Cat val</th>${headerCats}<th>FP</th></tr></thead>
      <tbody>${trs}</tbody></table>`;
  };
  let html = "";
  if (data.slot_capacity && Object.keys(data.slot_capacity).length) {
    const sc = data.slot_capacity;
    const summary = Object.entries(sc).map(([k, v]) => `${v}×${k}`).join(" · ");
    html += `<div class="muted" style="font-size:11px;margin:4px 0 6px;">Slots used for assignment: ${summary}</div>`;
  }
  // Current weekly matchup state + leverage display.
  if (data.matchup && data.matchup.values) {
    const m = data.matchup;
    const cats = m.category_short_names || [];
    const lev = data.leverage || {};
    const cells = cats.map(c => {
      const [my, opp] = m.values[c] || [0, 0];
      const l = lev[c] ?? 1.0;
      const lvCls = l > 1.2 ? "edge-pos" : l < 0.8 ? "muted" : "";
      const cmp = my > opp ? "edge-pos" : my < opp ? "edge-neg" : "muted";
      const lower_better = (c === "ERA" || c === "WHIP");
      const winning = lower_better ? my < opp : my > opp;
      const cmp2 = my === opp ? "muted" : (winning ? "edge-pos" : "edge-neg");
      const fmt = (v) => Number.isInteger(v) ? v : v.toFixed(c === "OPS" ? 3 : 2);
      return `<td><b>${c}</b><br><span class="${cmp2}">${fmt(my)} vs ${fmt(opp)}</span><br><span class="${lvCls}" style="font-size:10px;">×${l.toFixed(1)} lev</span></td>`;
    }).join("");
    html += `<div style="margin:6px 0 14px;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px;">
      <h3 style="margin:0 0 4px;">${m.period} <span class="muted" style="font-weight:400;font-size:12px;">${m.subCaption || ""}</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:6px;">${m.my_team || "you"} vs <b>${m.opp_team || "opp"}</b></div>
      <table style="font-size:12px;width:100%;text-align:center;"><tbody><tr>${cells}</tr></tbody></table>
      <div class="muted" style="font-size:11px;margin-top:6px;">Leverage: 1.5× = close cat, every contribution matters; 0.5× = essentially decided.</div>
    </div>`;
  }
  // Stash data for re-sort.
  window._lastLineupData = data;
  html += renderTable("Hitters", data.hitters, "No hitters matched.", _lineupSort.hitter);
  html += renderTable("Pitchers", data.pitchers, "No pitchers matched.", _lineupSort.pitcher);
  if (data.unmatched.length) {
    const list = data.unmatched.map(r => r.input).join(", ");
    html += `<details style="margin-top:12px;"><summary class="muted" style="cursor:pointer;font-size:13px;">Not playing today / unmatched (${data.unmatched.length}) — mostly minor leaguers</summary><div class="muted" style="font-size:12px;margin-top:4px;">${list}</div></details>`;
  }
  $("#lineup-out").innerHTML = html;
  // Wire sort dropdowns to re-render in place.
  document.querySelectorAll(".lineup-sort").forEach((sel) => {
    sel.addEventListener("change", () => {
      const tgt = sel.dataset.target;
      if (tgt === "lineup-hitters") _lineupSort.hitter = sel.value;
      else _lineupSort.pitcher = sel.value;
      _renderLineupOutput(window._lastLineupData);
    });
  });
});

function _renderLineupOutput(data) {
  // Re-trigger the same render path. Lift the closure logic up.
  // Simplest: call the click handler's body by reusing the data.
  const out = $("#lineup-out");
  const renderTable = (title, rows, emptyMsg, sortKey = "cat_value") => {
    if (!rows.length) return `<h3>${title}</h3><div class="muted">${emptyMsg}</div>`;
    const isHitter = rows[0]?.role === "hitter";
    const SLOT_ORDER = { C: 0, "1B": 1, "2B": 2, SS: 3, "3B": 4, MI: 5, CI: 6, OF: 7, UT: 8, SP: 0, RP: 1, P: 2, BN: 99 };
    rows = [...rows].sort((a, b) => {
      if (sortKey === "fp") return (b.projection || 0) - (a.projection || 0);
      if (sortKey === "slot") {
        const av = SLOT_ORDER[a.slot_assignment ?? "BN"] ?? 99;
        const bv = SLOT_ORDER[b.slot_assignment ?? "BN"] ?? 99;
        if (av !== bv) return av - bv;
        return (b.cat_value || 0) - (a.cat_value || 0);
      }
      return (b.cat_value || 0) - (a.cat_value || 0);
    });
    const trs = rows.map(r => {
      const action = _actionLabel(r);
      const rpTag = r.is_rp ? ` <span class="bench-tag" style="background:rgba(148,163,184,0.25);font-size:9px;">RP?</span>` : "";
      const recCls = action.cls;
      const recLabel = `${action.label}${rpTag}`;
      const cp = r.cat_proj || {};
      const catCols = isHitter
        ? `<td>${(cp.R ?? 0).toFixed(2)}</td><td>${(cp.HR ?? 0).toFixed(2)}</td><td>${(cp.RBI ?? 0).toFixed(2)}</td><td>${(cp.SB ?? 0).toFixed(2)}</td><td>${((cp.OPS ?? 0)).toFixed(3)}</td>`
        : `<td>${(cp.QS ?? 0).toFixed(2)}</td><td>${(cp.K ?? 0).toFixed(1)}</td><td>${(cp.ERA ?? 0).toFixed(2)}</td><td>${(cp.WHIP ?? 0).toFixed(2)}</td><td>${(cp.SVH ?? 0).toFixed(2)}</td>`;
      const cvCls = r.cat_value > 1.0 ? "edge-pos" : r.cat_value < -1.0 ? "edge-neg" : "";
      const cvSign = r.cat_value >= 0 ? "+" : "";
      const cvTitle = _catValBreakdown(r, data?.leverage || {});
      return `<tr><td class="${recCls}"><b>${recLabel}</b></td>${_lineupPlayerCell(r)}<td>${r.position ?? "—"}</td><td class="${cvCls}"><b>${cvSign}${r.cat_value.toFixed(2)}</b></td>${catCols}<td class="muted" style="font-size:11px;">${r.projection.toFixed(2)}</td></tr>`;
    }).join("");
    const headerCats = isHitter ? `<th>R</th><th>HR</th><th>RBI</th><th>SB</th><th>OPS</th>` : `<th>QS</th><th>K</th><th>ERA</th><th>WHIP</th><th>SVH</th>`;
    const tableId = isHitter ? "lineup-hitters" : "lineup-pitchers";
    return `<h3>${title}
      <select class="lineup-sort" data-target="${tableId}" style="margin-left:8px;font-size:12px;">
        <option value="cat_value" ${sortKey === "cat_value" ? "selected" : ""}>Sort: Cat val</option>
        <option value="fp" ${sortKey === "fp" ? "selected" : ""}>Sort: FP</option>
        <option value="slot" ${sortKey === "slot" ? "selected" : ""}>Sort: Slot</option>
      </select></h3>
      <table id="${tableId}"><thead><tr><th>Rec</th><th>Player</th><th>Pos</th><th title="Sum of z-scores across the 5 H2H categories — how much this player swings YOUR matchup vs an avg replacement, weighted by leverage on close cats.">Cat val ⓘ</th>${headerCats}<th>FP</th></tr></thead><tbody>${trs}</tbody></table>`;
  };
  let html = "";
  html += `<details style="margin:4px 0 8px;font-size:12px;">
    <summary class="muted" style="cursor:pointer;">ⓘ How "Cat val" is computed</summary>
    <div style="font-size:11.5px;margin:4px 0 0 14px;line-height:1.5;">
      <b>Cat val</b> = sum of z-scores across the 5 H2H categories (R/HR/RBI/SB/OPS for hitters; QS/K/ERA/WHIP/SVH for pitchers).<br>
      Each cat's z = <code>(player projection − league avg) / league stdev</code>. ERA/WHIP are inverted (lower = better).<br>
      Each z is then multiplied by <b>leverage</b>: 1.5× if your matchup is close in that cat, 1.0× competitive, 0.5× if essentially decided.<br>
      Hover over any Cat val cell to see the per-cat breakdown.
    </div>
  </details>`;
  if (data.slot_capacity && Object.keys(data.slot_capacity).length) {
    const sc = data.slot_capacity;
    html += `<div class="muted" style="font-size:11px;margin:4px 0 6px;">Slots: ${Object.entries(sc).map(([k,v]) => `${v}×${k}`).join(" · ")}</div>`;
  }
  if (data.matchup && data.matchup.values) {
    const m = data.matchup;
    const cats = m.category_short_names || [];
    const lev = data.leverage || {};
    const cells = cats.map(c => {
      const [my, opp] = m.values[c] || [0, 0];
      const l = lev[c] ?? 1.0;
      const lvCls = l > 1.2 ? "edge-pos" : l < 0.8 ? "muted" : "";
      const lower_better = (c === "ERA" || c === "WHIP");
      const winning = lower_better ? my < opp : my > opp;
      const cmp2 = my === opp ? "muted" : (winning ? "edge-pos" : "edge-neg");
      const fmt = (v) => Number.isInteger(v) ? v : v.toFixed(c === "OPS" ? 3 : 2);
      return `<td><b>${c}</b><br><span class="${cmp2}">${fmt(my)} vs ${fmt(opp)}</span><br><span class="${lvCls}" style="font-size:10px;">×${l.toFixed(1)} lev</span></td>`;
    }).join("");
    html += `<div style="margin:6px 0 14px;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px;"><h3 style="margin:0 0 4px;">${m.period} <span class="muted" style="font-weight:400;font-size:12px;">${m.subCaption || ""}</span></h3><div class="muted" style="font-size:12px;margin-bottom:6px;">${m.my_team || "you"} vs <b>${m.opp_team || "opp"}</b></div><table style="font-size:12px;width:100%;text-align:center;"><tbody><tr>${cells}</tr></tbody></table></div>`;
  }
  html += renderTable("Hitters", data.hitters, "No hitters matched.", _lineupSort.hitter);
  html += renderTable("Pitchers", data.pitchers, "No pitchers matched.", _lineupSort.pitcher);
  if (data.minors && data.minors.length) {
    const callup = data.minors_callup || [];
    const pure = data.minors_pure || data.minors;
    let body = "";
    if (callup.length) {
      body += `<div style="margin-top:6px;"><b style="color:var(--edge-pos,#0a7);font-size:12px;">MLB-active today — promote in Fantrax (${callup.length}):</b><div class="muted" style="font-size:12px;margin-top:2px;">${callup.join(", ")}</div></div>`;
    }
    if (pure.length) {
      body += `<div style="margin-top:6px;"><b class="muted" style="font-size:12px;">AAA / not on a slate today (${pure.length}):</b><div class="muted" style="font-size:12px;margin-top:2px;">${pure.join(", ")}</div></div>`;
    }
    if (!body) body = `<div class="muted" style="font-size:12px;margin-top:4px;">${data.minors.join(", ")}</div>`;
    html += `<details style="margin-top:12px;" ${callup.length ? "open" : ""}><summary class="muted" style="cursor:pointer;font-size:13px;">⚾ Minor-league slot in Fantrax — ${data.minors.length}${callup.length ? ` (${callup.length} actually MLB-active today)` : ""}. Toggle "Allow call-ups" to include them.</summary>${body}</details>`;
  }
  if (data.unmatched && data.unmatched.length) {
    const scratched = data.unmatched.filter(r => r.lineup_status === "out");
    const otherNot = data.unmatched.filter(r => r.lineup_status !== "out");
    let body = "";
    if (scratched.length) {
      body += `<div style="margin-top:4px;"><b style="color:var(--bad,#c33);font-size:12px;">Scratched / not in posted lineup (${scratched.length}):</b><div class="muted" style="font-size:12px;">${scratched.map(r => r.input).join(", ")}</div></div>`;
    }
    if (otherNot.length) {
      body += `<div style="margin-top:4px;"><span class="muted" style="font-size:12px;">Off / unmatched (${otherNot.length}):</span><div class="muted" style="font-size:12px;">${otherNot.map(r => r.input).join(", ")}</div></div>`;
    }
    html += `<details style="margin-top:12px;" ${scratched.length ? "open" : ""}><summary class="muted" style="cursor:pointer;font-size:13px;">Not playing today / unmatched (${data.unmatched.length})</summary>${body}</details>`;
  }
  out.innerHTML = html;
  document.querySelectorAll(".lineup-sort").forEach((sel) => {
    sel.addEventListener("change", () => {
      const tgt = sel.dataset.target;
      if (tgt === "lineup-hitters") _lineupSort.hitter = sel.value;
      else _lineupSort.pitcher = sel.value;
      _renderLineupOutput(window._lastLineupData);
    });
  });
}

// Restore last roster + Fantrax IDs
window.addEventListener("DOMContentLoaded", () => {
  const saved = localStorage.getItem("mlb_dfs_lineup_names");
  if (saved && $("#lineup-names")) $("#lineup-names").value = saved;
  const savedForceMin = localStorage.getItem("mlb_dfs_force_minors");
  if (savedForceMin && $("#lineup-force-minors")) $("#lineup-force-minors").value = savedForceMin;
  const savedForceBench = localStorage.getItem("mlb_dfs_force_bench");
  if (savedForceBench && $("#lineup-force-bench")) $("#lineup-force-bench").value = savedForceBench;
  const lg = localStorage.getItem("mlb_dfs_ftx_league");
  const tm = localStorage.getItem("mlb_dfs_ftx_team");
  if (lg && $("#ftx-league")) $("#ftx-league").value = lg;
  if (tm && $("#ftx-team")) $("#ftx-team").value = tm;
});

$("#ftx-league-info")?.addEventListener("click", async () => {
  const lg = $("#ftx-league").value.trim();
  if (!lg) return alert("Enter your league_id first.");
  $("#ftx-status").textContent = "Fetching league config…";
  $("#ftx-status").style.color = "";
  try {
    const data = await api(`/api/fantrax/league_info?league_id=${encodeURIComponent(lg)}`);
    const out = $("#lineup-out");
    const probesHtml = Object.entries(data.probes || {}).map(([k, v]) => {
      const ok = !v._error;
      const colorBadge = ok
        ? `<span style="color:var(--accent-2);">✓</span>`
        : `<span class="muted">✗</span>`;
      // Extract the bare method name (e.g. "getStandings" from "getStandings({})")
      const methodName = k.split("(")[0];
      const deepBtn = ok
        ? `<button class="ftx-deep-btn" data-method="${methodName}" style="margin-left:8px;font-size:10px;padding:2px 6px;">drill in</button>`
        : "";
      return `<details ${ok ? "open" : ""} style="margin:4px 0;">
        <summary style="cursor:pointer;font-size:12px;">${colorBadge} <code style="font-family:ui-monospace,Menlo,monospace;">${k}</code>${deepBtn}</summary>
        <pre style="font-size:10.5px;background:var(--panel);border:1px solid var(--border);padding:6px;border-radius:4px;overflow:auto;max-height:280px;">${JSON.stringify(v, null, 2)}</pre>
      </details>`;
    }).join("");
    out.innerHTML = `
      <h3>League: ${data.leagueName || "?"} <span class="muted">(${data.subtitle || "?"})</span></h3>
      <div style="margin:6px 0 12px;font-size:13px;">
        <b>headToHead:</b> ${data.headToHead ?? "?"} · <b>Sport:</b> ${data.sport || "?"} · <b>Season:</b> ${data.season || "?"}
      </div>
      <h4>API method probes</h4>
      <div class="muted" style="font-size:11px;margin-bottom:6px;">Each box below is a Fantrax API endpoint we tried. ✓ = it returned something useful. Click "drill in" on any ✓ box to see the FULL untrimmed response.</div>
      ${probesHtml}
      <div id="ftx-deep-out" style="margin-top:14px;"></div>
    `;
    // Wire drill-in buttons
    document.querySelectorAll(".ftx-deep-btn").forEach((b) => {
      b.addEventListener("click", async (e) => {
        e.preventDefault(); e.stopPropagation();
        const method = b.dataset.method;
        const deepOut = $("#ftx-deep-out");
        deepOut.innerHTML = `<div class="muted">Fetching full response for <code>${method}</code>…</div>`;
        try {
          const full = await api(`/api/fantrax/league_info?league_id=${encodeURIComponent(lg)}&deep=${encodeURIComponent(method)}`);
          deepOut.innerHTML = `<h4>Full response: <code>${method}</code></h4>
            <pre style="font-size:10.5px;background:var(--panel);border:1px solid var(--accent);padding:8px;border-radius:6px;overflow:auto;max-height:600px;">${JSON.stringify(full.response, null, 2)}</pre>`;
        } catch (err) {
          deepOut.innerHTML = `<div style="color:var(--bad);">Drill in failed: ${err.message}</div>`;
        }
      });
    });
    $("#ftx-status").textContent = "✓ League config loaded — check below.";
    $("#ftx-status").style.color = "var(--accent-2)";
  } catch (e) {
    if (/^401\b/.test(e.message)) {
      $("#ftx-status").innerHTML = `<span style="color:var(--bad);">Auth required — set up cookie first.</span>`;
    } else {
      $("#ftx-status").textContent = `Error: ${e.message}`;
      $("#ftx-status").style.color = "var(--bad)";
    }
  }
});

$("#ftx-auth")?.addEventListener("click", () => {
  const panel = $("#ftx-auth-panel");
  if (panel) panel.hidden = !panel.hidden;
});
$("#ftx-auth-close")?.addEventListener("click", () => {
  const panel = $("#ftx-auth-panel");
  if (panel) panel.hidden = true;
});

$("#ftx-cookie-save")?.addEventListener("click", async () => {
  const raw = ($("#ftx-cookie-input")?.value || "").trim();
  const status = $("#ftx-cookie-status");
  if (!raw) { status.textContent = "Paste the cookie value first."; status.style.color = "var(--bad)"; return; }
  // Strip a leading "Cookie:" if user copied the whole header line.
  const cookie = raw.replace(/^cookie:\s*/i, "");
  status.textContent = "Saving…"; status.style.color = "";
  try {
    await api(`/api/fantrax/cookie`, { method: "POST", body: JSON.stringify({ cookie }) });
  } catch (e) {
    status.textContent = `Save failed: ${e.message}`; status.style.color = "var(--bad)";
    return;
  }
  // Verify by trying to list teams against the league_id (if entered).
  const lg = ($("#ftx-league")?.value || "").trim();
  if (!lg) {
    status.textContent = "✓ Saved. Now enter your league_id and click Pull.";
    status.style.color = "var(--accent-2)";
    return;
  }
  status.textContent = "Saved. Verifying with Fantrax…";
  try {
    const data = await api(`/api/fantrax/teams?league_id=${encodeURIComponent(lg)}`);
    const n = (data.teams || []).length;
    status.innerHTML = `✓ Auth works — found <b>${n}</b> teams. Re-pulling your roster…`;
    status.style.color = "var(--accent-2)";
    $("#ftx-cookie-input").value = "";   // clear from UI for safety
    // Auto-trigger the Pull so the user doesn't have to click again.
    setTimeout(() => {
      $("#ftx-auth-panel").hidden = true;
      $("#ftx-pull")?.click();
    }, 600);
  } catch (e) {
    if (/401/.test(e.message)) {
      status.innerHTML = `✗ Cookie didn't work. Re-copy from a fresh logged-in fantrax.com request and try again.`;
    } else {
      status.textContent = `Saved but verification failed: ${e.message}`;
    }
    status.style.color = "var(--bad)";
  }
});

function _showTeamPicker(teams, leagueId) {
  $("#ftx-status").innerHTML = "";
  // Insert a picker right after the status span if not already there.
  let picker = document.getElementById("ftx-team-picker");
  if (picker) picker.remove();
  picker = document.createElement("div");
  picker.id = "ftx-team-picker";
  picker.className = "setup-row";
  picker.style.marginTop = "6px";
  const opts = teams.map(t => `<option value="${t.team_id}">${t.name}</option>`).join("");
  picker.innerHTML = `
    <span class="muted" style="font-size:12px;">League has ${teams.length} teams — pick yours:</span>
    <select id="ftx-team-select" style="min-width:200px;">${opts}</select>
    <button id="ftx-team-confirm" type="button" class="btn-pick">Use this team</button>
  `;
  $("#ftx-status").parentNode.insertAdjacentElement("afterend", picker);
  document.getElementById("ftx-team-confirm").addEventListener("click", () => {
    const tid = document.getElementById("ftx-team-select").value;
    $("#ftx-team").value = tid;
    localStorage.setItem("mlb_dfs_ftx_team", tid);
    picker.remove();
    $("#ftx-pull").click();
  });
}

$("#ftx-pull")?.addEventListener("click", async () => {
  const lg = $("#ftx-league").value.trim();
  const tm = $("#ftx-team").value.trim();
  if (!lg) return alert("Enter your Fantrax league_id (visible in any league URL).");
  localStorage.setItem("mlb_dfs_ftx_league", lg);
  if (tm) localStorage.setItem("mlb_dfs_ftx_team", tm);
  $("#ftx-status").textContent = "Pulling roster…";
  $("#ftx-status").style.color = "";
  try {
    const url = `/api/fantrax/roster?league_id=${encodeURIComponent(lg)}` + (tm ? `&team_id=${encodeURIComponent(tm)}` : "");
    const data = await api(url);
    if (data.error) {
      _showTeamPicker(data.teams || [], lg);
      return;
    }
    const names = (data.players || []).map(p => p.name).filter(Boolean);
    $("#lineup-names").value = names.join("\n");
    localStorage.setItem("mlb_dfs_lineup_names", $("#lineup-names").value);
    // Cache the full player list — /api/lineup needs position eligibility for
    // slot-aware optimization.
    _lastFantraxPlayers = data.players || [];
    _lastFantraxSlotCounts = data.slot_counts || null;
    $("#ftx-status").textContent = `✓ Pulled ${names.length} from ${data.team_name || "team"}. Click Project lineup.`;
    $("#ftx-status").style.color = "var(--accent-2)";
  } catch (e) {
    // 401 = auth needed/expired. Auto-open the cookie panel so the user
    // doesn't have to hunt for the button.
    if (/^401\b/.test(e.message)) {
      $("#ftx-status").innerHTML = `<span style="color:var(--bad);">Auth required — opening cookie setup ↓</span>`;
      const panel = $("#ftx-auth-panel");
      if (panel) {
        panel.hidden = false;
        panel.scrollIntoView({ behavior: "smooth", block: "center" });
        const status = $("#ftx-cookie-status");
        if (status) {
          status.innerHTML = `<span style="color:var(--bad);">Your saved cookie didn't work (or there isn't one). Follow the steps above to paste a fresh one, then click Save & test.</span>`;
        }
        setTimeout(() => $("#ftx-cookie-input")?.focus(), 400);
      }
    } else {
      $("#ftx-status").textContent = `Error: ${e.message}`;
      $("#ftx-status").style.color = "var(--bad)";
    }
  }
});
$("#date").addEventListener("change", async () => {
  const newDate = $("#date").value;
  state._slateDate = null;
  state.slateGames = [];
  if (state.tab === "draft") {
    stopPolling();
    state.currentDraftId = null;
    state.selectedGamePks = new Set();
    poolCache = { draftId: null, pool: [] };
    try { await loadDraftList(); } catch {}
    const sel = $("#draft-id");
    const opts = sel ? Array.from(sel.options).map((o) => o.value).filter(Boolean) : [];
    let pick = null;
    if (opts.includes(newDate)) {
      pick = newDate;
    } else {
      // No draft on the chosen date — fall back to the next upcoming draft
      // (≥ chosen date), or the most recent past draft if none upcoming.
      const upcoming = opts.filter((d) => d >= newDate).sort();
      const past = opts.filter((d) => d < newDate).sort().reverse();
      pick = upcoming[0] || past[0] || null;
    }
    if (pick && sel) {
      sel.value = pick;
      state.currentDraftId = pick;
      await syncToLoadedDraft();
      startPolling();
      return;
    }
  }
  if (state.tab === "score") {
    try { await loadDraftList(); } catch {}
    const sel = $("#score-draft-id");
    if (sel && Array.from(sel.options).some((o) => o.value === newDate)) {
      sel.value = newDate;
      state.currentDraftId = newDate;
      $("#score-load").click();
      return;
    }
  }
  refresh();
});

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

  // First-pitch local time for SCHEDULED games (no value once live/final).
  const gameTime = (() => {
    if (!g.gameDate) return "";
    try {
      return new Date(g.gameDate).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    } catch { return ""; }
  })();
  // Live status header — inning + half indicator, or FINAL, or SCHEDULED + time.
  let topBar = `<div class="status">${g.detailedStatus ?? ""}${gameTime ? ` <span class="muted" style="font-weight:400;">· ${gameTime}</span>` : ""}</div>`;
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

// Edit-mode lock: the game pool is read-only by default. User clicks "Edit
// pool" to mutate, then "Save" to commit (or "Cancel" to discard). Prevents
// accidentally clobbering a curated 6-game pool with a stray click.
let _poolEditing = false;
let _poolEditOriginal = null;  // snapshot of selectedGamePks at edit start

function renderGamePicker() {
  const host = $("#game-picker");
  if (!host) return;
  if (!state.slateGames.length) {
    host.innerHTML = `<div class="muted">No games today — switch to a date with games.</div>`;
    $("#game-count").textContent = "";
    return;
  }
  // In draft-edit mode with no explicit game selection, the full slate is
  // implicitly included in the pool. Show every card green so the user
  // sees "yes, all 15 games are in this draft".
  const fullSlateInDraft = !!state.currentDraftId && state.selectedGamePks.size === 0;
  host.innerHTML = state.slateGames
    .map((g) => {
      const explicitlySel = state.selectedGamePks.has(g.gamePk);
      const sel = (explicitlySel || fullSlateInDraft) ? "selected" : "";
      const editClass = _poolEditing ? "editable" : "readonly";
      const ap = g.away.probablePitcher?.name ?? "TBD";
      const hp = g.home.probablePitcher?.name ?? "TBD";
      return `
        <div class="game-card ${sel} ${editClass}" data-pk="${g.gamePk}">
          <div class="status">${g.detailedStatus ?? ""}</div>
          <div class="matchup">${g.away.abbr ?? g.away.name} @ ${g.home.abbr ?? g.home.name}</div>
          <div class="sps">SP: ${ap} vs ${hp}</div>
        </div>`;
    })
    .join("");
  // Click handler is only wired when the picker is in Edit mode. In view
  // mode cards are pure display — no accidental mutations.
  if (_poolEditing) {
    $$("#game-picker .game-card").forEach((el) => {
      el.addEventListener("click", () => {
        const pk = Number(el.dataset.pk);
        // Starting from implicit-all, the first click in Edit mode promotes
        // to explicit-all (so subsequent clicks subtract). User can then
        // build the desired narrow set.
        if (fullSlateInDraft && state.selectedGamePks.size === 0) {
          state.slateGames.forEach((g) => state.selectedGamePks.add(g.gamePk));
          state.selectedGamePks.delete(pk);
        } else if (state.selectedGamePks.has(pk)) {
          state.selectedGamePks.delete(pk);
        } else {
          state.selectedGamePks.add(pk);
        }
        renderGamePicker();
      });
    });
  }
  const n = state.selectedGamePks.size;
  const total = state.slateGames.length;
  let suffix;
  if (_poolEditing) {
    suffix = `· editing — ${n || "all " + total} in pool`;
  } else if (state.currentDraftId) {
    suffix = n ? `· ${n} of ${total} games` : `· full slate (${total} games)`;
  } else {
    suffix = n ? `· ${n} selected` : `· ${total} total`;
  }
  $("#game-count").textContent = suffix;
  $("#game-picker").classList.toggle("editing-draft", !!state.currentDraftId);
  $("#game-picker").classList.toggle("edit-mode", _poolEditing);
  // Toggle button visibility based on edit state.
  $("#games-edit").hidden = _poolEditing;
  $("#games-save").hidden = !_poolEditing;
  $("#games-cancel").hidden = !_poolEditing;
  $("#games-clear").hidden = !_poolEditing;
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
  // Helper: render an adjustment factor row, color-coded vs 1.0.
  const factorRow2 = (label, factor, detail = "") => {
    if (factor == null || Math.abs(factor - 1.0) < 0.005) return "";
    const cls = factor > 1.0 ? "pos" : "neg";
    const det = detail ? ` <span class="muted">${detail}</span>` : "";
    return `<div class="bk-row"><span class="bk-label">${label}</span><span class="bk-total ${cls}">×${factor.toFixed(2)}${det}</span></div>`;
  };
  // Same but always renders the row, with muted "neutral" badge when factor is 1.0.
  // Use this for v9.3 factors so users can see the model considered them even when
  // there's no signal to apply (e.g. SB threat for a non-runner, TTO for short starters).
  const factorRow2Always = (label, factor, detail = "", neutralLabel = "no signal") => {
    if (factor == null) return "";
    if (Math.abs(factor - 1.0) < 0.005) {
      return `<div class="bk-row"><span class="bk-label">${label}</span><span class="bk-total muted">×1.00 <span style="font-size:11px;">(${neutralLabel})</span></span></div>`;
    }
    const cls = factor > 1.0 ? "pos" : "neg";
    const det = detail ? ` <span class="muted">${detail}</span>` : "";
    return `<div class="bk-row"><span class="bk-label">${label}</span><span class="bk-total ${cls}">×${factor.toFixed(2)}${det}</span></div>`;
  };
  // Hot/cold post-matchup multiplier (v9.7 hitter / v9.10 pitcher). Stored
  // as `hot_cold_factor` so the tooltip can show the exact applied value.
  const hotColdRow = () => {
    const f = c.hot_cold_factor;
    if (f == null || Math.abs(f - 1.0) < 0.005) return "";
    const cls = f > 1.0 ? "pos" : "neg";
    const tag = c.form_tag || "";
    const label = tag === "HOT" ? `HOT post-matchup`
                 : tag === "COLD" ? `COLD post-matchup`
                 : tag === "ELITE" ? `ELITE form post-matchup`
                 : tag === "STEADY" ? `STEADY form post-matchup`
                 : `Post-matchup`;
    return `<div class="bk-row"><span class="bk-label">${label}</span><span class="bk-total ${cls}">×${f.toFixed(2)}</span></div>`;
  };
  // Reconciliation row: shows the exact factor product (chain × hot_cold) so
  // a user multiplying the displayed factors can verify the final projection.
  // Pre-rounding chain_product comes from the projection engine and matches
  // the math the model actually did, even if some rows showed ×1.00 because
  // a sub-factor was 0.997 etc.
  const chainTotalRow = () => {
    if (c.chain_product == null || c.base_pg == null && c.base_per_start == null) return "";
    return `<div class="bk-row" style="border-top:1px dashed var(--border);padding-top:4px;margin-top:4px;"><span class="bk-label muted">Chain total (base × all factors)</span><span class="bk-total muted">×${c.chain_product.toFixed(3)}</span></div>`;
  };
  if (p.role === "hitter") {
    if (c.base_pg != null) rows.push(`<div class="bk-row"><span class="bk-label">Base 14d pts/G</span><span class="bk-total">${c.base_pg.toFixed(2)}</span></div>`);
    // All chain factors render ALWAYS (×1.00 shows as "neutral") so the user
    // can audit every step and the displayed math reconciles to the final.
    // Opp SP factor: when Vegas implied total is set, sp_factor is reset to
    // 1.0 in the chain to avoid double-counting (Vegas already prices in the
    // opposing SP). Show the RAW computed value with an "(absorbed by Vegas)"
    // annotation. Also surface when fallback fired (low-IP SP → Savant xERA).
    if (c.sp_absorbed_by_vegas && c.sp_factor_raw != null && Math.abs(c.sp_factor_raw - 1.0) >= 0.005) {
      const f = c.sp_factor_raw;
      const cls = f > 1.0 ? "pos" : "neg";
      const src = c.sp_factor_source === "savant_fallback" ? " · Savant fallback (low IP)" : "";
      rows.push(`<div class="bk-row"><span class="bk-label">Opp SP</span><span class="bk-total ${cls}">×${f.toFixed(2)} <span class="muted">(folded into Vegas${src})</span></span></div>`);
    } else if (c.sp_factor != null) {
      const src = c.sp_factor_source === "savant_fallback" ? "Savant fallback (low IP)" : "no signal";
      rows.push(factorRow2Always("Opp SP", c.sp_factor, "", src));
    }
    if (c.qoc_factor != null) rows.push(factorRow2Always("QoC residual", c.qoc_factor, "", "neutral"));
    // Park: when combined factor is near-1.0 but the breakdown shows real
    // components (e.g. WSH: 1.02 run env + 0.97 weather-suppressed HR cancel
    // to 1.003), show "neutral net" with the underlying values so the user
    // can see the model DID look at the park — it just balanced out.
    {
      const pf = c.park_factor;
      const pb = c.park_breakdown;
      const isNeutral = pf != null && Math.abs(pf - 1.0) < 0.005;
      const hasRealBreakdown = pb && (Math.abs((pb.run_env ?? 1) - 1) >= 0.005 || Math.abs((pb.hr_factor ?? 1) - 1) >= 0.005 || Math.abs((pb.hand_bias ?? 1) - 1) >= 0.005);
      if (isNeutral && hasRealBreakdown) {
        const parts = [];
        if (Math.abs((pb.run_env ?? 1) - 1) >= 0.005) parts.push(`run ${pb.run_env.toFixed(2)}`);
        if (Math.abs((pb.hr_factor ?? 1) - 1) >= 0.005) parts.push(`HR ${pb.hr_factor.toFixed(2)}`);
        if (Math.abs((pb.hand_bias ?? 1) - 1) >= 0.005) parts.push(`${c.bats || ""}H bias ${pb.hand_bias.toFixed(2)}`);
        const venueStr = c.park_venue ? `${c.park_venue} · ` : "";
        rows.push(`<div class="bk-row"><span class="bk-label">Park</span><span class="bk-total muted">×1.00 <span style="font-size:11px;">(${venueStr}neutral net — ${parts.join(" · ")})</span></span></div>`);
      } else {
        rows.push(factorRow2Always("Park", c.park_factor, c.park_venue || "", "neutral park"));
      }
    }
    rows.push(factorRow2Always("Vegas implied", c.vegas_factor, c.implied_team_total ? `${c.implied_team_total.toFixed(1)} R` : "", "no Vegas line"));
    rows.push(factorRow2Always("Order PA", c.order_factor, c.batting_order ? `#${c.batting_order}` : "", "lineup not posted"));
    rows.push(factorRow2Always("Platoon", c.platoon_factor, (c.bats && c.vs_throws) ? `${c.bats}H vs ${c.vs_throws}HP` : "", "no platoon edge"));
    // Bullpen: same Vegas-supersedes pattern as Opp SP. When Vegas is set the
    // chain factor is 1.0 but the raw bullpen-implied factor still tells you
    // what we'd otherwise have applied — surface it with the "folded into
    // Vegas" annotation so the tooltip doesn't claim "league-avg pen" when
    // we actually know the opposing pen ERA.
    if (c.bullpen_absorbed_by_vegas && c.bullpen_factor_raw != null && Math.abs(c.bullpen_factor_raw - 1.0) >= 0.005) {
      const f = c.bullpen_factor_raw;
      const cls = f > 1.0 ? "pos" : "neg";
      const det = c.opp_bullpen_era ? ` <span class="muted">${c.opp_bullpen_era.toFixed(2)} ERA · folded into Vegas</span>` : ` <span class="muted">(folded into Vegas)</span>`;
      rows.push(`<div class="bk-row"><span class="bk-label">Opp bullpen</span><span class="bk-total ${cls}">×${f.toFixed(2)}${det}</span></div>`);
    } else {
      rows.push(factorRow2Always("Opp bullpen", c.bullpen_factor, c.opp_bullpen_era ? `${c.opp_bullpen_era.toFixed(2)} ERA` : "", "league-avg pen"));
    }
    rows.push(factorRow2Always("Rolling K-rate", c.rolling_factor, (c.rolling_k_pct != null && c.season_k_pct != null) ? `${(c.rolling_k_pct*100).toFixed(1)}% vs szn ${(c.season_k_pct*100).toFixed(1)}%` : "", (c.rolling_k_pct != null && c.season_k_pct != null) ? `${(c.rolling_k_pct*100).toFixed(1)}% ≈ szn ${(c.season_k_pct*100).toFixed(1)}% (neutral)` : (c.rolling_pa_l14 ? `min 30 PA — have ${c.rolling_pa_l14}` : "no recent PAs")));
    rows.push(factorRow2Always("ISO form (v9.3)", c.iso_factor, "", "no power surge/slump"));
    rows.push(factorRow2Always("SB threat (v9.3)", c.sb_factor, "", "not an SB threat"));
    const hcRow = hotColdRow();
    if (hcRow) rows.push(hcRow);
    rows.push(chainTotalRow());
    if (c.barrel_pct != null) rows.push(`<div class="bk-row"><span class="bk-label">Barrel %</span><span class="bk-total">${c.barrel_pct.toFixed(1)} <span class="muted">(lg ${(c.lg_barrel_pct ?? 8.8).toFixed(1)})</span></span></div>`);
    if (c.hardhit_pct != null) rows.push(`<div class="bk-row"><span class="bk-label">Hard-hit %</span><span class="bk-total">${c.hardhit_pct.toFixed(0)} <span class="muted">(lg ${(c.lg_hardhit_pct ?? 40).toFixed(0)})</span></span></div>`);
  } else {
    if (c.base_per_start != null) rows.push(`<div class="bk-row"><span class="bk-label">Base 14d pts/start</span><span class="bk-total">${c.base_per_start.toFixed(2)}</span></div>`);
    if (c.is_opener) rows.push(`<div class="bk-row"><span class="bk-label">Role</span><span class="bk-total neg">OPENER (${c.ip_per_start ?? "?"} IP/start)</span></div>`);
    else if (c.ip_per_start != null) rows.push(`<div class="bk-row"><span class="bk-label">Avg IP/start</span><span class="bk-total muted">${c.ip_per_start.toFixed(1)}</span></div>`);
    // Opp run-env factor: same Vegas-supersedes pattern as hitter sp_factor.
    if (c.opp_absorbed_by_vegas && c.opp_factor_raw != null && Math.abs(c.opp_factor_raw - 1.0) >= 0.005) {
      const f = c.opp_factor_raw;
      const cls = f > 1.0 ? "pos" : "neg";
      rows.push(`<div class="bk-row"><span class="bk-label">Opp run-env</span><span class="bk-total ${cls}">×${f.toFixed(2)} <span class="muted">(folded into Vegas)</span></span></div>`);
    } else if (c.opp_factor != null) {
      rows.push(factorRow2Always("Opp run-env", c.opp_factor, "", "neutral offense"));
    }
    if (c.qoc_factor != null) rows.push(factorRow2Always("QoC residual", c.qoc_factor, "", "neutral"));
    rows.push(factorRow2Always("Park", c.park_factor, c.park_venue || "", "neutral park"));
    rows.push(factorRow2Always("Opp Vegas", c.vegas_factor, c.opp_implied_total ? `${c.opp_implied_total.toFixed(1)} R` : "", "no Vegas line"));
    rows.push(factorRow2Always("Rolling K-rate", c.rolling_factor, (c.rolling_k_pct != null && c.season_k_pct != null) ? `${(c.rolling_k_pct*100).toFixed(1)}% vs szn ${(c.season_k_pct*100).toFixed(1)}%` : "", c.rolling_bf_l14 ? `min 30 BF — have ${c.rolling_bf_l14}` : "no recent BF"));
    rows.push(factorRow2Always("HP ump", c.ump_factor, "", "neutral ump"));
    // Opp lineup: same Vegas-supersedes pattern. When Vegas opp implied total
    // is set, lineup_factor is reset to 1.0 in the chain (Vegas already prices
    // in lineup quality). Show raw with "folded into Vegas" annotation.
    if (c.lineup_absorbed_by_vegas && c.lineup_factor_raw != null && Math.abs(c.lineup_factor_raw - 1.0) >= 0.005) {
      const f = c.lineup_factor_raw;
      const cls = f > 1.0 ? "pos" : "neg";
      rows.push(`<div class="bk-row"><span class="bk-label">Opp lineup</span><span class="bk-total ${cls}">×${f.toFixed(2)} <span class="muted">(folded into Vegas)</span></span></div>`);
    } else {
      rows.push(factorRow2Always("Opp lineup", c.lineup_factor, "", "lineup not posted"));
    }
    rows.push(factorRow2Always("Catcher framing (v9.8)", c.framing_factor, c.catcher_framing_rv != null ? `rv ${c.catcher_framing_rv >= 0 ? "+" : ""}${c.catcher_framing_rv.toFixed(1)}` : "", "no framing data"));
    rows.push(factorRow2Always("TTO penalty (v9.3)", c.tto_factor, c.ip_per_start ? `${c.ip_per_start} IP/start` : "", "short starter — no TTO3"));
    rows.push(factorRow2Always("Team defense (v9.3)", c.defense_factor, "", "league-avg fielding"));
    const hcRow = hotColdRow();
    if (hcRow) rows.push(hcRow);
    rows.push(chainTotalRow());
    // K-prop adjustment (v9.5) is ADDITIVE, not multiplicative — applied
    // after the factor chain, so it doesn't fit in the chain_product. Show
    // as a +/- pts row with the Vegas K line for context.
    if (c.k_prop_adj != null && Math.abs(c.k_prop_adj) >= 0.05) {
      const cls = c.k_prop_adj > 0 ? "pos" : "neg";
      const sign = c.k_prop_adj > 0 ? "+" : "";
      const det = c.vegas_k_line ? ` <span class="muted">Vegas ${c.vegas_k_line.toFixed(1)} K</span>` : "";
      rows.push(`<div class="bk-row"><span class="bk-label">K-prop adj (v9.5)</span><span class="bk-total ${cls}">${sign}${c.k_prop_adj.toFixed(2)} pts${det}</span></div>`);
    }
    if (c.k9_season != null) rows.push(`<div class="bk-row"><span class="bk-label">K/9 (season)</span><span class="bk-total">${c.k9_season.toFixed(1)}</span></div>`);
    if (c.xera != null) rows.push(`<div class="bk-row"><span class="bk-label">xERA</span><span class="bk-total">${c.xera.toFixed(2)}</span></div>`);
    if (c.xwoba_against != null) rows.push(`<div class="bk-row"><span class="bk-label">xwOBA agst</span><span class="bk-total">${c.xwoba_against.toFixed(3)}</span></div>`);
    if (c.barrel_pct_allowed != null) rows.push(`<div class="bk-row"><span class="bk-label">brl-allowed %</span><span class="bk-total">${c.barrel_pct_allowed.toFixed(1)} <span class="muted">(lg ${(c.lg_barrel_pct_allowed ?? 8.0).toFixed(1)})</span></span></div>`);
  }
  // Drop empty strings (factors at 1.0 in the legacy hide-on-neutral path).
  for (let i = rows.length - 1; i >= 0; i--) if (rows[i] === "") rows.splice(i, 1);
  // Injury report row at the top of pitfalls — louder than a plain pitfall
  // because a D2D/IL flag changes the EV materially (Yandy on a 0.05 zeroed
  // projection if he's scratched, e.g.) and we want the user to see it.
  const inj = c.injury;
  const injHtml = inj && inj.status ? (() => {
    const det = [inj.type, inj.return_date ? `back ~${inj.return_date}` : null].filter(Boolean).join(" · ");
    const cmt = inj.comment ? `<div style="font-size:11px;color:var(--muted);margin-top:2px;">${escapeAttr(inj.comment).slice(0, 200)}</div>` : "";
    return `<div class="bk-row bk-pitfall" style="flex-direction:column;align-items:stretch;">🤕 <b>${inj.status}</b>${det ? " — " + det : ""}${cmt}</div>`;
  })() : "";
  const pitfalls = injHtml + (c.pitfalls || []).map(s => `<div class="bk-row bk-pitfall">⚠ ${s}</div>`).join("");
  const tierBadge = tier ? `<span class="bench-tag" style="background:${tier==="ELITE"?"rgba(52,211,153,0.25)":tier==="POOR"?"rgba(239,68,68,0.25)":"var(--border)"};color:${tier==="ELITE"?"var(--accent-2)":tier==="POOR"?"var(--bad)":"var(--text)"};">${tier}</span>` : "";
  const formB = formBadge(c.form_tag);
  // Per-game category projection (H2H cats) — "what cats it does". Shows the
  // expected per-game contribution to each scoring category so the points
  // total is grounded in actual stat lines (R/HR/RBI/SB/OPS or QS/K/ERA/WHIP/SVH).
  let catsHtml = "";
  const cp = p.cat_proj;
  if (cp && Object.keys(cp).length) {
    const order = p.role === "pitcher"
      ? [["QS","QS",2], ["K","K",1], ["ERA","ERA",2], ["WHIP","WHIP",2], ["SVH","SV+H",1]]
      : [["R","R",2], ["HR","HR",2], ["RBI","RBI",2], ["SB","SB",2], ["OPS","OPS",3]];
    const cells = order.filter(([k]) => cp[k] != null).map(([k, lbl, dp]) =>
      `<td style="text-align:center;padding:1px 7px;"><div class="muted" style="font-size:10px;">${lbl}</div><div style="font-weight:600;">${Number(cp[k]).toFixed(dp)}</div></td>`
    ).join("");
    catsHtml = `<div class="bk-rows" style="margin-top:6px;border-top:1px solid var(--border);padding-top:6px;">
      <div class="bk-label muted" style="font-size:11px;margin-bottom:2px;">Per-game category projection</div>
      <table style="border-collapse:collapse;"><tr>${cells}</tr></table>
    </div>`;
  }
  // "What makes up this number" — decompose the projected fantasy points into
  // the expected stat line (e.g. 0.20 HR, 0.05 SB) × the scoring weight for
  // each event. Events are scaled so the contributions sum to the projection,
  // so a 9 reads literally as "the line worth ~9 DK pts."
  let decompHtml = "";
  const pd = c.point_decomp;
  if (pd && pd.lines && pd.lines.length) {
    const drows = pd.lines.map(l => {
      const cls = l.pts < 0 ? "neg" : "pos";
      const sign = l.pts >= 0 ? "+" : "";
      return `<div class="bk-row"><span class="bk-label">${l.label} <span class="muted" style="font-size:10px;">${l.n}/g × ${l.pts_each}</span></span><span class="bk-total ${cls}">${sign}${l.pts.toFixed(2)}</span></div>`;
    }).join("");
    decompHtml = `<div class="bk-rows" style="margin-top:6px;border-top:1px solid var(--border);padding-top:6px;">
      <div class="bk-label muted" style="font-size:11px;margin-bottom:2px;">What makes up this number — expected line × DK points</div>
      ${drows}
      <div class="bk-row" style="border-top:1px solid var(--border);padding-top:3px;margin-top:3px;"><span class="bk-label"><b>Total</b></span><span class="bk-total"><b>${pd.total.toFixed(2)} pts</b></span></div>
    </div>`;
  }
  return `<div class="breakdown-tooltip">
    <div class="bk-title">${p.name} ${formB} ${tierBadge} <span class="muted" style="font-weight:400;font-size:11px;">— projection breakdown</span></div>
    <div class="bk-rows">${rows.join("")}</div>
    <div class="bk-grand"><span>Projection</span><span>${(p.projected_points ?? p.projected ?? 0).toFixed(2)} pts</span></div>
    ${(c.floor != null && c.ceiling != null) ? `<div class="bk-row" style="margin-top:2px;"><span class="bk-label">Range (p10–p90)</span><span class="bk-total muted">${c.floor.toFixed(1)} → ${c.ceiling.toFixed(1)}</span></div>` : ""}
    ${decompHtml}
    ${catsHtml}
    ${pitfalls ? `<div class="bk-rows" style="margin-top:6px;border-top:1px solid var(--border);padding-top:6px;">${pitfalls}</div>` : ""}
  </div>`;
}

// Live-projection tooltip — shown when hovering the "Live" column on the
// Live Score tab. Explains the live math (actual + remaining × pre_game)
// plus the full pre-game factor breakdown so users can audit both layers.
//
// Pick shape (from /api/drafts/{id}/score): {
//   projected, live_projection, remaining_fraction, actual, raw,
//   game_state, components: {role, ip_per_start, ...}, ...
// }
function liveProjTooltip(p) {
  const pre = p.projected ?? 0;
  const act = p.actual ?? 0;
  const live = p.live_projection ?? pre;
  const remaining = p.remaining_fraction ?? 0;
  const remPct = (remaining * 100).toFixed(0);
  const role = (p.components || {}).role || p.role || "hitter";
  // Math explainer
  let mathRows;
  if (role === "pitcher") {
    const ipPerStart = (p.components || {}).ip_per_start ?? 5.5;
    const expectedOuts = Math.max(6, Math.round(ipPerStart * 3));
    const outs = (p.raw || {}).outs || 0;
    mathRows = `
      <div class="bk-row"><span class="bk-label">Pre-game projection</span><span class="bk-total">${pre.toFixed(2)} pts</span></div>
      <div class="bk-row"><span class="bk-label">Actual so far</span><span class="bk-total">${act.toFixed(2)} pts</span></div>
      <div class="bk-row"><span class="bk-label">Outs recorded</span><span class="bk-total muted">${outs} of ${expectedOuts} expected (${ipPerStart} IP/start × 3)</span></div>
      <div class="bk-row"><span class="bk-label">Remaining share</span><span class="bk-total">${remPct}%</span></div>
      <div class="bk-row" style="border-top:1px solid var(--border);padding-top:4px;margin-top:4px;">
        <span class="bk-label">Live = actual + pre × remaining</span>
        <span class="bk-total">${act.toFixed(2)} + ${pre.toFixed(2)} × ${remaining.toFixed(2)}</span>
      </div>`;
  } else {
    const pa = (p.raw || {}).PA || 0;
    const expectedPA = 4.3;
    mathRows = `
      <div class="bk-row"><span class="bk-label">Pre-game projection</span><span class="bk-total">${pre.toFixed(2)} pts</span></div>
      <div class="bk-row"><span class="bk-label">Actual so far</span><span class="bk-total">${act.toFixed(2)} pts</span></div>
      <div class="bk-row"><span class="bk-label">PAs taken</span><span class="bk-total muted">${pa} of ${expectedPA.toFixed(1)} expected</span></div>
      <div class="bk-row"><span class="bk-label">Remaining share</span><span class="bk-total">${remPct}%</span></div>
      <div class="bk-row" style="border-top:1px solid var(--border);padding-top:4px;margin-top:4px;">
        <span class="bk-label">Live = actual + pre × remaining</span>
        <span class="bk-total">${act.toFixed(2)} + ${pre.toFixed(2)} × ${remaining.toFixed(2)}</span>
      </div>`;
  }
  // Reuse the pre-game projection breakdown for the bottom half. projTooltip
  // expects {name, projected_points, role, components} — adapt the score-pick.
  const pre_tt_inner = projTooltip({
    name: p.name,
    projected_points: pre,
    role: role,
    components: p.components || {},
  });
  // Strip the outer wrapper from projTooltip so we can inline its content.
  // projTooltip returns '<div class="breakdown-tooltip">...</div>'.
  const pre_inner = pre_tt_inner.replace(/^<div class="breakdown-tooltip">/, "").replace(/<\/div>\s*$/, "");
  return `<div class="breakdown-tooltip">
    <div class="bk-title">${p.name} <span class="muted" style="font-weight:400;font-size:11px;">— live projection</span></div>
    <div class="bk-rows">${mathRows}</div>
    <div class="bk-grand"><span>Live projection</span><span>${live.toFixed(2)} pts</span></div>
    <div class="bk-rows" style="margin-top:8px;border-top:1px solid var(--border);padding-top:6px;">
      <div class="bk-row"><span class="muted" style="font-size:11px;font-style:italic;">Pre-game seed (full breakdown):</span></div>
      ${pre_inner}
    </div>
  </div>`;
}

let projView = "mean"; // mean | ceiling | floor — empirical p10/p90 bands (v9.42)
function _projViewVal(p) {
  const c = p.components || {};
  if (projView === "ceiling") return c.ceiling ?? p.projected_points;
  if (projView === "floor") return c.floor ?? p.projected_points;
  return p.projected_points;
}
function renderProjectionsTable() {
  const head = projView === "ceiling" ? "Ceiling" : projView === "floor" ? "Floor" : "Pts";
  const rows = projCache.data
    .map((p) => ({ p, v: _projViewVal(p) }))
    .sort((a, b) => b.v - a.v)
    .slice(0, 60)
    .map(
      ({ p, v }) => `
      <tr class="${p.role} score-row">
        <td>${v.toFixed(2)}${projView !== "mean" ? ` <span class="muted" style="font-size:11px;">(${p.projected_points.toFixed(1)})</span>` : ""}</td>
        <td class="player-cell"><span class="name-trigger">${p.name} ${formBadge((p.components||{}).form_tag)} ${injuryBadge((p.components||{}).injury)}</span>${_oppFromComponents(p)}${projTooltip(p)}</td>
        <td>${p.position ?? "-"}</td>
        <td>${p.role}</td>
        <td class="notes">${(p.notes || []).join(" · ")}</td>
      </tr>`,
    )
    .join("");
  $("#proj-out").innerHTML = `
    <div style="margin-bottom:8px;">
      <label class="muted" style="font-size:12px;">View:
        <select id="proj-view-sel">
          <option value="mean" ${projView === "mean" ? "selected" : ""}>Mean (expected)</option>
          <option value="ceiling" ${projView === "ceiling" ? "selected" : ""}>Ceiling (proj+σ) — tournament upside</option>
          <option value="floor" ${projView === "floor" ? "selected" : ""}>Floor (proj−σ) — safety</option>
        </select>
      </label>
    </div>
    <table>
      <thead><tr><th>${head}</th><th>Player</th><th>Pos</th><th>Role</th><th>Notes</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  const sel = $("#proj-view-sel");
  if (sel) sel.onchange = () => { projView = sel.value; renderProjectionsTable(); };
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
  // Clear in edit mode = full slate intent. Save still has to be clicked
  // to commit, so this isn't a destructive action without confirmation.
  state.selectedGamePks.clear();
  renderGamePicker();
});

$("#games-edit").addEventListener("click", () => {
  _poolEditing = true;
  _poolEditOriginal = new Set(state.selectedGamePks);
  renderGamePicker();
});

$("#games-cancel").addEventListener("click", () => {
  // Restore the snapshot taken when Edit was clicked.
  if (_poolEditOriginal) state.selectedGamePks = new Set(_poolEditOriginal);
  _poolEditing = false;
  _poolEditOriginal = null;
  renderGamePicker();
});

$("#games-save").addEventListener("click", async () => {
  // Commit: POST to backend if a draft is loaded, then exit edit mode.
  // No-op write when no draft is loaded — selection is just used by the
  // Start-draft path.
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
      return;
    }
  }
  _poolEditing = false;
  _poolEditOriginal = null;
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

$("#show-tag-guide")?.addEventListener("click", () => {
  const guide = $("#tag-guide");
  if (guide) guide.hidden = !guide.hidden;
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
  // Pass the already-fetched data so renderDraft skips its own fetch.
  await renderDraft(data);
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
  // Pass our drafter name so the backend rejects the call if the last pick
  // belongs to someone else — protects against one user accidentally undoing
  // another user's pick during a live draft.
  const url = `/api/drafts/${state.currentDraftId}/last_pick${state.identity ? `?drafter=${encodeURIComponent(state.identity)}` : ""}`;
  try {
    await api(url, { method: "DELETE" });
    await renderDraft();
  } catch (e) {
    // Backend returns 403 with a helpful message when it's not your pick.
    alert(e.message || "Could not undo: " + e);
  }
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

async function renderDraft(prefetchedData) {
  if (!state.currentDraftId) {
    $("#draft-state").innerHTML = `<div class="muted">No draft loaded. Start a new one or pick from the dropdown.</div>`;
    $("#pick-log").innerHTML = "";
    $("#recs-out").innerHTML = "";
    $("#pool-out").innerHTML = "";
    $("#identity-bar").hidden = true;
    state.lastPicksCount = -1;
    return;
  }
  // Allow caller (e.g. syncToLoadedDraft) to pass already-fetched draft state
  // so we don't re-hit /api/drafts/{id}, which triggers a slow projection
  // recompute on cold cache.
  const data = prefetchedData || await api(`/api/drafts/${state.currentDraftId}`);
  state.lastPicksCount = (data.picks || []).length;
  state._spJumpDrafter = data.sp_jump_drafter || null;
  state._nonSpFree = !!data.non_sp_free;
  state._nextOooDrafter = data.next_ooo_drafter || null;
  state._hitterFreeDrafter = data.hitter_free_drafter || null;
  // Match the poll's myTurn definition exactly — otherwise the poll detects
  // a phantom flip every 4s and re-renders, which looks like flashing.
  state._orderPending = !!data.order_pending_on;
  state._myTurnAtLastRender = !state._orderPending && (isMyTurn(data.on_the_clock) || canJumpForSP() || canJumpForNonSP());
  // The REAL snake turn (genuinely on the clock) — distinct from the jump
  // states. Only a real turn unlocks every position; an SP/non-SP jump must
  // stay restricted to that slot type (see drawPool lockFor). Without this,
  // being the lone-SP-needer unlocked ALL players, not just SP.
  state._isRealTurn = !state._orderPending && isMyTurn(data.on_the_clock);
  renderIdentityBar(data);
  // Enable the Undo button only when the most recent pick belongs to the
  // current user — protects the rest of the league from accidentally undoing
  // someone else's pick. If no identity is set we permit it (backwards-compat;
  // anyone managing the draft can still admin-undo).
  const undoBtn = $("#undo");
  if (undoBtn) {
    const lastPick = (data.picks || [])[((data.picks || []).length - 1)];
    if (!lastPick) {
      undoBtn.disabled = true;
      undoBtn.title = "No picks yet";
      undoBtn.textContent = "Undo last pick";
    } else if (!state.identity || lastPick.drafter === state.identity) {
      undoBtn.disabled = false;
      undoBtn.title = `Undo your pick of ${lastPick.name}`;
      undoBtn.textContent = state.identity ? `↩️ Undo my pick (${lastPick.name})` : "Undo last pick";
    } else {
      undoBtn.disabled = true;
      undoBtn.title = `Last pick was ${lastPick.drafter}'s (${lastPick.name}). Only they can undo it.`;
      undoBtn.textContent = `Undo (${lastPick.drafter}'s pick — locked)`;
    }
  }
  const onClock = data.on_the_clock; // [drafter, suggested_slot] | null  — drafter picks any open slot
  const html = [];
  html.push(`<div class="muted">Draft <b>${data.draft_id}</b> — ${data.is_complete ? "complete" : (data.order_pending_on ? `On the clock: <b>TBD</b> (order set by ${data.order_pending_on} results)` : `On the clock: <b>${onClock?.[0] ?? "-"}</b>`)}</div>`);

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
  // The lone SP-needer in OOO mode can pick SP anytime (not just "on hold").
  const spAnytime = data.sp_jump_drafter || null;
  const oooNext = data.next_ooo_drafter || null;                     // next-in-snake for non-SP OOO pick
  const hitterFree = data.hitter_free_drafter || null;                // lone non-SP-needer, free-for-all hitters
  const pendingOrder = !!data.order_pending_on;
  const stripOrder = pendingOrder ? [...order].sort() : order;
  const orderHtml = stripOrder.map((d) => {
    let cls = "";
    let label = d;
    if (pendingOrder) {
      return `<div class="drafter-strip-row" style="opacity:0.6;">${d}</div>`;
    }
    if (d === hitterFree) {
      cls = "on-clock";
      label = `${d} <span style="font-size:10px;opacity:0.85;">↑ hitters anytime</span>`;
    } else if (d === oooNext) {
      cls = "on-clock";
      label = `${d} <span style="font-size:10px;opacity:0.85;">↑ next</span>`;
    } else if (d === spAnytime && data.non_sp_free) {
      // Held by snake but can interject SP picks at any time — both true.
      cls = "held";
      label = `${d} <span style="font-size:10px;opacity:0.85;">↑ SP anytime</span>`;
    } else if (d === onClockName && !spAnytime) {
      cls = "on-clock";
    } else if (data.rosters[d]?.length > round) {
      cls = "done";
    }
    return `<div class="drafter-strip-row ${cls}">${label}</div>`;
  }).join("");
  const orderNote = data.order_pending_on
    ? `<div class="muted" style="font-size:10px;padding:2px 0;">⏳ pick order TBD — set by ${data.order_pending_on} final standings (winner first)</div>`
    : (data.order_source || "").startsWith("performance:")
      ? `<div class="muted" style="font-size:10px;padding:2px 0;">🏆 order: winner of ${data.order_source.split(":")[1]} picks first</div>`
      : "";
  html.push(`<div class="draft-strip">
    <div class="strip-time">⏰ ${startTime}</div>
    <div class="strip-round">Round ${data.is_complete ? nDrafters * 10 / nDrafters : round + 1}</div>
    ${orderNote}
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
            <div class="slot-name player-cell"><span class="name-trigger">${pick.name} ${formBadge((pick.components||{}).form_tag)} ${injuryBadge((pick.components||{}).injury)}</span>${_oppFromComponents(pick)}${projTooltip(pick)}</div>
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
    // Draft over: replace recs with a flat table of every drafted player +
    // their live projection / form / Statcast / notes. The roster cells above
    // show the headline number; this gives the full breakdown in one place.
    $("#recs-out").innerHTML = renderDraftedTable(data);
    $("#pool-out").innerHTML = "";
  }
}

function renderDraftedTable(data) {
  // Flatten every roster pick into one sortable list. Order by drafter then
  // by projection desc within drafter so it reads like a leaderboard.
  const all = [];
  for (const d of data.drafters) {
    for (const p of (data.rosters[d] || [])) all.push(p);
  }
  if (!all.length) {
    return `<div class="muted">Draft complete — no picks recorded.</div>`;
  }
  all.sort((a, b) => {
    if (a.drafter !== b.drafter) return data.drafters.indexOf(a.drafter) - data.drafters.indexOf(b.drafter);
    return (b.projected ?? 0) - (a.projected ?? 0);
  });
  const rows = all.map((p) => {
    const c = p.components || {};
    let stat = "—";
    if (p.role === "hitter" && (c.barrel_pct != null || c.hardhit_pct != null)) {
      const brl = c.barrel_pct != null ? c.barrel_pct.toFixed(1) : "—";
      const hh = c.hardhit_pct != null ? c.hardhit_pct.toFixed(0) : "—";
      stat = `brl ${brl}% · hh ${hh}%`;
    } else if (p.role === "pitcher" && (c.xera != null || c.xwoba_against != null)) {
      const xe = c.xera != null ? c.xera.toFixed(2) : "—";
      stat = `xERA ${xe}`;
    }
    const notes = (p.notes || []).join(" · ");
    return `
      <tr class="${p.role || ""} score-row">
        <td>${p.drafter}</td>
        <td>${p.slot}</td>
        <td class="player-cell"><span class="name-trigger">${p.name} ${formBadge((p.components||{}).form_tag)} ${injuryBadge((p.components||{}).injury)}</span>${_oppFromComponents(p)}${projTooltip(p)}</td>
        <td>${(p.projected ?? 0).toFixed(2)}</td>
        <td>${p.position ?? "-"}</td>
        <td class="muted" style="font-size:11px;">${stat}</td>
        <td class="notes">${notes}</td>
      </tr>`;
  }).join("");
  return `
    <div class="muted" style="margin-bottom:6px;">Draft complete — projections for every drafted player:</div>
    <table>
      <thead>
        <tr><th>Drafter</th><th>Slot</th><th>Player</th><th>Proj</th><th>Pos</th><th>Statcast</th><th>Notes</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
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
  const myTurn = !data.order_pending_on && isMyTurn(onClock);
  bar.classList.toggle("your-turn", myTurn);
  if (data.order_pending_on && !data.is_complete) {
    $("#turn-status").textContent = `· order TBD — drafting locked until ${data.order_pending_on} results are final ⏳`;
  } else if (data.is_complete) {
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
  // Replaceable iff (a) eligible for the slot AND (b) at least one of the
  // player's slate games hasn't started yet. Backend stamps `replaceable`
  // on every pool entry — once every relevant game is Live/Final you can't
  // add a new player for that team for the day.
  const beforeFilterCount = data.pool.filter((p) => (p.position_slots || p.eligible_slots).includes(slot)).length;
  const allCandidates = data.pool
    .filter((p) => (p.position_slots || p.eligible_slots).includes(slot))
    .filter((p) => p.replaceable !== false)
    .sort((a, b) => {
      const order = { in: 0, pending: 1, out: 2, undefined: 1 };
      const da = order[a.lineup_status] ?? 1;
      const db = order[b.lineup_status] ?? 1;
      if (da !== db) return da - db;
      return b.projected_points - a.projected_points;
    });
  const blockedCount = beforeFilterCount - allCandidates.length;

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
        ${blockedCount > 0 ? `<br><span style="color:var(--bad);">${blockedCount} candidate${blockedCount === 1 ? "" : "s"} hidden — their game has already started/finished today.</span>` : ""}
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
  // Only show 'Loading…' on the first paint when the panel is empty.
  // Subsequent re-renders (e.g. triggered by polling) keep the existing list
  // visible and swap atomically when the new HTML is ready, so the user
  // doesn't see a blank flash every 4 seconds.
  const out = $("#recs-out");
  if (!out.innerHTML.trim() || out.dataset.empty === "1") {
    out.innerHTML = `<div class="muted">Loading recommendations…</div>`;
  }
  try {
    const data = await api(`/api/drafts/${state.currentDraftId}/recommend?top_n=10`);
    const myTurn = isMyTurn(data.on_the_clock);
    const spJump = canJumpForSP();
    const nonSpJump = canJumpForNonSP();
    const lockFor = (slot) => {
      if (myTurn) return "";
      if (spJump && slot === "SP") return "";
      if (nonSpJump && slot !== "SP") return "";
      return "locked";
    };
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
            <td class="player-cell"><span class="name-trigger">${r.name} ${formBadge((r.components||{}).form_tag)}</span>${_oppFromComponents(r)}${projTooltip(r)}</td>
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
              drafter_override: ((b.dataset.slot === "SP" && canJumpForSP()) || (b.dataset.slot !== "SP" && canJumpForNonSP())) ? state.identity : undefined,
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
  // Only show 'Loading…' when the panel is empty. Subsequent re-renders keep
  // the existing pool visible and swap atomically when fresh data arrives.
  const out = $("#pool-out");
  if (!out.innerHTML.trim()) {
    out.innerHTML = `<div class="muted" style="padding:12px;">Loading available players…</div>`;
  }
  try {
    const data = await api(`/api/drafts/${state.currentDraftId}/pool`);
    poolCache = {
      draftId: state.currentDraftId,
      pool: data.pool,
      remainingByDrafter: data.remaining_by_drafter || {},
      nonSpFree: !!data.non_sp_free,
      hitterFreeDrafter: data.hitter_free_drafter || null,
    };
    populateTeamFilter();
    drawPool();
  } catch (e) {
    $("#pool-out").innerHTML = `<div class="muted" style="padding:12px;">${e.message}</div>`;
  }
}

function drawPool() {
  const search = ($("#pool-search").value || "").toLowerCase().trim();
  const filter = $("#pool-filter").value;
  const sortMode = $("#pool-sort")?.value || "proj";
  // Only a REAL on-the-clock turn unlocks every slot. The jump states
  // (spOnly / nonSpOpen) must stay restricted to their slot type below —
  // using the jump-inclusive _myTurnAtLastRender here let the lone SP-needer
  // pick ANY player instead of just SP (bug, 2026-06-24).
  const myTurn = state._isRealTurn;
  const spOnly = canJumpForSP();
  const nonSpOpen = canJumpForNonSP();
  // Two OOO unlock modes:
  //   1) I'm the lone SP-needer  → my SP pills unlock.
  //   2) Someone else is the lone SP-needer with only SPs left → my non-SP
  //      pills unlock (snake is just waiting on their pitcher picks).
  const lockFor = (slot) => {
    if (myTurn) return "";
    if (spOnly && slot === "SP") return "";
    if (nonSpOpen && slot !== "SP") return "";
    return "locked";
  };
  // {normalized_name -> rank} from the internal dynasty Top 500 (loaded once).
  // Lower rank = better. Case- and punctuation-insensitive match.
  const dynasty = state._dynastyMap || null;
  // Form-tag chips: show players whose form_tag matches ANY active chip (OR).
  // Empty set = no form filtering. Lets you e.g. surface only HOT+ELITE bats.
  const formTags = state._poolFormTags || new Set();
  const teamSel = $("#pool-team")?.value || "all";
  const rows = poolCache.pool.filter((p) => {
    if (search && !p.name.toLowerCase().includes(search)) return false;
    if (teamSel !== "all" && p.team_abbr !== teamSel) return false;
    if (formTags.size && !formTags.has((p.components || {}).form_tag)) return false;
    if (filter === "hitter") return p.role === "hitter";
    if (filter === "pitcher") return p.role === "pitcher";
    if (filter === "IF" || filter === "OF") return (p.position_slots || p.eligible_slots).includes(filter);
    return true;
  });
  // Apply sort. Default "proj" — preserve incoming pool order (already ranked
  // by projected_points desc on the backend).
  if (sortMode === "name") {
    rows.sort((a, b) => a.name.localeCompare(b.name));
  } else if (sortMode === "dynasty") {
    const big = 9999;
    const rankOf = (p) => {
      if (!dynasty) return big;
      return dynasty.lookup(p.name) ?? big;
    };
    rows.sort((a, b) => {
      const ra = rankOf(a), rb = rankOf(b);
      if (ra !== rb) return ra - rb;
      // Tiebreak: keep projection order so unranked players stay sensible.
      return (b.projected_points || 0) - (a.projected_points || 0);
    });
  }
  $("#pool-count").textContent = `${rows.length} available`;
  if (!rows.length) {
    $("#pool-out").innerHTML = `<div class="muted" style="padding:12px;">No matches.</div>`;
    return;
  }
  const rankOf = (p) => {
    if (!dynasty) return null;
    return dynasty.lookup(p.name) ?? null;
  };
  const html = `
    <table>
      <thead>
        <tr><th>Proj</th><th title="Dynasty rank from internal Top list (— = unranked)">Dyn</th><th>Player</th><th>Pos</th><th title="Lineup status: in posted lineup, OUT (scratched), or TBD (lineup not yet posted)">Status</th><th>Role</th><th>Statcast</th><th>Pick into…</th><th>Notes</th></tr>
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
            const dr = rankOf(p);
            const dynCell = dr == null
              ? `<span class="muted">—</span>`
              : `<span class="${dr <= 50 ? 'edge-pos' : ''}" style="font-variant-numeric:tabular-nums;">#${dr}</span>`;
            return `
        <tr class="${p.role} score-row">
          <td>${p.projected_points.toFixed(2)}</td>
          <td>${dynCell}</td>
          <td class="player-cell"><span class="name-trigger">${p.name} ${formBadge((p.components||{}).form_tag)} ${injuryBadge((p.components||{}).injury)}</span>${_oppFromComponents(p)}${projTooltip(p)}</td>
          <td>${p.position ?? "-"}</td>
          <td class="pool-status">${lineupBadge(p.lineup_status)}</td>
          <td>${p.role}</td>
          <td class="muted" style="font-size:11px;">${stat}</td>
          <td>${(() => {
            // In either free-for-all mode (non-SP or lone-hitter-needer),
            // eligible_slots is the union across all drafters. Filter to
            // slots THIS user actually has open.
            let pickable = p.eligible_slots;
            if ((poolCache.nonSpFree || poolCache.hitterFreeDrafter) && state.identity && poolCache.remainingByDrafter) {
              const mine = new Set(poolCache.remainingByDrafter[state.identity] || []);
              pickable = p.eligible_slots.filter((s) => mine.has(s));
            }
            if (!pickable.length) {
              return `<span class="slot-pill disabled">no slot left</span>`;
            }
            const tg = encodeAttrJSON(p.team_games_in_slate);
            return pickable
              .map((s) => `<span class="slot-pill ${lockFor(s)}" data-pid="${p.player_id}" data-slot="${s}" data-name="${escapeAttr(p.name)}" data-team-games="${tg}">${s}</span>`)
              .join("");
          })()}</td>
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
            drafter_override: ((el.dataset.slot === "SP" && canJumpForSP()) || (el.dataset.slot !== "SP" && canJumpForNonSP())) ? state.identity : undefined,
          }),
        });
      } catch (e) {
        alert(e.message);
      }
      await renderDraft();
    });
  });
}

// Fill the team dropdown with only the teams playing today (distinct in the
// pool), sorted by full name. Preserves the current selection across reloads.
function populateTeamFilter() {
  const sel = $("#pool-team");
  if (!sel) return;
  const prev = sel.value;
  const seen = new Map(); // abbr -> full name
  for (const p of poolCache.pool) {
    if (p.team_abbr && !seen.has(p.team_abbr)) seen.set(p.team_abbr, p.team_name || p.team_abbr);
  }
  const teams = [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  sel.innerHTML = `<option value="all">All teams</option>` +
    teams.map(([abbr, name]) => `<option value="${abbr}">${name} (${abbr})</option>`).join("");
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev; // keep selection
}

$("#pool-search").addEventListener("input", () => poolCache.pool.length && drawPool());
$("#pool-filter").addEventListener("change", () => poolCache.pool.length && drawPool());
$("#pool-team")?.addEventListener("change", () => poolCache.pool.length && drawPool());
$("#pool-sort")?.addEventListener("change", () => poolCache.pool.length && drawPool());

// Form-tag filter chips (HOT/ELITE/STEADY/COLD) — multi-select OR.
state._poolFormTags = state._poolFormTags || new Set();
document.querySelectorAll("#pool-form-chips .form-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const tag = chip.dataset.tag;
    if (state._poolFormTags.has(tag)) { state._poolFormTags.delete(tag); chip.classList.remove("active"); }
    else { state._poolFormTags.add(tag); chip.classList.add("active"); }
    const hint = $("#pool-form-hint");
    if (hint) hint.textContent = state._poolFormTags.size
      ? `showing ${[...state._poolFormTags].join(" / ")} only` : "";
    if (poolCache.pool.length) drawPool();
  });
});

// Load the internal dynasty Top 500 list once at startup. Tolerates failure —
// dynasty sort just falls back to projection order when the list isn't loaded.
async function _loadDynasty() {
  try {
    const r = await fetch("/api/dynasty_rankings");
    if (!r.ok) return;
    const data = await r.json();
    // Aggressive normalization to maximize matches against the 500-name CSV.
    // Strips: accents, punctuation, suffixes (Jr/Sr/II/III/IV), middle
    // initials, lowercases, collapses whitespace. Two players with the same
    // first+last (rare) keep the higher-ranked entry.
    const SUFFIXES = new Set(["jr", "sr", "ii", "iii", "iv"]);
    const norm = (s) => {
      // Decompose accents (Acuña → Acuna), strip combining marks, lowercase,
      // strip everything except letters/digits/spaces (drops .'`/-_ etc.).
      const cleaned = (s || "")
        .normalize("NFKD")
        .replace(/[̀-ͯ]/g, "")
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, " ");
      const tokens = cleaned.trim().split(/\s+/).filter(Boolean);
      // Drop suffix tokens (Jr/II) and lone-letter middle initials ('a')
      const kept = tokens.filter(t => !SUFFIXES.has(t) && t.length > 1);
      return kept.join(" ");
    };
    // Exact normalized-name match only. We previously had a (first-initial
    // + last-name) fallback to catch 'B. Witt' → 'Bobby Witt Jr.', but the
    // MLB API always returns full first names so the fallback's only real
    // effect was creating false positives between players who share both
    // a first initial AND last name:
    //   - 'Endy Rodríguez' → matched Julio Rodriguez's rank #9 (both 'j'+rod
    //     after the earlier last-name-only bug, fixed to first-letter, but
    //     'Jesus Rodriguez' is also 'j'+rodriguez and still collided)
    //   - Other plausible collisions: Jose/Jordan/Jason Ramirez, Luis Robert
    //     Sr/Jr, etc.
    // Better to underclaim and show players as 'unranked' than to mislabel.
    // Normalization is already aggressive (accents, suffixes, periods,
    // middle initials) so exact match catches all the legitimate cases.
    const map = new Map();
    (data.rankings || []).forEach((name, i) => {
      const k = norm(name);
      if (k && !map.has(k)) map.set(k, i + 1);
    });
    const lookup = (name) => {
      const k = norm(name);
      return (k && map.has(k)) ? map.get(k) : null;
    };
    state._dynastyMap = { map, norm, lookup, n: (data.rankings || []).length };
  } catch {}
}
// Deferred to first Draft-tab entry (see the tab handler) — only the draft
// pool consumes the dynasty-rank map, so no need to fetch it on every load.

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
            // Show '→ live X' only when there's actual remaining game time —
            // i.e. live_projected_total > total by a meaningful margin.
            // Once every game is final, live == actual and the arrow is noise.
            const hasLive = s.live_projected_total != null
              && Math.abs(s.live_projected_total - s.total) > 0.5;
            const liveProj = hasLive
              ? `<span class="muted" style="font-size:12px;"> → live ${s.live_projected_total.toFixed(2)}</span>`
              : "";
            return `<div class="row ${rankClass}"><span>${s.rank}. <b>${s.drafter}</b></span><span class="total">${s.total.toFixed(2)}${liveProj} <span class="muted">(full ${s.full_total.toFixed(2)})</span></span></div>`;
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
                    data-name="${escapeAttr(p.name)}"
                    title="Replace ${escapeAttr(p.name)}"><span class="rb-full">Replace</span><span class="rb-short">↻</span></button></td>`
              : `<td></td>`;
            const tooltip = renderBreakdownTooltip(p);
            // 2 decimals so per-pick actuals literally sum to the team total
            // (e.g. Blackburn 0.45 displays as 0.45, not rounded 0.5).
            // dataEmpty flag keeps the muted styling for the "no data yet"
            // dash; real numeric values use the bright Actual styling.
            const actualVal = p.actual === null ? "-" : p.actual.toFixed(2);
            const actualEmpty = p.actual === null;
            const stateLabel = (() => {
              const gs = (p.game_state || "").toLowerCase();
              if (!gs) return "";
              if (gs.includes("final")) return "Final";
              if (gs.includes("progress") || gs.includes("live") || gs.includes("delayed")) return "Live";
              if (gs.includes("pre") || gs.includes("warmup") || gs.includes("scheduled")) return "Pre";
              return p.game_state;
            })();
            // Live projection: only meaningful WHILE the game is in progress.
            //   - Final / Pre / no-game: render '—' (live_proj == actual once
            //     the game is over and == pre_game before the game starts,
            //     so the column would be redundant. Showing '—' is clearer).
            //   - Live: show the live projection with green/red color cue
            //     vs the original pre-game projection. Hover (or tap) shows
            //     the live math + the full pre-game factor breakdown so the
            //     user can audit how this number was derived.
            const lp = p.live_projection;
            const isLive = stateLabel === "Live" && p.played;
            const liveCls = !isLive ? "" :
              (lp > p.projected + 1.5 ? "live-up" :
               lp < p.projected - 1.5 ? "live-down" : "");
            const liveTT = isLive && lp != null ? liveProjTooltip(p) : "";
            const liveCell = isLive && lp != null
              ? `<td class="live-proj player-cell ${liveCls}"><span class="name-trigger" style="cursor:help;">${lp.toFixed(2)}</span>${liveTT}</td>`
              : `<td class="live-proj muted" style="text-align:center;">—</td>`;
            return `
          <tr class="${cls} score-row">
            <td>${p.slot}</td>
            <td title="${escapeAttr(p.name)}">${p.name} ${lineupTag} ${tag} ${promoted}</td>
            <td>${p.projected.toFixed(2)}</td>
            ${liveCell}
            <td class="player-cell"><span class="name-trigger${actualEmpty ? " actual-empty" : ""}">${actualVal}</span>${tooltip}</td>
            <td>${stateLabel}</td>
            ${replaceCell}
          </tr>`;
          },
        )
        .join("");
      const hasLiveTeam = s.live_projected_total != null
        && Math.abs(s.live_projected_total - s.total) > 0.5;
      const liveProjTotal = hasLiveTeam
        ? ` → <span class="muted">live ${s.live_projected_total.toFixed(2)}</span>`
        : "";
      return `
        <div class="standings">
          <h4 style="margin:0 0 6px;">${s.drafter} — ${s.total.toFixed(2)}${liveProjTotal}</h4>
          <table>
            <thead><tr><th>Slot</th><th>Player</th><th>Proj</th><th title="Actual + remaining projection estimate">Live</th><th>Actual</th><th>State</th><th></th></tr></thead>
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
    const myTurn = isMyTurn(data.on_the_clock) || canJumpForSP() || canJumpForNonSP();
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
  if (state.tab === "calib") await loadCalibration();
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

// ---------- Calibration tab ----------

let _calibSort = { col: "absdiff", dir: -1 };

function _yesterdayISO() {
  const d = new Date();
  d.setDate(d.getDate() - 1);
  return d.toISOString().slice(0, 10);
}

function _shiftDateISO(iso, days) {
  const d = new Date(iso + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

async function loadCalibration() {
  // Use the tab-local date if it's set; otherwise default to yesterday.
  const dateInput = $("#calib-date");
  if (dateInput && !dateInput.value) dateInput.value = _yesterdayISO();
  const d = dateInput?.value || _yesterdayISO();
  const out = $("#calib-out");
  out.innerHTML = `<div class="muted">Crunching ${d}… (fetches box scores per game, ~10s)</div>`;
  let data;
  try { data = await api(`/api/calibration?date=${d}`); }
  catch (e) { out.innerHTML = `<div class="muted">${e.message}</div>`; return; }
  renderCalibration(data);
}

// Wire the calibration controls once.
$("#calib-load")?.addEventListener("click", loadCalibration);
$("#calib-date")?.addEventListener("change", loadCalibration);
$("#calib-yesterday")?.addEventListener("click", () => {
  $("#calib-date").value = _yesterdayISO();
  loadCalibration();
});
$("#calib-prev")?.addEventListener("click", () => {
  const cur = $("#calib-date").value || _yesterdayISO();
  $("#calib-date").value = _shiftDateISO(cur, -1);
  loadCalibration();
});
$("#calib-next")?.addEventListener("click", () => {
  const cur = $("#calib-date").value || _yesterdayISO();
  $("#calib-date").value = _shiftDateISO(cur, 1);
  loadCalibration();
});

function _aggCard(label, a) {
  if (!a || !a.n) return `<div class="agg"><h4>${label}</h4><div class="muted">no data</div></div>`;
  const biasCls = a.bias > 0.5 ? "neg" : a.bias < -0.5 ? "pos" : "";
  const sign = a.bias >= 0 ? "+" : "";
  return `<div class="agg">
    <h4>${label} <span class="muted" style="font-weight:400;font-size:11px;">n=${a.n}</span></h4>
    <div class="agg-row"><span>Bias</span><span class="${biasCls}">${sign}${a.bias}</span></div>
    <div class="agg-row"><span>MAE</span><span>${a.mae}</span></div>
    <div class="agg-row muted"><span>avg proj</span><span>${a.mean_proj}</span></div>
    <div class="agg-row muted"><span>avg actual</span><span>${a.mean_actual}</span></div>
  </div>`;
}

function renderCalibration(data) {
  const out = $("#calib-out");
  const o = data.overall;
  if (!o.n) {
    out.innerHTML = `<div class="muted">No completed games on ${data.date} yet.</div>`;
    return;
  }
  const role = data.by_role;
  const tag = data.by_form_tag;
  const tier = data.by_qoc_tier;
  const aggs = `
    <div class="agg-grid">
      ${_aggCard("Overall", o)}
      ${_aggCard("Hitters", role.hitter)}
      ${_aggCard("Pitchers", role.pitcher)}
      ${_aggCard("🔥 HOT-tagged", tag.HOT)}
      ${_aggCard("🧊 COLD-tagged", tag.COLD)}
      ${_aggCard("📊 STEADY", tag.STEADY)}
      ${_aggCard("⭐ ELITE form", tag.ELITE)}
      ${_aggCard("Untagged", tag[""])}
      ${_aggCard("ELITE Statcast", tier.ELITE)}
      ${_aggCard("SOLID Statcast", tier.SOLID)}
      ${_aggCard("AVERAGE Statcast", tier.AVERAGE)}
      ${_aggCard("POOR Statcast", tier.POOR)}
    </div>`;

  const sorted = [...data.rows];
  const { col, dir } = _calibSort;
  sorted.sort((a, b) => {
    let av, bv;
    if (col === "absdiff") { av = Math.abs(a.diff); bv = Math.abs(b.diff); }
    else if (col === "name") { return dir * a.name.localeCompare(b.name); }
    else { av = a[col]; bv = b[col]; }
    return dir * (av - bv);
  });

  const headers = [
    ["name", "Player"],
    ["projected", "Proj"],
    ["actual", "Actual"],
    ["diff", "Diff"],
    ["absdiff", "|Diff|"],
  ];
  const ths = headers.map(([k, lbl]) => {
    const arrow = _calibSort.col === k ? (_calibSort.dir < 0 ? " ▼" : " ▲") : "";
    return `<th class="calib-th" data-col="${k}">${lbl}${arrow}</th>`;
  }).join("");

  const rowsHtml = sorted.map(r => {
    const cls = r.diff < 0 ? "neg" : "pos";
    const sign = r.diff >= 0 ? "+" : "";
    const formB = formBadge(r.form_tag);
    const tierB = r.qoc_tier && r.qoc_tier !== "—"
      ? `<span class="bench-tag" style="background:rgba(96,165,250,0.18);">${r.qoc_tier}</span>` : "";
    return `<tr class="${r.role}">
      <td>${r.name} ${formB} ${tierB}</td>
      <td>${r.projected.toFixed(2)}</td>
      <td>${r.actual.toFixed(2)}</td>
      <td class="${cls}"><b>${sign}${r.diff.toFixed(2)}</b></td>
      <td>${Math.abs(r.diff).toFixed(2)}</td>
    </tr>`;
  }).join("");

  out.innerHTML = `
    ${aggs}
    <h3 style="margin-top:18px;">Per-player (${sorted.length})</h3>
    <table id="calib-table">
      <thead><tr>${ths}</tr></thead>
      <tbody>${rowsHtml}</tbody>
    </table>`;

  out.querySelectorAll(".calib-th").forEach((th) => {
    th.style.cursor = "pointer";
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      if (_calibSort.col === col) _calibSort.dir *= -1;
      else { _calibSort.col = col; _calibSort.dir = -1; }
      renderCalibration(data);
    });
  });
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

// Per-table sort state for the Stats tab. Keyed by table id ("hitters" / "pitchers").
// Each entry is {col: <sort_key>, dir: "asc"|"desc"}.
const statsSort = {
  hitters:  { col: "total_picks", dir: "desc" },
  pitchers: { col: "total_picks", dir: "desc" },
};

function _statsPlayerSortValue(p, col, drafters) {
  switch (col) {
    case "name":         return (p.name || "").toLowerCase();
    case "position":     return (p.position || "").toLowerCase();
    case "total_picks":  return p.total_picks || 0;
    case "avg_per_pick": return p.avg_per_pick == null ? -Infinity : p.avg_per_pick;
    default:
      // pick_<drafter> / avg_<drafter> columns
      if (col.startsWith("pick_")) {
        const d = col.slice(5);
        return p.picks_by_drafter?.[d] || 0;
      }
      if (col.startsWith("avg_")) {
        const d = col.slice(4);
        const v = p.avg_per_drafter?.[d];
        return v == null ? -Infinity : v;
      }
      return 0;
  }
}

function renderStats(stand, players) {
  // Stash for re-render after sort clicks
  window._statsData = { stand, players };
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

  function renderPlayerTbl(title, list, tableKey) {
    const cols = drafters;
    const sortState = statsSort[tableKey];
    // Apply current sort
    const sorted = list.slice().sort((a, b) => {
      const av = _statsPlayerSortValue(a, sortState.col, cols);
      const bv = _statsPlayerSortValue(b, sortState.col, cols);
      const cmp = (av < bv) ? -1 : (av > bv) ? 1 : 0;
      return sortState.dir === "asc" ? cmp : -cmp;
    });
    // Build header with sort indicators
    const arrow = (col) => {
      if (sortState.col !== col) return `<span class="stats-sort-arrow muted">↕</span>`;
      return `<span class="stats-sort-arrow active">${sortState.dir === "asc" ? "▲" : "▼"}</span>`;
    };
    const th = (col, label) =>
      `<th class="stats-sortable" data-stats-sort="${col}" data-stats-table="${tableKey}">${label} ${arrow(col)}</th>`;

    const head = `<tr>
      ${th("name", title)}
      ${th("position", "Pos")}
      ${cols.map((d) => th(`pick_${d}`, d)).join("")}
      ${th("total_picks", "Total")}
      ${th("avg_per_pick", "Avg")}
      ${cols.map((d) => th(`avg_${d}`, `${d} Avg`)).join("")}
    </tr>`;
    const body = sorted
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
    return `<h3 style="margin-top:24px;">${title} — top ${list.length} by pick volume <span class="muted" style="font-weight:400;font-size:12px;">(click any column header to sort)</span></h3>
      <div style="max-height:420px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;">
        <table class="stats-player-table"><thead>${head}</thead><tbody>${body}</tbody></table>
      </div>`;
  }

  $("#stats-out").innerHTML =
    recordsTbl +
    perDayTbl +
    renderPlayerTbl("Hitters", players.hitters, "hitters") +
    renderPlayerTbl("Pitchers", players.pitchers, "pitchers");
}

// Delegated click handler for sortable headers on the Stats tab player tables.
// Clicking the same column toggles asc/desc; clicking a new column resets to
// desc (highest first — what you usually want for points/picks/averages).
document.addEventListener("click", (e) => {
  const th = e.target.closest && e.target.closest(".stats-sortable");
  if (!th) return;
  const tableKey = th.dataset.statsTable;
  const col = th.dataset.statsSort;
  if (!tableKey || !col) return;
  const cur = statsSort[tableKey];
  if (cur.col === col) {
    cur.dir = cur.dir === "asc" ? "desc" : "asc";
  } else {
    cur.col = col;
    cur.dir = "desc";
  }
  // Re-render with the cached data
  if (window._statsData) {
    renderStats(window._statsData.stand, window._statsData.players);
  }
});

// ---------- Schedule tab ----------

let scheduleResult = null;

function _toSunday(iso) {
  const d = new Date(iso + "T12:00:00Z");
  // getUTCDay: Sun=0..Sat=6. Roll back to Sunday.
  d.setUTCDate(d.getUTCDate() - d.getUTCDay());
  return d.toISOString().slice(0, 10);
}

function _isoDate(d) { return d.toISOString().slice(0, 10); }

function _shortMonthDay(iso) {
  const d = new Date(iso + "T12:00:00Z");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

let _schedAnchorSunday = null;   // The Sunday at the LEFT of the visible chip strip.
let _schedDraftDates = new Set(); // ISO dates of existing drafts.
const SCHED_CHIP_COUNT = 5;

function _weekHasDrafts(sundayIso) {
  // A week is "built" if a draft exists for any day Sun-Thu of that week.
  const sun = new Date(sundayIso + "T12:00:00Z");
  for (let i = 0; i < 5; i++) {
    const d = new Date(sun);
    d.setUTCDate(sun.getUTCDate() + i);
    if (_schedDraftDates.has(_isoDate(d))) return true;
  }
  return false;
}

function _renderSundayChips() {
  if (!_schedAnchorSunday) return;
  const host = $("#sched-sundays");
  if (!host) return;
  const selected = $("#sched-start").value;
  const todaySunday = _toSunday(_isoDate(new Date()));
  const anchor = new Date(_schedAnchorSunday + "T12:00:00Z");
  const chips = [];
  for (let i = 0; i < SCHED_CHIP_COUNT; i++) {
    const d = new Date(anchor);
    d.setUTCDate(anchor.getUTCDate() + i * 7);
    const iso = _isoDate(d);
    const label = iso === todaySunday ? "This wk"
      : iso === _isoDate(new Date(new Date(todaySunday + "T12:00:00Z").getTime() + 7*86400000)) ? "Next wk"
      : _shortMonthDay(iso);
    const sel = iso === selected ? "selected" : "";
    const built = _weekHasDrafts(iso) ? "built" : "";
    const builtTag = built ? `<span class="built-tag" title="Drafts already created for this week">✓ built</span>` : "";
    chips.push(`<div class="sunday-chip ${sel} ${built}" data-iso="${iso}">
      <span class="label">${label}</span>
      ${label === "This wk" || label === "Next wk" ? `<span class="sub">${_shortMonthDay(iso)}</span>` : ""}
      ${builtTag}
    </div>`);
  }
  host.innerHTML = chips.join("");
  host.querySelectorAll(".sunday-chip").forEach((el) => {
    el.addEventListener("click", async () => {
      $("#sched-start").value = el.dataset.iso;
      _renderSundayChips();
      // If this week is marked "built", auto-load the existing drafts so
      // the user can SEE what was built without clicking Build manually.
      // We pin each saved day's game_pks as locked_days so the rendered
      // preview exactly matches what's on disk.
      if (el.classList.contains("built")) {
        await _loadBuiltWeek(el.dataset.iso);
      }
    });
  });
}

// Tracks whether the current preview corresponds to a built week. When set,
// a "💾 Save changes to drafts" button appears once the user makes any
// swap — it pushes the new game_pks to each affected draft via
// /api/drafts/{date}/games (which doesn't touch picks).
let scheduleBuiltWeek = null;       // sundayIso when a built week is loaded
let scheduleOriginalLocks = {};     // {date: [pks]} snapshot at load time

// Fetch every existing draft for the Sun-Thu of a given week, build a
// locked_days payload from their saved game_pks, then call rebuildSchedule
// so the preview reads the exact same games that were saved.
async function _loadBuiltWeek(sundayIso) {
  const sun = new Date(sundayIso + "T12:00:00Z");
  const dayDates = [];
  for (let i = 0; i < 5; i++) {
    const d = new Date(sun);
    d.setUTCDate(sun.getUTCDate() + i);
    dayDates.push(_isoDate(d));
  }
  scheduleLocks = {};
  // Pull each existing draft in parallel; ignore failures (gap days).
  const results = await Promise.all(dayDates.map(async (date) => {
    if (!_schedDraftDates.has(date)) return null;
    try {
      const dr = await api(`/api/drafts/${date}`);
      const game_pks = (dr.game_pks || []);
      return game_pks.length ? { date, game_pks } : null;
    } catch { return null; }
  }));
  for (const r of results) if (r) scheduleLocks[r.date] = r.game_pks;
  scheduleBuiltWeek = sundayIso;
  // Deep-copy so we can detect drift later
  scheduleOriginalLocks = {};
  for (const [k, v] of Object.entries(scheduleLocks)) scheduleOriginalLocks[k] = [...v];
  $("#sched-out").innerHTML = `<div class="muted">Loading built schedule for ${sundayIso}…</div>`;
  await rebuildSchedule(sundayIso);
}

// True if any day's game-pk set has changed from what was on-disk at load time.
function _scheduleHasUnsavedChanges() {
  if (!scheduleBuiltWeek || !scheduleResult) return false;
  for (const day of (scheduleResult.days || [])) {
    if (day.past) continue;  // past days never count
    const currentPks = (day.selected_games || []).map(g => g.gamePk).sort();
    const originalPks = [...(scheduleOriginalLocks[day.date] || [])].sort();
    if (currentPks.length !== originalPks.length) return true;
    for (let i = 0; i < currentPks.length; i++) {
      if (currentPks[i] !== originalPks[i]) return true;
    }
  }
  return false;
}

// Push the current preview's game_pks for each non-past day back to its
// existing draft via /api/drafts/{date}/games. Picks are NOT modified —
// the endpoint only updates game_pks. Days with no on-disk draft get
// skipped (would need to be created via the apply flow instead).
async function _saveScheduleChanges() {
  if (!scheduleResult) return;
  const out = $("#sched-out");
  const ops = [];
  for (const day of (scheduleResult.days || [])) {
    if (day.past) continue;
    if (day.skipped) continue;  // user removed this day — leave any existing draft alone
    if (!_schedDraftDates.has(day.date)) continue;  // no saved draft to update
    const game_pks = (day.selected_games || []).map(g => g.gamePk);
    ops.push({ date: day.date, game_pks });
  }
  if (!ops.length) return alert("No editable days to save.");
  // Run sequentially so errors surface clearly
  const results = [];
  for (const op of ops) {
    try {
      await api(`/api/drafts/${op.date}/games`, {
        method: "POST",
        body: JSON.stringify({ game_pks: op.game_pks }),
      });
      results.push({ ok: true, date: op.date });
    } catch (e) {
      results.push({ ok: false, date: op.date, error: e.message || String(e) });
    }
  }
  const ok = results.filter(r => r.ok).length;
  const fails = results.filter(r => !r.ok);
  // Refresh the original-locks snapshot so the button hides again
  scheduleOriginalLocks = {};
  for (const [k, v] of Object.entries(scheduleLocks)) scheduleOriginalLocks[k] = [...v];
  let msg = `Saved ${ok}/${ops.length} day${ops.length === 1 ? "" : "s"}.`;
  if (fails.length) msg += " Failed: " + fails.map(f => `${f.date} (${f.error})`).join(", ");
  // Re-render to drop the save banner
  renderSchedule(scheduleResult);
  // Add a transient confirmation atop
  out.insertAdjacentHTML("afterbegin", `<div class="muted" style="margin-bottom:8px;color:var(--accent-2);">✓ ${msg}</div>`);
}

async function _refreshSchedDrafts() {
  try {
    const data = await api(`/api/drafts`);
    _schedDraftDates = new Set((data.drafts || []).filter(d => /^\d{4}-\d{2}-\d{2}$/.test(d)));
  } catch {}
}

async function initScheduleTab() {
  const today = $("#date").value || _isoDate(new Date());
  const sun = _toSunday(today);
  if (!$("#sched-start").value) $("#sched-start").value = sun;
  if (!_schedAnchorSunday) {
    const anchor = new Date(sun + "T12:00:00Z");
    anchor.setUTCDate(anchor.getUTCDate() - 7);
    _schedAnchorSunday = _isoDate(anchor);
  }
  await _refreshSchedDrafts();
  _renderSundayChips();
}

$("#sched-prev")?.addEventListener("click", () => {
  if (!_schedAnchorSunday) return;
  const a = new Date(_schedAnchorSunday + "T12:00:00Z");
  a.setUTCDate(a.getUTCDate() - 7 * SCHED_CHIP_COUNT);
  _schedAnchorSunday = _isoDate(a);
  _renderSundayChips();
});
$("#sched-next")?.addEventListener("click", () => {
  if (!_schedAnchorSunday) return;
  const a = new Date(_schedAnchorSunday + "T12:00:00Z");
  a.setUTCDate(a.getUTCDate() + 7 * SCHED_CHIP_COUNT);
  _schedAnchorSunday = _isoDate(a);
  _renderSundayChips();
});

$("#sched-build").addEventListener("click", async () => {
  let start = $("#sched-start").value;
  if (!start) return alert("Pick a week-start (Sunday).");
  start = _toSunday(start);
  $("#sched-start").value = start;
  $("#sched-out").innerHTML = `<div class="muted">Building (this fetches each day's slate from MLB)…</div>`;
  // Reset locks AND built-week context when building a fresh week so the
  // Save Changes button doesn't appear on a freshly-built (unsaved) preview.
  scheduleLocks = {};
  scheduleSkips = new Set();
  scheduleBuiltWeek = null;
  scheduleOriginalLocks = {};
  await rebuildSchedule(start);
});

// {date: [gamePk, gamePk, ...]} — user's manually-pinned game choices that
// override the greedy filler. Persists across rebuilds within the same week
// so subsequent click-swaps don't undo prior swaps.
let scheduleLocks = {};
// Set of ISO dates the user has removed from this week's schedule entirely
// (e.g. "we're not drafting Sunday"). Remaining days rebalance team counts
// around the absence.
let scheduleSkips = new Set();

async function rebuildSchedule(start) {
  try {
    const lockedDays = Object.entries(scheduleLocks).map(([date, pks]) => ({date, game_pks: pks}));
    const parts = [`start=${start}`];
    if (lockedDays.length) {
      parts.push(`locked_days=${encodeURIComponent(JSON.stringify(lockedDays))}`);
    }
    if (scheduleSkips.size) {
      parts.push(`skipped_days=${encodeURIComponent(JSON.stringify([...scheduleSkips]))}`);
    }
    const data = await api(`/api/schedule_builder?${parts.join("&")}`);
    scheduleResult = data;
    renderSchedule(data);
    $("#sched-apply-row").hidden = false;
  } catch (e) {
    $("#sched-out").innerHTML = `<div class="muted">${e.message}</div>`;
  }
}

function renderSchedule(data) {
  const days = data.days
    .map((day) => {
      // Past days (already played) get a permanent badge + no edit controls.
      // The backend auto-locks them based on the saved draft's game_pks so
      // they're treated as fixed history for the team-count balancer.
      const isPast = !!day.past;
      const isSkipped = !!day.skipped;
      // Skipped days render as a compact placeholder with a "Restore" button.
      // No chips, no day-part toggles — the day is removed from the week and
      // contributes nothing to team counts.
      if (isSkipped) {
        return `<div class="sched-day skipped" data-date="${day.date}" style="opacity:0.6;">
          <h4 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <span>${day.date} <span class="muted" style="font-weight:400;">— ❌ removed from week</span></span>
            <button class="sched-restore-day" data-date="${day.date}" title="Add this day back to the schedule">↩️ Restore</button>
          </h4>
        </div>`;
      }
      const pastNote = isPast
        ? ` <span class="muted" style="font-size:11px;color:var(--accent-2);">— ✓ played (locked)</span>`
        : (scheduleLocks[day.date]
            ? ` <span class="muted" style="font-size:11px;">— 🔒 swapped</span>`
            : "");
      // Per-day "🌙 Late" / "☀️ Early" — shown only if this day has more
      // games scheduled than the slate cap AND it's not in the past. Past
      // days can't be retroactively edited.
      const hasAlternates = (day.all_games || []).length > day.selected_games.length;
      const dayActionBtns = (!isPast && hasAlternates)
        ? `<button class="sched-early-day" data-date="${day.date}" title="Lock this day to the 6 earliest start-times; rest of the week rebalances">☀️ Early</button>
           <button class="sched-late-day" data-date="${day.date}" title="Lock this day to the 6 latest start-times; rest of the week rebalances">🌙 Late</button>`
        : "";
      // Skip button — shown for any non-past day, even ones without alternates,
      // so the user can take the day out of the week entirely (e.g. "no draft
      // Sunday"). Team counts rebalance over the remaining days.
      const skipBtn = !isPast
        ? `<button class="sched-skip-day" data-date="${day.date}" title="Remove this day from the schedule entirely; remaining days rebalance">❌ Skip</button>`
        : "";
      // Sort chips by start time ascending so the row reads naturally —
      // earliest first pitch on the left, latest on the right. The backend
      // returns games in pick-order (matchup-uniqueness + team-count) which
      // is logically correct for the algorithm but confusing to scan.
      const chipsSorted = [...day.selected_games].sort((a, b) =>
        (a.gameDate || "").localeCompare(b.gameDate || "")
      );
      const chips = chipsSorted
        .map((g) => {
          const t = _fmtETTime(g.gameDate);
          const isDay = _isDayGameET(g.gameDate);
          // Past days: chips render but aren't clickable (no swap allowed).
          const baseCls = isPast
            ? "matchup-chip past"
            : (isDay ? "matchup-chip clickable day" : "matchup-chip clickable night");
          const titleTxt = isPast
            ? `${g.away_sp} vs ${g.home_sp} · ${t || "?"} ET · this day is already played`
            : `${g.away_sp} vs ${g.home_sp} · ${t || "?"} ET · click to swap`;
          return `<span class="${baseCls}"
              data-date="${day.date}" data-gamepk="${g.gamePk}"
              title="${titleTxt}">
              <span class="time">${t || ""}</span>
              <span class="teams">${g.away_abbr} @ ${g.home_abbr}</span>
            </span>`;
        })
        .join("");
      const nDay = day.selected_games.filter(g => _isDayGameET(g.gameDate)).length;
      const nNight = day.selected_games.length - nDay;
      const mix = day.selected_games.length
        ? ` <span class="muted" style="font-size:11px;">(${nDay} day · ${nNight} night)</span>` : "";
      return `<div class="sched-day" data-date="${day.date}">
        <h4 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <span>${day.date} <span class="muted" style="font-weight:400;">— ${day.selected_games.length} games</span>${mix}${pastNote}</span>
          ${dayActionBtns}
          ${skipBtn}
        </h4>
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
  // Per-day "🌙 Late" buttons live on each day header now (was a single
  // top-level "Late Sunday" — too narrow). Reset locks button still here.
  const lockedCount = Object.keys(scheduleLocks).length;
  const resetLocksBtn = lockedCount
    ? `<button id="sched-reset-locks" title="Clear all locks and let every day rebalance" style="background:rgba(239,68,68,0.1);border-color:var(--bad);color:var(--bad);">↺ Reset ${lockedCount} lock${lockedCount === 1 ? "" : "s"}</button>`
    : "";
  // Save button — shown when looking at a built week AND the preview has
  // drifted from what's on-disk. Saves each non-past day's new game_pks
  // back to its existing draft (picks are preserved).
  const showSave = _scheduleHasUnsavedChanges();
  const saveBtn = showSave
    ? `<button id="sched-save-changes" title="Push the swapped games back to each draft for this week (picks stay)" style="background:var(--accent-2);color:#000;font-weight:700;">💾 Save changes to drafts</button>`
    : "";

  $("#sched-out").innerHTML = `
    <div class="muted" style="margin-bottom:8px;font-size:12px;display:flex;align-items:center;flex-wrap:wrap;gap:6px;">
      <span>💡 Click any game to swap it. Hit 🌙 Late on any day to pin that day's 6 latest games; downstream days rebalance around your locks.</span>
      ${resetLocksBtn}
      ${saveBtn}
    </div>
    ${days}
    <div class="team-counts">
      <div class="muted" style="margin-bottom:6px;">Team appearances after applying this schedule (green = lowest, orange = highest, range ${min}–${max}):</div>
      <div class="row">${teamRow}</div>
    </div>`;
  // Wire click-to-swap on every chip
  document.querySelectorAll("#sched-out .matchup-chip.clickable").forEach(chip => {
    chip.addEventListener("click", () => openSwapModal(chip.dataset.date, parseInt(chip.dataset.gamepk, 10)));
  });
  $("#sched-save-changes")?.addEventListener("click", _saveScheduleChanges);
  // Wire Reset Locks — clears every manual override and re-runs the
  // builder so every day picks greedily again with the day-game tiebreaker.
  $("#sched-reset-locks")?.addEventListener("click", () => {
    scheduleLocks = {};
    $("#sched-out").innerHTML = `<div class="muted">Resetting locks and rebuilding…</div>`;
    rebuildSchedule($("#sched-start").value);
  });
  // Per-day daypart locks — '🌙 Late' picks the 6 latest start-times,
  // '☀️ Early' picks the 6 earliest. Both lock the day and trigger a
  // cascading rebuild so the rest of the week rebalances around it.
  function _wireDaypartLock(selector, ascending) {
    document.querySelectorAll(selector).forEach(btn => {
      btn.addEventListener("click", () => {
        const dt = btn.dataset.date;
        const day = (data.days || []).find(d => d.date === dt);
        if (!day || !day.all_games) return;
        const sorted = [...day.all_games]
          .filter(g => g.gameDate)
          .sort((a, b) => ascending
            ? (a.gameDate || "").localeCompare(b.gameDate || "")
            : (b.gameDate || "").localeCompare(a.gameDate || ""));
        if (!sorted.length) return alert(`No games with valid start times on ${dt}.`);
        const picks = sorted.slice(0, 6).map(g => g.gamePk);
        scheduleLocks[dt] = picks;
        const label = ascending ? "early" : "late";
        $("#sched-out").innerHTML = `<div class="muted">Pinning ${picks.length} ${label} game${picks.length === 1 ? "" : "s"} on ${dt} and rebalancing the rest of the week…</div>`;
        rebuildSchedule($("#sched-start").value);
      });
    });
  }
  _wireDaypartLock(".sched-late-day", false);
  _wireDaypartLock(".sched-early-day", true);
  // Per-day skip/restore — toggles a day in/out of the week. Skipped days
  // contribute no team-count signal, so the rest of the week rebalances
  // around the absence on the next rebuild.
  document.querySelectorAll(".sched-skip-day").forEach(btn => {
    btn.addEventListener("click", () => {
      const dt = btn.dataset.date;
      scheduleSkips.add(dt);
      // Clear any matchup lock on the skipped day — it's not picking games
      // anyway and the stale lock would resurrect on restore.
      delete scheduleLocks[dt];
      $("#sched-out").innerHTML = `<div class="muted">Removing ${dt} and rebalancing the rest of the week…</div>`;
      rebuildSchedule($("#sched-start").value);
    });
  });
  document.querySelectorAll(".sched-restore-day").forEach(btn => {
    btn.addEventListener("click", () => {
      const dt = btn.dataset.date;
      scheduleSkips.delete(dt);
      $("#sched-out").innerHTML = `<div class="muted">Restoring ${dt} and rebalancing…</div>`;
      rebuildSchedule($("#sched-start").value);
    });
  });
}

// Format an ISO-UTC timestamp from MLB schedule into '7:05p ET' style. Returns
// "" for missing or invalid input. Uses Intl.DateTimeFormat for proper TZ math
// (handles DST correctly — EST/EDT boundary in March/November).
function _fmtETTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    const s = d.toLocaleTimeString("en-US", {
      hour: "numeric", minute: "2-digit", hour12: true, timeZone: "America/New_York",
    });
    // "7:05 PM" → "7:05p"
    return s.replace(/\s?AM/, "a").replace(/\s?PM/, "p");
  } catch { return ""; }
}
// Day game = first pitch before 5 PM ET. Matches the backend's _is_day_game
// heuristic which uses hour < 22 UTC ≈ 6 PM ET; we use 17 ET for the chip
// tag to be slightly stricter (matinees + early afternoons are clearly day).
function _isDayGameET(iso) {
  if (!iso) return false;
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return false;
    const hourStr = d.toLocaleString("en-US", {
      hour: "numeric", hour12: false, timeZone: "America/New_York",
    });
    const h = parseInt(hourStr, 10);
    return h < 17;
  } catch { return false; }
}

// Modal: swap one game in a day for another game from that day's full schedule.
// Shows all candidate games for the date with a "pick" button each; on pick,
// locks the new game and re-runs the schedule builder.
function openSwapModal(date, currentGamePk) {
  const day = (scheduleResult.days || []).find(d => d.date === date);
  if (!day) return;
  const currentGames = day.selected_games || [];
  const allGames = day.all_games || [];
  const inSlate = new Set(currentGames.map(g => g.gamePk));
  const candidates = allGames.filter(g => !inSlate.has(g.gamePk));
  if (!candidates.length) {
    alert(`Only ${currentGames.length} games scheduled on ${date} — no alternates to swap in.`);
    return;
  }
  const current = currentGames.find(g => g.gamePk === currentGamePk);
  // Sort candidates: day games first, then by start time so the modal makes
  // it easy to find a matinee replacement
  const sortedCandidates = [...candidates].sort((a, b) => {
    const da = _isDayGameET(a.gameDate) ? 0 : 1;
    const db = _isDayGameET(b.gameDate) ? 0 : 1;
    if (da !== db) return da - db;
    return (a.gameDate || "").localeCompare(b.gameDate || "");
  });
  const modal = $("#changelog-modal");
  const body = $("#changelog-body");
  $("#changelog-modal .modal-header h2").textContent = `Swap game on ${date}`;
  body.innerHTML = `
    <div style="margin-bottom:10px;">
      <div class="muted" style="font-size:12px;">Removing:</div>
      <div style="font-size:14px;font-weight:600;">${current ? `${current.away_abbr} @ ${current.home_abbr}` : currentGamePk}
        <span class="muted" style="font-weight:400;font-size:12px;"> — ${_fmtETTime(current?.gameDate) || "?"} ET · ${current?.away_sp || "?"} vs ${current?.home_sp || "?"}</span></div>
    </div>
    <div class="muted" style="font-size:12px;margin-bottom:6px;">Pick a replacement (${sortedCandidates.length} alternates available, day games first):</div>
    <div style="display:flex;flex-direction:column;gap:6px;">
      ${sortedCandidates.map(g => {
        const t = _fmtETTime(g.gameDate);
        const isDay = _isDayGameET(g.gameDate);
        const dayTag = isDay ? `<span style="background:rgba(251,191,36,0.18);color:#fbbf24;padding:1px 6px;border-radius:4px;font-size:10px;margin-right:6px;">DAY</span>` : "";
        return `
        <button class="btn-pick sched-swap-pick" data-date="${date}" data-newpk="${g.gamePk}"
                style="text-align:left;display:flex;justify-content:space-between;align-items:center;">
          <span>${dayTag}<span style="display:inline-block;width:48px;font-variant-numeric:tabular-nums;">${t || ""}</span>
            <b>${g.away_abbr} @ ${g.home_abbr}</b>
            <span class="muted" style="font-size:11px;margin-left:6px;">${g.away_sp} vs ${g.home_sp}</span></span>
          <span class="muted" style="font-size:11px;">${g.status || ""}</span>
        </button>`;
      }).join("")}
    </div>`;
  modal.style.display = "flex";
  document.querySelectorAll(".sched-swap-pick").forEach(btn => {
    btn.addEventListener("click", async () => {
      const d = btn.dataset.date;
      const newPk = parseInt(btn.dataset.newpk, 10);
      // Build the new locked game list for this day: keep all current games
      // EXCEPT the one being swapped, then add the new one.
      const remaining = currentGames.filter(g => g.gamePk !== currentGamePk).map(g => g.gamePk);
      scheduleLocks[d] = [...remaining, newPk];
      modal.style.display = "none";
      $("#sched-out").innerHTML = `<div class="muted">Rebalancing the week around your swap…</div>`;
      await rebuildSchedule($("#sched-start").value);
    });
  });
}

async function _applySchedule(force) {
  const drafters = $("#sched-drafters").value
    .split(",").map((s) => s.trim()).filter(Boolean);
  if (drafters.length < 2) return alert("Need at least 2 drafters.");
  const randomize = $("#sched-randomize").checked;
  const days = scheduleResult.days
    .filter((d) => !d.skipped)
    .map((d) => ({
      date: d.date,
      game_pks: d.selected_games.map((g) => g.gamePk),
    }));
  const out = $("#sched-apply-out");
  out.textContent = force ? "Overwriting drafts…" : "Creating drafts…";
  const data = await api(`/api/schedule_builder/apply`, {
    method: "POST",
    body: JSON.stringify({ drafters, days, randomize_order: randomize, force_overwrite: !!force }),
  });
  // Categorize the response
  const created = data.created || [];
  const overwritten = data.overwritten || [];
  const skipped = data.skipped || [];
  // Days that were skipped specifically because picks already exist —
  // those can be retried with force_overwrite=true.
  const conflicts = skipped.filter(s => (s.had_picks || 0) > 0);
  let html = "";
  if (created.length) html += `Created ${created.length} draft${created.length === 1 ? "" : "s"}. `;
  if (overwritten.length) html += `Overwrote ${overwritten.length} (lost ${overwritten.reduce((a, b) => a + (b.lost_picks || 0), 0)} picks). `;
  if (skipped.length) html += `Skipped ${skipped.length}. `;
  if (conflicts.length && !force) {
    const datesList = conflicts.map(c => `${c.date} (${c.had_picks} picks)`).join(", ");
    html += `<br><span style="color:var(--warn);">⚠ ${conflicts.length} day${conflicts.length === 1 ? "" : "s"} already have picks: ${datesList}.</span>
      <br><button id="sched-force" class="btn-danger" style="margin-top:6px;">↻ Overwrite anyway (wipes those ${conflicts.reduce((a, b) => a + (b.had_picks || 0), 0)} picks)</button>`;
  } else if (created.length + overwritten.length) {
    html += `<br><a href="#" id="sched-go-draft">Switch to Draft tab</a> to load any of them.`;
  }
  out.innerHTML = html || "No changes.";
  $("#sched-go-draft")?.addEventListener("click", (e) => {
    e.preventDefault();
    document.querySelector('nav button[data-tab="draft"]').click();
  });
  $("#sched-force")?.addEventListener("click", async () => {
    if (!confirm("Wipe existing picks and rebuild these days? This can't be undone.")) return;
    await _applySchedule(true);
  });
  await loadDraftList();
}

$("#sched-apply").addEventListener("click", async () => {
  if (!scheduleResult) return alert("Build a schedule first.");
  try {
    await _applySchedule(false);
  } catch (e) {
    $("#sched-apply-out").textContent = e.message;
  }
});
refresh();


// ---------- Trade-Deadline Draft ----------
const MLB_TEAMS = ["ATH","ATL","AZ","BAL","BOS","CHC","CIN","CLE","COL","CWS","DET","HOU","KC","LAA","LAD","MIA","MIL","MIN","NYM","NYY","PHI","PIT","SD","SEA","SF","STL","TB","TEX","TOR","WSH"];
let dlPool = [];
let dlState = null;
let dlExtra = [];      // league-wide search hits (non-pool players)
let dlExtraQ = "";
let dlSearchTimer = null;

async function loadDeadline() {
  try {
    const [dr, pool] = await Promise.all([
      api("/api/deadline/draft"),
      api("/api/deadline/candidates"),
    ]);
    dlState = dr;
    dlPool = pool.candidates || [];
    const teamSel = $("#dl-team");
    if (teamSel) {
      const prev = teamSel.value;
      const teams = [...new Set(dlPool.map((c) => c.team).filter(Boolean))].sort();
      teamSel.innerHTML = `<option value="all">All teams</option>` +
        teams.map((t) => `<option value="${t}">${t}</option>`).join("");
      if ([...teamSel.options].some((o) => o.value === prev)) teamSel.value = prev;
    }
    $("#dl-pool-meta").textContent = pool.as_of
      ? `— ${dlPool.length} candidates · research as of ${pool.as_of}`
      : "— candidate list not loaded yet";
    $("#dl-setup").style.display = dr.exists ? "none" : "flex";
    renderDeadlineBoard();
    renderDeadlinePool();
  } catch (e) {
    window._dlWireIdentity = () => {
    const sel = $("#dl-identity");
    if (sel) sel.onchange = () => { setIdentity(sel.value); renderDeadlineBoard(); renderDeadlinePool(); };

  };
  $("#dl-board").innerHTML = `<div class="muted">${e.message}</div>`;
  }
}

function renderDeadlineBoard() {
  const dr = dlState;
  if (!dr || !dr.exists) {
    window._dlWireIdentity = () => {
    const sel = $("#dl-identity");
    if (sel) sel.onchange = () => { setIdentity(sel.value); renderDeadlineBoard(); renderDeadlinePool(); };

  };
  $("#dl-board").innerHTML = `<div class="muted" style="padding:8px 0;">No deadline draft yet — enter drafters above and start.</div>`;
    return;
  }
  const totals = Object.entries(dr.totals || {}).sort((a, b) => b[1] - a[1]);
  const board = totals.map(([d, pts], i) =>
    `<div class="m" style="display:inline-block;margin-right:14px;"><b>${i + 1}. ${d}</b> — ${pts.toFixed(1)} pts</div>`).join("");
  const otc = dr.on_the_clock;
  // Snake-order strip, same component as the daily draft: this round's
  // order with the on-clock drafter highlighted and already-picked (this
  // round) drafters struck through.
  const nD = dr.drafters.length;
  const nPicks = (dr.picks || []).length;
  const round = Math.floor(nPicks / nD);
  const rOrder = round % 2 === 0 ? dr.drafters : [...dr.drafters].reverse();
  const pickedThisRound = new Set((dr.picks || []).slice(round * nD).map((p) => p.drafter));
  const stripRows = rOrder.map((d) => {
    let cls = "";
    if (d === otc) cls = "on-clock";
    else if (pickedThisRound.has(d)) cls = "done";
    return `<div class="drafter-strip-row ${cls}">${d}</div>`;
  }).join("");
  const strip = otc ? `<div class="draft-strip" style="margin:10px 0;">
      <div class="strip-round">Round ${round + 1} / ${dr.rounds}</div>
      ${stripRows}
    </div>` : "";
  // Identity — shared with the daily draft (same people, one "who am I").
  const idOpts = ["", ...dr.drafters].map((d) =>
    `<option value="${d}" ${state.identity === d ? "selected" : ""}>${d || "— Who are you? —"}</option>`).join("");
  const myTurn = otc && state.identity === otc;
  const idBar = `<div class="setup-row" style="margin:8px 0;">
      <label class="muted" style="font-size:12px;">You are:
        <select id="dl-identity">${idOpts}</select></label>
      ${otc && state.identity && state.identity !== otc
        ? `<span class="muted" style="font-size:12px;">⏳ waiting for <b>${otc}</b> to pick</span>` : ""}
      ${otc && !state.identity
        ? `<span class="muted" style="font-size:12px;">identify yourself to pick</span>` : ""}
    </div>
    `;
  const rows = (dr.picks || []).map((p) => {
    const status = p.traded
      ? `✅ traded → ${p.traded_to || "?"} ${p.hit_team ? "🎯" : ""} <b>+${p.points.toFixed(1)}</b>`
      : `⏳`;
    const badges = `${p.has_allstar ? "⭐" : ""}${p.has_top3_voting ? "🏅" : ""}`;
    return `<tr><td>${p.pick_number}</td><td>${p.drafter}</td>
      <td>${p.player_name} ${badges} <span class="muted">(${p.position || "?"} · ${p.team || "?"})</span></td>
      <td>→ ${p.predicted_team}</td><td>${status}</td></tr>`;
  }).join("");
  window._dlWireIdentity = () => {
    const sel = $("#dl-identity");
    if (sel) sel.onchange = () => { setIdentity(sel.value); renderDeadlineBoard(); renderDeadlinePool(); };

  };
  $("#dl-board").innerHTML = `
    <div class="accuracy-strip" style="margin:10px 0;">${board}
      <div class="m muted">deadline ${dr.deadline} · ${dr.trades_seen} MLB trades seen since ${dr.created}</div></div>
    ${strip}
    ${idBar}
    ${otc ? "" : "<p><b>Draft complete</b> — scores update as trades happen.</p>"}
    <table><thead><tr><th>#</th><th>Drafter</th><th>Player</th><th>Predicted</th><th>Status</th></tr></thead>
    <tbody>${rows || ""}</tbody></table>`;
  window._dlWireIdentity && window._dlWireIdentity();
}

function renderDeadlinePool() {
  const q = ($("#dl-search").value || "").toLowerCase();
  const tier = $("#dl-tier").value;
  const posF = $("#dl-pos")?.value || "all";
  const teamF = $("#dl-team")?.value || "all";
  const awardF = $("#dl-award")?.value || "all";
  const awardMatch = (c) =>
    awardF === "all" ? true :
    awardF === "allstar" ? !!c.has_allstar :
    awardF === "top3" ? !!c.has_top3_voting :
    (!!c.has_allstar || !!c.has_top3_voting);
  const posMatch = (c) => {
    if (posF === "all") return true;
    const p = (c.position || "").toUpperCase();
    if (posF === "OF") return ["OF", "LF", "CF", "RF"].includes(p);
    if (posF === "DH") return ["DH", "UT", "UTIL"].includes(p);
    return p === posF;
  };
  const otcRaw = dlState && dlState.exists ? dlState.on_the_clock : null;
  // You must identify yourself, and it must be YOUR turn, to see Pick buttons.
  const otc = otcRaw && state.identity === otcRaw ? otcRaw : null;
  // Merge league-wide search hits (any active MLB player) into the table —
  // one search bar, no separate write-in. Extras only apply to the current query.
  const extras = (q && q === dlExtraQ) ? dlExtra : [];
  const combined = dlPool
    .filter((c) => (!q || c.name.toLowerCase().includes(q)))
    .concat(extras.filter((e) => !dlPool.some((c) => c.name === e.name)));
  const rows = combined
    .filter((c) => (tier === "all" || c.tier === tier || c.tier === "write-in") && posMatch(c) && (teamF === "all" || c.team === teamF) && awardMatch(c))
    .map((c) => {
      const badges = `${c.has_allstar ? "⭐" : ""}${c.has_top3_voting ? "🏅" : ""}`;
      const rumored = (c.rumored_teams || []).join(", ");
      const opts = MLB_TEAMS.map((t) => `<option value="${t}" ${(c.rumored_teams || [])[0] === t ? "selected" : ""}>${t}</option>`).join("");
      const pickCtl = c.already_traded
        ? `<span class="muted" style="font-size:11px;">✅ already traded</span>`
        : otc
          ? `<select class="dl-team" data-name="${escapeAttr(c.name)}">${opts}</select>
             <button class="btn-pick dl-pick" data-name="${escapeAttr(c.name)}">Pick</button>`
          : "";
      return `<tr class="${c.tier === "high" ? "hitter" : ""}" style="${c.already_traded ? "opacity:0.45;" : ""}">
        <td><b>${c.name}</b> ${badges}</td><td>${c.position || ""}</td><td>${c.team || ""}</td>
        <td><span class="bench-tag">${c.tier || "?"}</span></td>
        <td class="muted" style="font-size:12px;">${rumored}</td>
        <td class="notes" style="font-size:11px;max-width:340px;">${c.context || ""}</td>
        <td>${pickCtl}</td></tr>`;
    }).join("");
  $("#dl-pool").innerHTML = `<table><thead><tr><th>Player</th><th>Pos</th><th>Team</th><th>Tier</th><th>Rumored to</th><th>Context</th><th>${otc ? "Pick (you're up)" : (otcRaw ? `${otcRaw} is up` : "")}</th></tr></thead><tbody>${rows || "<tr><td colspan=7 class=muted>No candidates match.</td></tr>"}</tbody></table>`;
  $("#dl-pool").querySelectorAll(".dl-pick").forEach((b) => {
    b.addEventListener("click", async () => {
      const name = b.dataset.name;
      const sel = $("#dl-pool").querySelector(`.dl-team[data-name="${CSS.escape(name)}"]`);
      try {
        await api("/api/deadline/pick", { method: "POST", body: JSON.stringify({ drafter: state.identity, player_name: name, predicted_team: sel.value }) });
        await loadDeadline();
      } catch (e) { alert(e.message); }
    });
  });
}

$("#dl-create")?.addEventListener("click", async () => {
  const names = ($("#dl-drafters").value || "").split(",").map((s) => s.trim()).filter(Boolean);
  if (names.length < 2) return alert("need at least 2 drafters");
  try {
    await api("/api/deadline/draft", { method: "POST", body: JSON.stringify({ drafters: names, rounds: parseInt($("#dl-rounds").value, 10) }) });
    await loadDeadline();
  } catch (e) { alert(e.message); }
});
$("#dl-search")?.addEventListener("input", () => {
  if (!dlPool.length) return;
  renderDeadlinePool();
  const q = ($("#dl-search").value || "").trim();
  clearTimeout(dlSearchTimer);
  if (q.length < 3) { dlExtra = []; dlExtraQ = ""; return; }
  dlSearchTimer = setTimeout(async () => {
    try {
      const r = await api(`/api/deadline/player_search?q=${encodeURIComponent(q)}`);
      dlExtra = r.results || [];
      dlExtraQ = q.toLowerCase();
      renderDeadlinePool();
    } catch (e) { /* search is best-effort */ }
  }, 300);
});
$("#dl-tier")?.addEventListener("change", () => dlPool.length && renderDeadlinePool());
$("#dl-pos")?.addEventListener("change", () => dlPool.length && renderDeadlinePool());
$("#dl-team")?.addEventListener("change", () => dlPool.length && renderDeadlinePool());
$("#dl-award")?.addEventListener("change", () => dlPool.length && renderDeadlinePool());


// ---------- Farm Report ----------
// Works for ANY Fantrax league/team; defaults to ours, remembered per browser.
const FARM_DEFAULT_LEAGUE = "jfrwctf2mhjchb09";
const FARM_DEFAULT_TEAM = "tm87w5i5mhjchb0h";  // Crochet's Swinging Junk

function farmIds() {
  return {
    league: localStorage.getItem("farm_league") || FARM_DEFAULT_LEAGUE,
    team: localStorage.getItem("farm_team") || FARM_DEFAULT_TEAM,
  };
}
async function farmPopulateTeams() {
  const league = ($("#farm-league").value || "").trim();
  const sel = $("#farm-team");
  if (!league) return;
  try {
    const d = await api(`/api/fantrax/teams?league_id=${encodeURIComponent(league)}`);
    const cur = farmIds().team;
    sel.innerHTML = (d.teams || []).map((t) =>
      `<option value="${t.team_id}" ${t.team_id === cur ? "selected" : ""}>${t.name}</option>`).join("");
  } catch (e) {
    sel.innerHTML = `<option value="">(teams: ${e.message.slice(0, 40)})</option>`;
  }
}

function farmBadge(v) {
  const c = v === "green" ? "var(--accent-2)" : v === "red" ? "var(--bad)" : "#eab308";
  const t = v === "green" ? "KEEP" : v === "red" ? "CUTTABLE" : "WATCH";
  return `<span style="color:${c};font-weight:700;font-size:11px;">● ${t}</span>`;
}
function farmLine(r) {
  const bits = [];
  for (const b of r.bat || []) bits.push(`${b.level}: ${b.ops.toFixed(3)} OPS · ${b.k_pct}% K · ${b.bb_pct}% BB · ${b.hr} HR (${b.pa} PA)`);
  for (const a of r.arm || []) bits.push(`${a.level}: ${a.era} ERA · ${a.kbb_pct}% K-BB · ${a.fip_lite} FIPlite (${a.ip} IP)`);
  return bits.join(" | ") || "no 2026 MiLB stats";
}
function farmTable(rows, withRank) {
  return `<table><thead><tr>${withRank ? "<th>#</th>" : ""}<th>Player</th><th>Verdict</th><th>Why</th><th>2026 line</th></tr></thead><tbody>${
    rows.map((r) => `<tr>${withRank ? `<td>${r.rank ?? ""}</td>` : ""}
      <td><b>${r.name}</b>${withRank ? ` <span class="muted">(${r.position || ""} ${r.team || ""})</span>` : ""}</td>
      <td>${farmBadge(r.verdict)}</td><td class="muted" style="font-size:12px;">${r.reason}</td>
      <td class="muted" style="font-size:11px;">${farmLine(r)}</td></tr>`).join("")}</tbody></table>`;
}
async function loadFarm() {
  window._farmLoaded = true;
  const ids = farmIds();
  const lg = $("#farm-league");
  if (lg && !lg.value) { lg.value = ids.league; farmPopulateTeams(); }
  const league = (lg?.value || ids.league).trim();
  const team = $("#farm-team")?.value || ids.team;
  localStorage.setItem("farm_league", league);
  if (team) localStorage.setItem("farm_team", team);
  $("#farm-status").textContent = "loading… (first load ~30s, cached after)";
  try {
    const [mine, targets] = await Promise.all([
      api(`/api/farm/report?league_id=${encodeURIComponent(league)}&team_id=${encodeURIComponent(team)}`),
      api(`/api/farm/targets?league_id=${encodeURIComponent(league)}`),
    ]);
    $("#farm-mine").innerHTML = farmTable(mine.players || [], false);
    $("#farm-targets").innerHTML = farmTable(targets.targets || [], true);
    $("#farm-status").textContent = `rankings as of ${targets.as_of || "?"}`;
  } catch (e) {
    $("#farm-status").textContent = e.message.includes("401")
      ? "Fantrax cookie expired — re-auth on the Fantrax tab, then reload"
      : e.message;
    window._farmLoaded = false;
  }
}
$("#farm-load")?.addEventListener("click", loadFarm);
$("#farm-league")?.addEventListener("change", farmPopulateTeams);
