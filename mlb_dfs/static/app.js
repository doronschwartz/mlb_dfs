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
    refresh();
  });
});

$("#refresh").addEventListener("click", refresh);
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
  } catch (e) {
    out.innerHTML = `<div class="muted">${e.message}</div>`;
  }
}

function renderGame(g) {
  const ap = g.away.probablePitcher?.name ?? "TBD";
  const hp = g.home.probablePitcher?.name ?? "TBD";
  return `
    <div class="game">
      <div class="status">${g.detailedStatus ?? ""}</div>
      <div class="matchup">${g.away.abbr ?? g.away.name} @ ${g.home.abbr ?? g.home.name}</div>
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
    el.addEventListener("click", () => {
      const pk = Number(el.dataset.pk);
      if (state.selectedGamePks.has(pk)) state.selectedGamePks.delete(pk);
      else state.selectedGamePks.add(pk);
      renderGamePicker();
    });
  });
  const n = state.selectedGamePks.size;
  $("#game-count").textContent = n
    ? `· ${n} selected`
    : `· ${state.slateGames.length} total`;
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

function renderProjectionsTable() {
  const rows = projCache.data
    .slice(0, 60)
    .map(
      (p) => `
      <tr class="${p.role}">
        <td>${p.projected_points.toFixed(2)}</td>
        <td>${p.name}</td>
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

$("#load-draft").addEventListener("click", async () => {
  state.currentDraftId = $("#draft-id").value;
  await renderDraft();
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
        <h4>${d} ${onC ? `<span class="muted">← on the clock</span>` : ""}
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
          <td><button class="btn-pick swap-btn" data-pid="${p.player_id}">Swap in</button></td>
        </tr>`,
      )
      .join("");
    overlay.querySelectorAll(".swap-btn").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await api(`/api/drafts/${state.currentDraftId}/picks/${pickNumber}/replace`, {
            method: "POST",
            body: JSON.stringify({ player_id: Number(b.dataset.pid) }),
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
    const lock = myTurn ? "" : "locked";
    const rows = data.recommendations
      .map(
        (r) => {
          const slots = r.eligible_slots && r.eligible_slots.length
            ? r.eligible_slots
            : [r.recommend_slot];
          const pills = slots
            .map((s) => {
              const recommended = s === r.recommend_slot ? "recommended" : "";
              return `<span class="slot-pill ${recommended} ${lock}" data-pid="${r.player_id}" data-slot="${s}">${s}</span>`;
            })
            .join("");
          return `
          <tr class="${r.role}">
            <td>${r.score.toFixed(2)}</td>
            <td>${r.projected_points.toFixed(2)}</td>
            <td>${r.name}</td>
            <td>${r.position ?? "-"}</td>
            <td>${pills}</td>
          </tr>`;
        },
      )
      .join("");
    $("#recs-out").innerHTML = `
      <table>
        <thead><tr><th>Score</th><th>Proj</th><th>Player</th><th>Pos</th><th>Pick into…</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="muted" style="margin-top:6px;font-size:11px;">Click any slot to draft into it. The starred slot is the recommender's default — but pick whatever you want.</div>`;
    $$("#recs-out .slot-pill").forEach((b) => {
      if (b.classList.contains("locked")) return;
      b.addEventListener("click", async () => {
        try {
          await api(`/api/drafts/${state.currentDraftId}/pick`, {
            method: "POST",
            body: JSON.stringify({
              draft_id: state.currentDraftId,
              player_id: Number(b.dataset.pid),
              slot: b.dataset.slot,
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
  const lock = state._myTurnAtLastRender ? "" : "locked";
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
        <tr><th>Proj</th><th>Player</th><th>Pos</th><th>Role</th><th>Pick into…</th><th>Notes</th></tr>
      </thead>
      <tbody>${rows
        .map(
          (p) => `
        <tr class="${p.role}">
          <td>${p.projected_points.toFixed(2)}</td>
          <td>${p.name}</td>
          <td>${p.position ?? "-"}</td>
          <td>${p.role}</td>
          <td>${
            p.eligible_slots.length
              ? p.eligible_slots
                  .map(
                    (s) =>
                      `<span class="slot-pill ${lock}" data-pid="${p.player_id}" data-slot="${s}">${s}</span>`,
                  )
                  .join("")
              : `<span class="slot-pill disabled">no slot left</span>`
          }</td>
          <td class="notes">${(p.notes || []).join(" · ")}</td>
        </tr>`,
        )
        .join("")}</tbody>
    </table>`;
  $("#pool-out").innerHTML = html;
  $$("#pool-out .slot-pill").forEach((el) => {
    if (el.classList.contains("disabled") || el.classList.contains("locked")) return;
    el.addEventListener("click", async () => {
      try {
        await api(`/api/drafts/${state.currentDraftId}/pick`, {
          method: "POST",
          body: JSON.stringify({
            draft_id: state.currentDraftId,
            player_id: Number(el.dataset.pid),
            slot: el.dataset.slot,
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
          (s) =>
            `<div class="row"><span>${s.rank}. <b>${s.drafter}</b></span><span class="total">${s.total.toFixed(2)} <span class="muted">(full ${s.full_total.toFixed(2)})</span></span></div>`,
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
    const myTurn = isMyTurn(data.on_the_clock);
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

async function refresh() {
  await loadDraftList().catch(() => {});
  if (state.tab === "slate") await loadSlate();
  if (state.tab === "project") await loadProjections();
  if (state.tab === "draft") {
    await ensureSlateLoaded();
    renderGamePicker();
    await renderDraft();
    startPolling();
  } else {
    stopPolling();
  }
  if (state.tab === "schedule") initScheduleTab();
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
