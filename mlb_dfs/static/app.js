const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const today = new Date();
$("#date").value = today.toISOString().slice(0, 10);

const state = { tab: "slate", currentDraftId: null };

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
    out.innerHTML = data.games.length
      ? data.games.map(renderGame).join("")
      : `<div class="muted">No games scheduled.</div>`;
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
    body: JSON.stringify({ date: $("#date").value, drafters }),
  });
  state.currentDraftId = data.draft_id;
  await loadDraftList();
  await renderDraft();
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
    $("#recs-out").innerHTML = "";
    return;
  }
  const data = await api(`/api/drafts/${state.currentDraftId}`);
  const onClock = data.on_the_clock; // [drafter, slot] | null
  const html = [];
  html.push(`<div class="muted">Draft <b>${data.draft_id}</b> — ${data.is_complete ? "complete" : `On the clock: <b>${onClock?.[0] ?? "-"}</b> (${onClock?.[1] ?? "-"})`}</div>`);
  html.push(`<div class="rosters">`);
  for (const d of data.drafters) {
    const onC = onClock && onClock[0] === d;
    const picks = data.rosters[d] || [];
    html.push(
      `<div class="roster ${onC ? "on-clock" : ""}">
        <h4>${d} ${onC ? `<span class="muted">← (${onClock[1]})</span>` : ""}</h4>
        ${picks
          .map(
            (p) =>
              `<div class="row"><span><span class="slot">${p.slot}</span> ${p.name}</span><span class="muted">${(p.projected ?? 0).toFixed(1)}</span></div>`,
          )
          .join("")}
        <div class="muted" style="margin-top:6px;font-size:11px;">${picks.length}/10 picks</div>
      </div>`,
    );
  }
  html.push(`</div>`);
  $("#draft-state").innerHTML = html.join("");

  if (!data.is_complete) await renderRecs();
  else $("#recs-out").innerHTML = `<div class="muted">Draft complete.</div>`;
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
async function refresh() {
  await loadDraftList().catch(() => {});
  if (state.tab === "slate") await loadSlate();
  if (state.tab === "project") await loadProjections();
  if (state.tab === "draft") await renderDraft();
}
refresh();
