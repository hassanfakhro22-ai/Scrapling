# Installed Agent Skills

This directory vendors [AgentSkills](https://agentskills.io/specification)-compatible skills.
`Scrapling-Skill/` is Scrapling's own skill (see [README.md](README.md)); `last30days/` is a
third-party skill installed alongside it.

## last30days

| | |
|---|---|
| Skill | [`last30days`](https://github.com/mvanhorn/last30days-skill) v3.33.1 |
| Author / License | mvanhorn / MIT |
| Path | `agent-skill/last30days/` |
| Upstream commit | `5f485e5b211689cfd397dfc31e9093fcc8afa91b` (2026-11-10) |
| Installed | 2026-11-10 |
| Integrity | 154/154 files verified bit-identical to upstream (`git hash-object` == upstream blob SHA) |

Research skill: pulls the last 30 days of posts and engagement from Reddit, X, YouTube, TikTok,
Hacker News, Polymarket, GitHub and the web, then synthesises one cited brief.

### What was deliberately omitted

`assets/` (~14 MB of demo media: `dog-original.jpeg`, `swimmom-mockup.jpeg`, `aging-portrait.jpeg`,
`dog-as-human.png`, `claude-code-rap.mp3`) is **not** vendored. It is referenced only by upstream
`.skillignore` / `.clawhubignore`, never by `SKILL.md`, `references/` or `scripts/`, so nothing at
runtime needs it. Everything else is an exact copy.

### Runtime requirements

- **Python 3.12+ — hard requirement.** Not a soft preference: `scripts/lib/hackernews.py` and
  `scripts/lib/render.py` use PEP 701 f-strings (nested quotes / backslashes inside f-string
  expressions) and fail to *parse* on 3.11. The engine's own gate reports:

  ```text
  last30days v3 requires Python 3.12+.
  Detected Python 3.11.2.
  ```

- **Node >= 22** for the vendored X-search client in `scripts/lib/vendor/bird-search/`.
  It is vendored and dependency-free — **no `npm install` step is required**.
- **No credentials needed to start.** Reddit, Hacker News, Polymarket and GitHub work keyless.
  Optional env vars (`SCRAPECREATORS_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `X_BEARER_TOKEN`,
  `OPENROUTER_API_KEY`, `PERPLEXITY_API_KEY`, `BRAVE_API_KEY`, `APIFY_API_TOKEN`, ...) unlock
  further sources; run the skill's setup wizard or `doctor` to see what is active.

### Health check / usage

```bash
cd agent-skill/last30days
python3.12 scripts/last30days.py doctor          # diagnose broken or missing sources
python3.12 scripts/last30days.py "AI video tools"
```

In hosts that support slash-command skills (Claude Code, OpenClaw, Codex, Cursor, ...), invoke it
as `/last30days <topic>`. To install it per-user instead of vendoring it here:

```bash
npx skills add mvanhorn/last30days-skill -g      # drop -g to scope per project
```

### Updating

```bash
git clone --depth 1 https://github.com/mvanhorn/last30days-skill.git /tmp/l30
rsync -a --delete --exclude 'assets/' --exclude '__pycache__/' \
  /tmp/l30/skills/last30days/ agent-skill/last30days/
```

Then bump the version/commit fields in the table above.

### Note on the Arena sandbox

The sandbox this was installed in ships Python 3.11.2 only, and CPython 3.12 cannot be provisioned
there: `uv python install 3.12` (the fallback documented in `references/runtime.md`) downloads from
`release-assets.githubusercontent.com`, which the sandbox proxy blocks, and a source build is not
possible either (no `libssl-dev` / `zlib1g-dev` / `libffi-dev` headers, no apt network). The skill
is therefore correctly installed and structurally verified, but engine commands need to run on a
machine with Python 3.12+.
