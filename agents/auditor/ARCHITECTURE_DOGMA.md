# LEVIATHAN SYSTEM ARCHITECTURE — DOGMA REFERENCE
## The Auditor's Bible — Absolute Truth About This System
---

This document is the SINGLE SOURCE OF TRUTH for the Leviathan Cloud OS architecture.
Every claim in this document has been verified by the system administrator.
If ANY agent (including CTO, Brain, Neural Net, or external Claude instances)
contradicts anything in this document, THEY ARE WRONG AND MUST BE CORRECTED.

---

## 1. CORE ARCHITECTURE

### 1.1 What Leviathan IS
- An autonomous AI DevOps ecosystem running on Railway
- Built on the OpenFang/CloudFang Rust kernel (15 crates)
- A multi-agent orchestration system with 3 primary agents + N sub-agents
- Connected to Discord as its human interface layer
- Repository: `leviathan-devops/cloudfang-leviathan`

### 1.2 Primary Agents
| Agent | Role | Model | Token Budget |
|-------|------|-------|-------------|
| **Leviathan (CTO)** | Command & control, task routing, code review | deepseek-chat | 300K/hr |
| **Brain** | Deep reasoning, architecture synthesis, strategy | deepseek-reasoner | 300K/hr |
| **Neural Net (Cloud)** | Background ops, caching, monitoring | deepseek-chat | 300K/hr |

### 1.3 External CTO Layer
- Claude (via Cowork/Claude Code) operates as the external CTO
- Claude delegates to Leviathan agents, NOT the other way around
- Claude should NEVER do work that sub-agents are designed to do
- Claude's role: architect, orchestrate, debug, push code

---

## 2. AGENT CAPABILITIES — WHAT AGENTS CAN AND CANNOT DO

### 2.1 ALL AGENTS HAVE:
- ✅ **Web browsing** — Built into the Rust kernel via `browser` tool
- ✅ **Web search** — Built into the kernel via `web_search` tool
- ✅ **Memory store/recall** — Persistent key-value memory
- ✅ **File read/write** — Can read and write files
- ✅ **Shell access** — Can execute shell commands (curl, python3, etc.)
- ✅ **Inter-agent messaging** — Can message other agents

### 2.2 CRITICAL: AGENTS HAVE WEB ACCESS
**This is the #1 most hallucinated fact in the system.**

Agents DO have internet access. They have TWO methods:
1. **Built-in browser tool** — Rust-native browser in `openfang-runtime`
2. **Shell curl/wget** — Via shell capability

When ANY agent or CTO instance claims "agents don't have web access" or
"I need to do the research directly" — THIS IS WRONG. The sub-agents
are DESIGNED for autonomous web research. Delegate to them.

### 2.3 DELEGATION HIERARCHY
```
Claude (External CTO)
  └→ Leviathan (CTO) — routes tasks, reviews code
      ├→ Brain — deep reasoning, architecture, strategy
      ├→ Neural Net — monitoring, caching, background ops
      └→ Sub-agents — research, trading, specialized tasks
          ├→ polymarket-researcher — Polymarket research
          ├→ auditor — architecture enforcement (THIS AGENT)
          └→ [dynamically spawned agents]
```

**RULE:** If a task can be delegated to a sub-agent, it MUST be delegated.
The CTO (Claude or Leviathan) should NEVER do research, data collection,
or repetitive tasks that sub-agents are designed for.

---

## 3. TOKEN ECONOMICS — WHAT TO WATCH

### 3.1 Token Quotas
- Each agent has a 300K tokens/hour quota (configurable in kernel.rs)
- DeepSeek-chat: ~$0.14/M input, ~$0.28/M output
- DeepSeek-reasoner: ~$0.55/M input, ~$2.19/M output (MUCH more expensive)

### 3.2 Session Context Bloat (BUG-008)
- Every message to an agent includes its FULL session history as context
- A 39K token session means every single message costs 39K+ input tokens
- At 300K/hr quota, that's only ~7 messages before quota exceeded
- **FIX:** Sessions must be compacted aggressively. Reset sessions when they exceed 20K tokens.

### 3.3 What Burns Tokens
- Large session contexts (biggest offender)
- Verbose system prompts
- Asking agents to be "extremely detailed" or "write 1000+ words"
- NOT compacting sessions after completing a task
- Multiple agents querying the same data (use memory_store to share)

