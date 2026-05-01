# Working in this repo

Hard rules to keep token usage low. Follow them.

## Code edits
- Read with `grep -n` first; only Read whole files when grep can't tell you the answer.
- One Edit per change. If an Edit fails, re-read the exact line and try once more — don't fish.
- No commentary in code. No "why this matters" docstrings. One short comment when WHY is non-obvious.

## Tool calls
- Don't curl-verify after every deploy. Trust `fly deploy` exit code + the build hash. Verify only when something's broken or the change is risky.
- Don't print `picks intact: 30 /30` after every change. Drafts are fsync'd; assume durable.
- Don't pretty-print JSON unless debugging. Use `| head -3` or `| python3 -c "...one liner..."`.
- Skip "before / after" comparisons. Show after only.

## Commits
- One-line subject, no body unless the change is genuinely complex.
- No "Why this matters / What this enables" paragraphs.
- No emoji, no checklists.

## Replies to user
- Lead with the answer. No recap of what they asked.
- Skip the "✅ Live (build XYZ)" header unless they explicitly asked for verification.
- No multi-row markdown tables for simple changes — one sentence.
- Don't re-explain features I just shipped.
- Don't list "what else shipped this round" — they were there.

## TodoWrite
- Use only for >3 distinct steps. Skip for one-liners.
- Don't update todo list mid-task just to satisfy reminders.

## When the user pastes a long file (CSV, notebook, screenshot text)
- Extract only the fields you need with a single `python3 -c` or `head`. Don't dump the whole thing.
- Don't re-read it later in the same session.

## Things I keep doing that waste tokens
- "Let me verify..." then 3 curl calls. Stop.
- Re-rendering the full file structure / endpoint list after each deploy.
- Long explanations after a deploy when the user can just look at the page.
- Echoing back what was committed in the response when the commit message already says it.
