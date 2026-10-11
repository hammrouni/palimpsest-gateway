# Palimpsest Gateway — Launch Marketing Plan & Distribution

**Version:** 1.0  
**Date:** 2026-10-11  
**Status:** Draft  
**Owner:** Marketing (agent)  

---

## 1. Goal

**Primary:** Drive GitHub stars → repo awareness → self-hosted deployments → community contributors.  
**North-star metric:** 500+ GitHub stars in first 30 days post-launch.  
**Leading indicators:** Dev.to article views, HN submission score, LinkedIn impressions, Docker pulls.

---

## 2. Positioning

| Element | Definition |
|---------|------------|
| **Category** | Self-hosted LLM API gateway / key-pool proxy |
| **For** | OSS developers, self-hosting teams, cost-optimizers running multiple LLM providers |
| **Vs.** | Manual key rotation, single-provider lock-in, paid managed gateways (Portkey, Helicone) |
| **One-liner** | One OpenAI-compatible endpoint in front of all your LLM keys — pool, failover, keep conversations alive. |

**Key differentiators:**
- Session continuity across provider failover (conversation context survives key switches)
- OpenAI API drop-in compatibility (zero code change)
- Free, MIT-licensed, fully self-hosted
- Encrypted key storage at rest

---

## 3. Target Audience (ICP)

| Segment | Pain Point | Hook |
|---------|------------|------|
| **OSS developers** building LLM apps | Rate limits break dev loops; managing multiple keys is messy | "Stop juggling keys. One endpoint, zero code changes." |
| **Self-hosters / homelab** | Want control, privacy, no SaaS fees | "Run your own gateway. Your keys, your server, your data." |
| **LLM-cost-optimizers** (startups, agencies) | Paying for unused quota; free-tier keys exhaust fast | "Pool free + paid keys. Auto-failover = zero waste." |
| **Multi-key teams** (internal platforms) | Sharing provider keys across apps is insecure | "Issue app-scoped gateway keys. Provider keys never leave the vault." |

---

## 4. Distribution Channels (3 Channels)

### Channel 1: GitHub Trending / Organic Discovery
**Why:** Primary acquisition surface for dev tools. Stars = social proof = more stars.
**Tactics:**
- Optimize repo: topics `llm`, `openai`, `gateway`, `proxy`, `failover`, `self-hosted`, `fastapi`, `docker`
- README: clear hero, quickstart < 60 sec, architecture diagram, badges
- `CONTRIBUTING.md` + `CODE_OF_CONDUCT.md` + good first issues labeled
- Launch week: ask early users to star; pin launch issue
- Post-launch: weekly "good first issue" tweets/X posts

**KPIs:** Stars/day, watchers, traffic from GitHub search

---

### Channel 2: Dev.to Article (Technical Deep-Dive)
**Why:** Dev.to audience = builders who self-host. High intent, long dwell time, SEO compounding.
**Owner:** Marketing agent (TASK-175)
**Profile:** `khaled_hammrouni_bc97ab98`
**Angle:** *"How I Built an Open-Source LLM Key-Pool Gateway"*
- Problem: cost, rate limits, vendor lock-in
- Architecture: key pooling, smart routing, session continuity
- Hard parts: streaming failover, context carry-over, encrypted storage
- Code sample: 10-line quickstart + Docker compose
- CTA: Star repo, try Docker, contribute

**KPIs:** Views, reads, reactions, comments, click-through to GitHub

---

### Channel 3: LinkedIn (via Content Engine) + X/Short Posts
**Why:** Reaches engineering leads, CTOs, platform teams who approve self-hosted tools.
**Owner:** Social agent (TASK-176)
**Format:**
- 1 LinkedIn launch post (first-person, no AI-isms, specific)
- 3–5 supporting X/Threads posts (thread: architecture, demo GIF, Docker one-liner)
- All posts require Khaled review before publishing

**KPIs:** Impressions, engagement rate, profile visits, referral traffic to repo

---

## 5. 2-Week Launch Cadence

| Week | Day | Activity | Channel | Owner |
|------|-----|----------|---------|-------|
| **Pre-launch (-7 to -1)** | -7 | Finalize landing page (GitHub Pages) | Repo | Dev agent |
|  | -5 | Dev.to article draft → Khaled review | Dev.to | Marketing |
|  | -3 | LinkedIn + X posts draft → Khaled review | LinkedIn/X | Social |
|  | -2 | Show HN draft → Khaled review | HN | Social |
|  | -1 | Smoke test: Docker compose up, quickstart works | — | Dev |
| **Launch Week (Day 0–6)** | Day 0 | **Push to GitHub Pages** — site live | Site | Dev |
|  | Day 0 | **Publish Dev.to article** (morning UTC) | Dev.to | Marketing |
|  | Day 0 | **Submit Show HN** (morning UTC, ~9 AM PT) | HN | Social |
|  | Day 0 | **Post LinkedIn** (after Dev.to/HN live) | LinkedIn | Social |
|  | Day 0 | **Thread X posts** (staggered) | X | Social |
|  | Day 1 | Respond to HN/Dev.to comments; amplify | All | All |
|  | Day 2 | Share metrics screenshot (stars, visits) | X/LinkedIn | Social |
|  | Day 3 | Publish "Week 1 retro" mini-post | Dev.to/LinkedIn | Marketing |
|  | Day 4–6 | Engage issues/PRs; triage good-first-issues | GitHub | Dev |
| **Post-launch (Week 2)** | Day 7–10 | Follow-up: "What we learned from launch traffic" | Dev.to | Marketing |
|  | Day 10 | Community call / office hours (optional) | Discord/Calendar | Dev |
|  | Day 14 | Monthly metrics report → stakeholders | Internal | Marketing |

