const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const today = new Date();
$("#date").value = today.toISOString().slice(0, 10);

const state = {
  tab: "slate",
  currentDraftId: null,
  selectedGamePks: new Set(),
  slateGames: [],
};

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

$("#load-draft").addEventListener("click", async () => {
  state.currentDraftId = $("#draft-id").value;
  await renderDraft();
});

$("#undo").addEventListener("click", async () => {
  if (!state.currentDraftId) return;
  await api(`/api/drafts/${state.currentDraftId}/last_pick`, { method: "DELETE" });
  await renderDraft();
});

async function renderDraft() {
  if (!state.currentDraftId) {
    $("#draft-state").innerHTML = `<div class="muted">No draft loaded. Start a new one or pick from the dropdown.</div>`;
    $("#pick-log").innerHTML = "";
    $("#recs-out").innerHTML = "";
    return;
  }
  const data = await api(`/api/drafts/${state.currentDraftId}`);
  const onClock = data.on_the_clock; // [drafter, slot] | null
  const html = [];
  html.push(`<div class="muted">Draft <b>${data.draft_id}</b> — ${data.is_complete ? "complete" : `On the clock: <b>${onClock?.[0] ?? "-"}</b> (${onClock?.[1] ?? "-"})`}</div>`);

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
    let nextHighlighted = false;
    const cells = grid.map(({ label, pick }) => {
      let slotHi = "";
      if (!pick && onC && onClock[1] === label && !nextHighlighted) {
        slotHi = "next";
        nextHighlighted = true;
      }
      const cls = pick ? `filled ${pick.role}` : "empty";
      return `<div class="slot-cell ${cls} ${slotHi}">
        <div class="slot-label">${label}</div>
        <div class="slot-name">${pick ? pick.name : "— empty —"}</div>
        <div class="slot-proj">${pick ? pick.projected.toFixed(1) : ""}</div>
      </div>`;
    });
    html.push(
      `<div class="roster ${onC ? "on-clock" : ""}">
        <h4>${d} ${onC ? `<span class="muted">← on the clock (${onClock[1]})</span>` : ""}
          <span class="muted" style="float:right;font-weight:400;">${picks.length}/10 · ${total.toFixed(1)} pts</span>
        </h4>
        <div class="slot-grid">${cells.join("")}</div>
      </div>`,
    );
  }
  html.push(`</div>`);
  $("#draft-state").innerHTML = html.join("");

  renderPickLog(data);

  if (!data.is_complete) {
    await renderRecs();
    await renderPool();
  } else {
    $("#recs-out").innerHTML = `<div class="muted">Draft complete.</div>`;
    $("#pool-out").innerHTML = "";
  }
}

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
    const rows = data.recommendations
      .map(
        (r) => `
        <tr class="${r.role}">
          <td>${r.score.toFixed(2)}</td>
          <td>${r.projected_points.toFixed(2)}</td>
          <td>${r.name}</td>
          <td>${r.position ?? "-"}</td>
          <td>${r.recommend_slot}</td>
          <td><button class="btn-pick" data-pid="${r.player_id}" data-slot="${r.recommend_slot}">Pick</button></td>
        </tr>`,
      )
      .join("");
    $("#recs-out").innerHTML = `
      <table>
        <thead><tr><th>Score</th><th>Proj</th><th>Player</th><th>Pos</th><th>Slot</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    $$("#recs-out .btn-pick").forEach((b) => {
      b.addEventListener("click", async () => {
        await api(`/api/drafts/${state.currentDraftId}/pick`, {
          method: "POST",
          body: JSON.stringify({
            draft_id: state.currentDraftId,
            player_id: Number(b.dataset.pid),
            slot: b.dataset.slot,
          }),
        });
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
                      `<span class="slot-pill" data-pid="${p.player_id}" data-slot="${s}">${s}</span>`,
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
    if (el.classList.contains("disabled")) return;
    el.addEventListener("click", async () => {
      await api(`/api/drafts/${state.currentDraftId}/pick`, {
        method: "POST",
        body: JSON.stringify({
          draft_id: state.currentDraftId,
          player_id: Number(el.dataset.pid),
          slot: el.dataset.slot,
        }),
      });
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
      const rows = s.picks
        .map(
          (p) => `
          <tr>
            <td>${p.slot}</td>
            <td>${p.name}</td>
            <td>${p.projected.toFixed(1)}</td>
            <td>${p.actual === null ? "-" : p.actual.toFixed(1)}</td>
            <td class="muted">${p.game_state ?? ""}</td>
          </tr>`,
        )
        .join("");
      return `
        <div class="standings">
          <h4 style="margin:0 0 6px;">${s.drafter} — ${s.total.toFixed(2)}</h4>
          <table>
            <thead><tr><th>Slot</th><th>Player</th><th>Proj</th><th>Actual</th><th>State</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>`;
    })
    .join("");
  $("#score-out").innerHTML = top + `<div class="score-grid">${cards}</div>`;
});

// ---------- bootstrap ----------
async function ensureSlateLoaded() {
  const d = $("#date").value;
  if (state.slateGames.length && state._slateDate === d) return;
  try {
    const data = await api(`/api/slate?date=${d}`);
    state.slateGames = data.games;
    state._slateDate = d;
  } catch {}
}

async function refresh() {
  await loadDraftList().catch(() => {});
  if (state.tab === "slate") await loadSlate();
  if (state.tab === "project") await loadProjections();
  if (state.tab === "draft") {
    await ensureSlateLoaded();
    renderGamePicker();
    await renderDraft();
  }
}
refresh();