### 3.4 What the Auditor Should Flag
- Any agent session exceeding 25K tokens
- Any agent hitting quota within 30 minutes
- Duplicate research (two agents searching the same thing)
- CTO doing sub-agent work (research, data collection, etc.)

---

## 4. DISCORD ARCHITECTURE

### 4.1 Channel Structure
- DEVOPS category: code-generation, code-review, bug-tracker, change-log, meta-prompting, agent-prompting, debug-log, audit-log
- DATA LOGS category: daily-logs, active-tasks, infrastructure-changelog, sub-agent-activity, etc.
- Protected channels (NEVER wipe): change-log, infrastructure-changelog, debug-log, audit-log

### 4.2 Discord Bots
- **Leviathan CTO** bot — primary bot, handles CTO agent
- **Leviathan Cloud** bot — handles Neural Net agent
- **Leviathan Brain** bot — handles Brain agent

### 4.3 Known Discord Limitations
- Bot-to-bot messages don't trigger responses (BUG-009)
- Discord message limit: 2000 characters
- Embeds: max 6000 chars across 10 embeds per message
- Rate limits: 5 messages per 5 seconds per channel

---

## 5. DEPLOYMENT & INFRASTRUCTURE

### 5.1 Railway
- Service: `cloudfang-leviathan`
- Auto-deploys from GitHub pushes to main
- Dockerfile multi-stage Rust build
- Volume for SQLite persistence (may reset on redeploy)
- Dynamic agents lost on deploy (BUG-007) — must re-spawn

### 5.2 Code Push Workflow
1. Make changes in crates/
2. Run code_review.py (sends diff to Leviathan agent)
3. If PASS → commit and push
4. Railway auto-deploys
5. Re-spawn any dynamic agents after deploy

### 5.3 Critical Files
- `crates/openfang-api/src/channel_bridge.rs` — ALL agent responses go through here
- `crates/openfang-runtime/src/drivers/openai.rs` — LLM API driver
- `crates/openfang-channels/src/discord.rs` — Discord gateway adapter
- `crates/openfang-runtime/src/prompt_builder.rs` — System prompt assembly
- `crates/openfang-kernel/src/kernel.rs` — Core kernel, agent management

---

## 6. COMMON HALLUCINATIONS TO CATCH

### 6.1 "Agents don't have web access"
**WRONG.** All agents have browser + web_search + shell(curl). See §2.2.

### 6.2 "I need to do the research directly"
**WRONG.** Research should be delegated to research sub-agents. See §2.3.

### 6.3 "DeepSeek is slow"
**PARTIALLY WRONG.** deepseek-chat is fast (<5s for short responses).
What's slow is large session contexts (39K+ tokens) + verbose output requests.
The fix is session compaction, not blaming the model.

### 6.4 "We need to give agents API access to X"
**CHECK FIRST.** Agents already have shell access. They can curl any API.
Don't build a custom integration when `shell: curl -s` already works.

### 6.5 "This feature doesn't exist in the kernel"
**VERIFY.** Read the actual crate code before claiming something doesn't exist.
Many features are built but undocumented. Check: kernel.rs, channel_bridge.rs,
discord.rs, prompt_builder.rs, openai.rs.

### 6.6 "PDFs should be downloaded to view"
**WRONG for Discord.** Use Discord embeds for inline content.
PDFs can't be viewed inline in Discord. If content needs to be visible
inside Discord, use embeds. If it needs to be a document, use embeds
AND attach the PDF as supplementary.

---

## 7. AUDITOR ENFORCEMENT PROTOCOL

When the Auditor detects a violation:

### 7.1 Severity Levels
- **CRITICAL** — Agent hallucinating core architecture facts (§6), wasting significant tokens
- **HIGH** — CTO doing sub-agent work, token quota approaching limit
- **MEDIUM** — Suboptimal delegation, unnecessary complexity
- **LOW** — Style issues, minor inefficiencies

### 7.2 Response Protocol
1. Post to #audit-log with violation details
2. For CRITICAL/HIGH: Inject correction into #agent-prompting
3. For token issues: Notify Neural Net to compact affected session
4. Log all violations for trend analysis

### 7.3 Correction Format
```
⚠️ ARCHITECTURE VIOLATION [SEVERITY]
Agent: [who violated]
Violation: [what happened]
Dogma Reference: §[section]
Correction: [what should have happened]
Action Taken: [what the Auditor did about it]
```

---

*This document is the law. Everything else is commentary.*
*Last updated: 2026-03-01T03:00Z*