---

## 6. Launch Checklist

### Pre-launch (T-7 days)
- [ ] Landing page deployed to GitHub Pages (`TASK-170`)
- [ ] Repo topics + description + SEO tags set (`TASK-143`)
- [ ] Dev.to article drafted, reviewed, scheduled (`TASK-175`)
- [ ] LinkedIn + X posts drafted, reviewed, scheduled (`TASK-176`)
- [ ] Show HN title + body drafted, reviewed (`TASK-147`)
- [ ] Docker image published to GHCR / Docker Hub
- [ ] Quickstart verified: `docker compose up -d` → `curl localhost:8000/health` → OpenAI client works
- [ ] Demo screenshot + short clip added to README (`TASK-172`)
- [ ] GitHub issue + PR templates added (`TASK-173`)
- [ ] Technical SEO: sitemap, robots.txt, meta tags (`TASK-150`)

### Launch Day (Day 0)
- [ ] GitHub Pages live
- [ ] Dev.to article published
- [ ] Show HN submitted
- [ ] LinkedIn post published
- [ ] X thread posted
- [ ] Monitor: GitHub traffic, Dev.to analytics, HN comments, LinkedIn notifications
- [ ] Respond to every comment/question within 2 hours

### Post-launch (Day 1–14)
- [ ] Daily: reply to issues, triage PRs, label good-first-issues
- [ ] Day 3: Publish launch retrospective (what worked, what didn't)
- [ ] Day 7: Follow-up content piece
- [ ] Day 14: Compile metrics report (stars, clones, Docker pulls, Dev.to reads, HN score, LinkedIn impressions)

---

## 7. Coordination Map

| Task | ID | Owner | Depends On | Status |
|------|-----|-------|------------|--------|
| Landing page → GitHub Pages | TASK-170 | Dev agent | — | `todo` |
| Dev.to launch article | TASK-175 | Marketing (this agent) | Landing page | `backlog` |
| LinkedIn + X posts | TASK-176 | Social agent | Dev.to draft | `backlog` |
| Show HN post | TASK-147 | Social agent | Launch day | `todo` |
| Demo screenshot/clip | TASK-172 | Dev agent | Running demo | `todo` |
| Issue/PR templates | TASK-173 | Dev agent | — | `backlog` |
| Technical SEO | TASK-150 | Dev agent | Landing page | `todo` |

---

## 8. Measurement & Success Criteria

| Metric | Target (Day 30) | Tool |
|--------|----------------|------|
| GitHub stars | ≥ 500 | GitHub Insights |
| Repo clones (unique) | ≥ 200 | GitHub Traffic |
| Docker pulls | ≥ 1,000 | Docker Hub / GHCR stats |
| Dev.to article reads | ≥ 2,000 | Dev.to Analytics |
| HN submission score | ≥ 50 | HN Algolia |
| LinkedIn impressions | ≥ 10,000 | LinkedIn Analytics |
| Referral traffic to repo | ≥ 500 sessions | GA4 / Plausible |
| Good-first-issue PRs opened | ≥ 5 | GitHub |

---

## 9. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| HN submission gets no traction | Medium | High | Have backup: post to r/selfhosted, r/localllama, r/programming |
| Dev.to article buried | Low | Medium | Cross-post to Medium, Hashnode; share in Discords |
| Docker image pull fails | Low | High | Test multi-arch build; publish to GHCR + Docker Hub |
| Khaled unavailable for review | Medium | Medium | Async review window: 24h SLA; pre-approve templates |

---

## 10. Recommended Next Moves

1. **Marketing agent (this run):** Move TASK-175 to `in_progress`, draft Dev.to article in `docs/devto-launch-article.md` for review.
2. **Social agent:** Move TASK-176 to `in_progress`, draft LinkedIn + X posts in `docs/social-launch-posts.md` for review.
3. **Dev agent:** Move TASK-170 to `in_progress`, deploy landing page to GitHub Pages.
4. **All:** Schedule 30-min launch sync (async or live) 48h before Day 0 to confirm all assets approved.

---

*End of Launch Marketing Plan v1.0*