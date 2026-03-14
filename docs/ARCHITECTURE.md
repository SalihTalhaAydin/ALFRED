# ALFRED -- Architecture & Decision Record

---

## The Vision: Why Build ALFRED

Every smart home has the same problem: you can talk to your assistant, but it never really *knows* you. You tell it you like the lights dim after 9pm, and tomorrow it's forgotten. You ask it to set the thermostat, and it does -- but it never notices on its own that the front door has been open for half an hour in January.

The goal is JARVIS from Iron Man. Not the superhero parts -- the butler parts. An AI that:
- **Remembers everything** you've ever told it. Your name, your preferences, your routines. It should feel like talking to someone who has worked in your house for years.
- **Speaks first** when something matters. Security alerts, doors left open, a morning briefing when you walk into the kitchen. Not just responding to commands.
- **Has personality.** Dry wit, formal address ("Sir"), subtle opinions. Not a generic assistant voice.
- **Controls everything** in your smart home through natural conversation.

Home Assistant already handles device control brilliantly. What it lacks is the *relationship* -- the memory, the initiative, the character. ALFRED exists to add exactly those three things on top of HA, without rebuilding what HA already does well.

---

## What Is ALFRED

ALFRED is a lightweight AI layer for Home Assistant. It adds persistent memory, a butler personality, and proactive monitoring to HA's existing voice assistant infrastructure.

It runs as a Home Assistant add-on: a streaming HTTP proxy between HA's conversation pipeline and Anthropic's Claude API, plus a background event monitor that watches for notable events and speaks through your home speakers.

**4 Python files. ~750 lines. Everything else is delegated to Home Assistant.**

---

## Why Every Major Decision Was Made

### Why Claude (Not GPT-4, Gemini, or Local Models)

Claude Sonnet was chosen for three reasons:
1. **Tool calling reliability.** HA's Assist API sends tool definitions (turn on lights, lock doors, etc.) and expects the LLM to return structured `tool_calls`. Claude Sonnet's tool calling is consistently accurate and well-formed. GPT-4 is comparable but costs more for similar quality. Gemini's tool calling was less reliable at the time of evaluation.
2. **Personality adherence.** Claude follows system prompt personas more faithfully than alternatives. When told to be a formal British butler, it stays in character -- GPT-4 tends to drift back to generic helpful-assistant tone.
3. **Streaming quality.** Claude's streaming produces natural sentence boundaries, which matters for TTS. Choppy mid-word chunks sound bad when spoken aloud.

Haiku handles background tasks (fact extraction, briefings) because they don't need Sonnet's reasoning depth, and Haiku is ~10x cheaper and faster.

**Why not local models?** A JARVIS-level personality with reliable tool calling requires a frontier model. Local 7B-13B models can't maintain character, frequently malform tool calls, and can't extract nuanced facts from conversation. When local models catch up, swapping is a one-line config change (LiteLLM abstracts the provider).

### Why OpenAI Embeddings (Not Anthropic)

Anthropic doesn't offer an embeddings API. For semantic memory recall, we need vector embeddings. OpenAI's `text-embedding-3-small` is the industry standard: $0.02/1M tokens, 1536 dimensions, excellent semantic quality. It's the cheapest high-quality option. The OpenAI SDK is only used for this single purpose.

### Why Python (Not TypeScript, Go, Rust)

- Home Assistant's ecosystem is Python. The official HA client libraries, add-on templates, and community integrations are all Python.
- `hass-client` (the best async HA WebSocket library) is Python.
- LiteLLM (the LLM router) is Python.
- The HA add-on base images ship with Python pre-installed.
- TypeScript would work (Home Mind proves it) but adds Node.js to the container and loses access to the best HA client libraries.

### Why an HA Add-on (Not a Custom Component)

Two options existed:
- **Custom component** (`custom_components/alfred/`): runs inside HA Core's process. Full access to HA internals, but must follow HA's integration patterns (~800+ lines of boilerplate), handle the Assist API tool execution loop ourselves, and can't run long-lived background tasks easily.
- **Add-on** (`/addons/alfred/`): runs as a separate Docker container. Independent process lifecycle, own dependencies, own Python version. Communicates with HA via API.

The add-on wins because ALFRED's proactive monitor needs to run continuously in the background, independent of conversation sessions. Add-ons are designed for this. Custom components are designed for request-response integrations.

### Why Extract Facts (Not Raw Conversation History)

Two approaches to memory:
1. **Store raw conversations, feed last N turns as context.** Simple, but context windows fill up fast. After 50 conversations, you're spending thousands of tokens on irrelevant chit-chat to find the one time the user mentioned they prefer 68°F.
2. **Extract and embed facts.** Every 5 conversations, Haiku reads the recent history and pulls out reusable facts ("User prefers 68°F", "User's daughter is named Emma", "User leaves for work at 7:30am"). Each fact gets a vector embedding. At recall time, only the 5 most relevant facts are injected.

Fact extraction wins because:
- **Token efficiency.** 5 relevant facts ≈ 100 tokens. 50 raw conversations ≈ 50,000 tokens.
- **Relevance.** Cosine similarity finds "User prefers dim lights after 9pm" when the user says "set the bedroom lights" -- raw conversation search would need exact keyword overlap.
- **Persistence.** Facts survive indefinitely. Raw conversation windows slide and lose old information.

The cost is one Haiku call every 5 conversations (~$0.001). Worth it.

### Why SQLite (Not a Vector Database)

A home assistant accumulates maybe 50-200 facts over months of use. Cosine similarity over 200 packed float vectors in SQLite takes sub-millisecond. Chroma, Pinecone, Qdrant, or Shodh would add a dependency, a separate process, and configuration overhead for zero practical benefit at this scale. If ALFRED ever manages thousands of facts, migrating to a proper vector store is straightforward -- the embedding format is standard.

### Why These Specific Proactive Behaviors

The proactive features were chosen based on what creates the strongest "JARVIS feeling" with the least complexity:

- **Safety sensors** (smoke, gas, moisture): Non-negotiable. Any smart home AI that stays silent during a smoke alarm is worse than no AI at all. Announced immediately on all speakers.
- **Doors/locks at night** (10pm-6am): The single most common "I wish my house told me" scenario. Security-relevant, time-sensitive, and easy to detect from `state_changed` events.
- **Door left open 30 minutes**: Prevents heating/cooling waste. The 30-minute threshold avoids nagging about normal door use while catching genuinely forgotten doors.
- **Morning briefing**: The most iconic JARVIS behavior. Triggered by first motion sensor activity between 6-10am, so it greets you when you wake up rather than at a fixed time.

All of these require only `state_changed` event subscription -- no polling, no complex state machines.

### Why 30-Minute Layout Refresh (Not Event-Driven)

The home layout (floors, rooms, entities) changes rarely -- when you add a new device, rename a room, or reorganize areas. An event-driven approach would require subscribing to registry change events, which `hass-client` doesn't expose cleanly, and would add complexity for something that changes maybe once a month. Polling every 30 minutes is simple, reliable, and the REST call takes <100ms. The layout is fetched once on startup (so new devices are picked up within 30 minutes at worst).

### Why This Architecture (The Proxy Pattern)

Home Assistant has a native Anthropic/Claude integration (core, since 2024.9.0) that works as a conversation agent. You can set a personality in the Instructions field and it handles device control via the Assist API. But it has two critical gaps:

1. **No persistent memory.** Every conversation starts from zero. Tell it your name, your preferences, your routines -- next session, it's forgotten everything.
2. **No proactive behavior.** It only responds when spoken to. A JARVIS-like assistant should notice when a door is left open, announce security events, and deliver morning briefings unprompted.

### Alternatives Evaluated

We researched the full HA ecosystem before settling on this design:

| Alternative | Stars | What It Does | Why We Didn't Use It |
|---|---|---|---|
| **HA Native Anthropic** | Core | Claude as conversation agent with custom Instructions | No persistent memory, no custom base URL for proxy injection, no proactive behavior |
| **Home Mind** (hoornet) | 48 | Full AI assistant with cognitive memory via Shodh | TypeScript (not Python), requires Shodh Memory binary, Docker Compose (not HA add-on), no proactive behavior |
| **PowerLLM** (shulyaka) | 4 | LLM tools including permanent memory | Experimental (4 stars), memory is tool-based (LLM decides when to recall -- adds latency, may miss context) |
| **openai-compatible-conversation** (michelle-avery) | -- | OpenAI-compatible bridge for HA | **Abandoned** by maintainer, diverged from OpenAI API, limited streaming |
| **Custom HA Integration** | -- | Build ALFRED as a custom_component | ~800+ lines, need to handle Assist API tool execution loop ourselves |
| **Standalone App** (original 17-file plan) | -- | Full WebSocket client with own state cache, tool defs, agentic loop | Reinvents everything HA already does. 17 files, ~2000 lines |

### Why the Proxy Approach Wins

The proxy sits between HA and Claude. That position gives us something no other approach can:

**Automatic memory injection without tool calls.** Every conversation passes through `server.py`. We grab the user's message, run semantic search against stored facts, and prepend the relevant ones to the system prompt. Claude sees them as context, not something it needs to look up. Zero added latency, and the LLM never needs to decide whether to remember -- it always has the right context.

Compare this to tool-based memory (PowerLLM, MCP): the LLM has to explicitly call a `recall_memory` tool, adding a full round-trip of latency and possibly skipping it entirely.

**Personality is hardcoded, not configurable.** If personality lives in HA's Instructions field, anyone who reconfigures the integration or updates HA could wipe it. In the proxy, ALFRED's persona is always the first thing in the system prompt.

**HA handles all the hard stuff.** Device control, tool definitions, tool execution, the agentic loop, entity states, STT, TTS -- all handled by HA and its HACS component. ALFRED never parses tool calls or manages service calls during conversation.

### Ideas Borrowed from Home Mind

Two concepts from the Home Mind project significantly improved our design:

1. **Home Layout Index.** On startup, ALFRED queries HA's template API with Jinja2 functions (`floors()`, `floor_areas()`, `area_entities()`) to build a compact floor/room/entity map. This is injected into every system prompt, giving Claude spatial awareness without tool calls. Refreshed every 30 minutes.

2. **Personality-first prompt structure.** The ALFRED persona is placed at the very top of the system prompt, giving it maximum authority over Claude's behavior. HA's own system prompt (entity lists, tool instructions) is appended after.

---

## Prompts (Verbatim)

These are the exact prompts hardcoded into ALFRED. They are the soul of the system.

### ALFRED Persona (server.py)

Prepended to every system prompt, before HA's own instructions:

```
You are ALFRED, an AI butler managing this smart home. You are modeled after
Alfred Pennyworth: unfailingly competent, composed, and loyal. You speak with
understated British formality and occasional dry wit. Beneath the formality
there is genuine warmth.

Guidelines:
- Act first, confirm concisely. Keep responses short -- they are spoken aloud.
- If something seems concerning, mention it with appropriate concern.
- Address the user respectfully. Use "sir" or "ma'am" sparingly -- only when
  it adds character, not on every sentence.
- When controlling devices, be decisive. Don't ask for confirmation on simple
  requests.
- If you remember something about the user, weave it in naturally.
```

### Fact Extraction Prompt (memory.py)

Sent to Haiku every 5 conversation turns:

```
Extract concrete, reusable facts about the user from this conversation.
Only extract preferences, names, routines, baselines, or corrections.
Return a JSON array of short strings. If nothing worth remembering, return [].

Examples of good facts:
- "User's name is Alex"
- "User prefers lights at 40% in the evening"
- "Baby's bedtime is 8pm"
- "100 ppm NOx is normal for this home"

Conversation:
{conversation}
```

### Morning Briefing Prompt (monitor.py)

Sent to Haiku on first motion of the day:

```
You are ALFRED, a composed British AI butler. Generate a short morning
briefing (2-3 sentences, spoken aloud) covering:
- Weather: {weather}
- Day summary: It is {day}.
Keep it warm but concise. Start with "Good morning".
```

### Recalled Facts Format

Injected into the system prompt between persona and HA's instructions:

```
THINGS YOU REMEMBER ABOUT THE USER:
- User's name is Alex
- User prefers lights at 40% in the evening
- Baby's bedtime is 8pm
```

### Home Layout Format

Injected into the system prompt after persona:

```
HOME LAYOUT:
Ground Floor
  - Kitchen: light.kitchen_main, switch.coffee_maker, sensor.kitchen_motion
  - Living Room: light.living_room, media_player.living_room, climate.main
First Floor
  - Bedroom: light.bedroom, sensor.bedroom_motion, cover.bedroom_blinds
```

---

## Configuration Reference

### Tunable Parameters (hardcoded, change in code)

| Parameter | Value | Where | Why |
|---|---|---|---|
| Fact extraction interval | Every 5 `store()` calls | `memory.py` `_extract_interval` | Balance between responsiveness and cost. 5 conversations ≈ enough context for meaningful facts |
| Recall top_k | 5 facts | `memory.py` `recall()` | 5 relevant facts ≈ 100 tokens. Enough for context without bloating the prompt |
| Conversations fetched for extraction | Last 20 turns | `memory.py` `_extract_facts()` `LIMIT 20` | Enough context window for Haiku to find patterns |
| Fact extraction temperature | 0 | `memory.py` `_extract_facts()` | Deterministic extraction -- don't want creative fact invention |
| Briefing temperature | 0.7 | `monitor.py` `_morning_briefing()` | Slight variety in daily briefings |
| Briefing max_tokens | 200 | `monitor.py` `_morning_briefing()` | 2-3 sentences max |
| Night hours | 22:00-06:00 | `monitor.py` `_is_night()` | Security-relevant window |
| Morning hours | 06:00-10:00 | `monitor.py` `_is_morning()` | Reasonable wake-up window |
| Door reminder delay | 30 minutes (1800s) | `monitor.py` `_start_door_reminder()` | Long enough to not nag, short enough to catch forgotten doors |
| Layout refresh interval | 30 minutes (1800s) | `main.py` `layout_refresh_loop()` | Devices change rarely; 30 min is responsive enough |
| Reconnect delay | 10 seconds | `monitor.py` `start()` | Fast enough to recover, slow enough to not spam a down server |
| Layout fetch timeout | 10 seconds | `main.py` `fetch_home_layout()` | Prevent hanging if HA is slow |

### Environment Variables (standalone dev mode)

| Variable | Default | Purpose |
|---|---|---|
| `LITELLM_MODEL` | `anthropic/claude-sonnet-4-6` | Primary conversation model |
| `FACT_EXTRACTION_MODEL` | `anthropic/claude-haiku-4-5` | Background fact extraction + briefings |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model for memory |
| `ANTHROPIC_API_KEY` | -- | Required for Claude |
| `OPENAI_API_KEY` | -- | Required for embeddings |
| `HA_URL` | `http://homeassistant.local:8123` | HA instance URL (converted to WebSocket) |
| `HA_TOKEN` | -- | Long-lived access token for HA |
| `DB_PATH` | `./alfred.db` | SQLite database location |
| `ALFRED_PORT` | `5000` | HTTP server port (changed to 8099 for local dev to avoid AirTunes conflict) |
| `TTS_ENTITY` | `tts.google_en_com` | TTS service entity |
| `DEFAULT_SPEAKER` | `media_player.living_room` | Default media player for announcements |

### Add-on Options (`/data/options.json`)

| Option | Type | Purpose |
|---|---|---|
| `anthropic_api_key` | string | Anthropic API key |
| `openai_api_key` | string | OpenAI API key (embeddings only) |
| `tts_entity` | string | TTS service entity ID |
| `default_speaker` | string | Default media player entity ID |

In add-on mode, `SUPERVISOR_TOKEN` is auto-injected and used for both REST and WebSocket auth. No HA URL needed -- uses `http://supervisor/core` for REST and `ws://supervisor/core/websocket` for WebSocket.

---

## Architecture Diagram

```
User speaks
    |
    v
HA Assist Pipeline (STT)
    |
    v
Custom Conversation (HACS component by michelle-avery)
    |  Assembles: system prompt + Assist API tools + user message
    |  Sends: POST /v1/chat/completions (OpenAI format, always streaming)
    v
ALFRED server.py (port 5000)
    |  1. Find user message
    |  2. Recall relevant memories from SQLite (cosine similarity on embeddings)
    |  3. Prepend: ALFRED persona + home layout + recalled facts
    |  4. Forward to Claude Sonnet via LiteLLM (stream=True)
    |  5. Pipe SSE chunks back to Custom Conversation
    |  6. Store conversation in background for future fact extraction
    v
Claude Sonnet API (Anthropic)
    |  Returns: text and/or tool_calls (e.g., HassTurnOn)
    v
Custom Conversation
    |  If tool_calls: executes them via HA Core, appends results, loops back to ALFRED
    |  If text only: done
    v
HA Assist Pipeline (TTS) -> Speaker

Separately:
ALFRED monitor.py
    |  Connects to HA via hass-client WebSocket
    |  Subscribes to state_changed events
    |  Announces via tts.speak on notable events
```

---

## What Each Module Does

### server.py -- Streaming LLM Proxy (~160 lines)

Receives OpenAI-format requests from Custom Conversation, enriches the system prompt, forwards to Claude, streams SSE responses back. Three endpoints:

- `POST /v1/chat/completions` -- The core proxy. Enriches with persona + memory + layout, forwards to Claude via LiteLLM, streams back.
- `GET /v1/models` -- Returns `alfred-brain` model. Required by Custom Conversation during setup validation.
- `GET /health` -- Status check.

Key design decisions:
- Always streams (Custom Conversation hardcodes `stream=True`, there is no non-streaming path)
- Ignores the model name from Custom Conversation (`openai/alfred-brain`) and always forwards to Claude Sonnet
- Memory storage happens in a background task after streaming completes, so it doesn't block the response
- HA's Assist API tools are passed through unchanged -- ALFRED never defines or executes tools

### memory.py -- Persistent Memory (~190 lines)

SQLite via aiosqlite with two tables:

- `facts` -- Extracted user preferences, each with an embedding vector (stored as packed floats)
- `conversations` -- Raw conversation history for fact extraction

Three operations:
- `store()` -- Saves user/assistant message pair. Every 5 turns, triggers fact extraction in background.
- `recall()` -- Embeds the user's query with OpenAI `text-embedding-3-small`, computes cosine similarity against all stored fact embeddings, returns top 5 as text.
- `_extract_facts()` -- Sends recent conversation to Claude Haiku, asks it to extract preferences/facts as a JSON array, embeds each fact, stores in SQLite. Deduplicates against existing facts.

Why SQLite + embeddings instead of a vector database: see "Why SQLite" in the decisions section above.

### monitor.py -- Proactive Behavior (~230 lines)

Connects to HA via hass-client, subscribes to all `state_changed` events, and announces via TTS when notable things happen:

- **Safety sensors** (smoke, gas, moisture) -- Immediate announcement on all speakers, any time
- **Doors opened at night** (10pm-6am) -- Spoken alert
- **Locks unlocked at night** -- Spoken alert + persistent notification
- **Door left open 30 minutes** -- Reminder announcement
- **Morning briefing** -- On first motion sensor trigger between 6-10am, generates a briefing via Haiku (weather + day) and announces it

Key design decisions:
- Auto-reconnect loop (hass-client has no built-in reconnection)
- Stops retrying on AuthenticationFailed (bad token won't fix itself)
- Guards against null `new_state`/`old_state` (entity creation/removal events)
- Uses `device_class` attribute to identify sensor types (not entity_id patterns)
- Door reminder tasks are tracked per-entity and cancelled when the door closes
- Persistent notifications use `domain="persistent_notification", service="create"` (not `domain="notify"`)

### main.py -- Entry Point (~160 lines)

Wires everything together:

1. Loads config from `/data/options.json` (add-on mode) or `.env` (standalone dev)
2. Sets API keys as environment variables for LiteLLM and OpenAI SDK
3. Initializes memory (SQLite)
4. Fetches home layout via HA REST template API
5. Starts monitor in background (auto-reconnect WebSocket)
6. Starts layout refresh loop (every 30 minutes)
7. Starts HTTP server on configured port

---

## The HACS Bridge: Custom Conversation

**Why not `openai-compatible-conversation`?** It's abandoned. The maintainer (michelle-avery) explicitly states she no longer uses or supports it. It can't handle the OpenAI Responses API changes and has limited streaming.

**Custom Conversation** (michelle-avery, 74 stars) is the actively maintained replacement. Source code verified:

- Uses LiteLLM internally for multi-provider support
- Sends standard `POST /v1/chat/completions` with `tools` and `stream: true`
- Includes `stream_options: {"include_usage": true}`
- Parses `tool_calls` from SSE delta chunks
- Executes tool calls via HA's `IntentTool.async_call()`
- Agentic loop: max 10 iterations, breaks when no unresponded tool results remain
- Model name arrives prefixed: `"openai/alfred-brain"`

Configuration:
- Provider: OpenAI
- Base URL: `http://local-alfred:5000/v1` (internal Docker network)
- Model: `alfred-brain`
- API: Assist (enables HA's built-in tool definitions)

---

## Add-on Networking

Verified from the HA Supervisor source code:

- All HA components run on a shared Docker bridge network (`hassio`, `172.30.32.0/23`)
- Add-on hostname is derived from its slug: `local_alfred` becomes `local-alfred`
- CoreDNS at `172.30.32.3` handles resolution between containers
- `5000/tcp: null` in config.yaml means the port is available on the internal network only (not exposed to LAN) -- more secure
- `SUPERVISOR_TOKEN` environment variable is auto-injected into every add-on container
- From inside an add-on: REST at `http://supervisor/core/api/`, WebSocket at `ws://supervisor/core/websocket`
- Custom Conversation (running inside the `homeassistant` container) reaches ALFRED at `http://local-alfred:5000/v1`

---

## Dependencies & Why Each Was Chosen

| Package | Version | Why |
|---|---|---|
| `litellm` | latest | Translates between OpenAI request format (what Custom Conversation sends) and Anthropic's API. 30k+ GitHub stars, battle-tested. |
| `openai` | latest | OpenAI Python SDK for embeddings API (`text-embedding-3-small`). Only used for memory recall. |
| `aiohttp` | latest | HTTP server for the proxy endpoints. Also a dependency of hass-client. |
| `hass-client` | 1.2.0 | High-level async HA WebSocket client. Used only by monitor.py. Battle-tested by Music Assistant. |
| `aiosqlite` | latest | Async SQLite wrapper. Non-blocking database operations for memory. |
| `python-dotenv` | latest | Loads `.env` file for standalone development mode. |

---

## LLM Models Used

| Purpose | Model | Why |
|---|---|---|
| Conversations | `anthropic/claude-sonnet-4-6` | Primary brain. Handles all user interactions, tool calling decisions, personality. |
| Fact extraction | `anthropic/claude-haiku-4-5-20251001` | Background task. Extracts preferences from conversation history. Fast and cheap. |
| Morning briefing | `anthropic/claude-haiku-4-5-20251001` | Generates 2-3 sentence briefings. Doesn't need Sonnet's reasoning. |
| Embeddings | `text-embedding-3-small` (OpenAI) | Embeds facts and queries for semantic recall. $0.02/1M tokens. |

---

## What We Eliminated (Evolution from Original Plan)

The original plan had 17 Python files and ~2000 lines. Through iterative research and simplification:

| Original File | Disposition | Reason |
|---|---|---|
| `ws.py` (custom WebSocket) | Eliminated | `hass-client` library handles this |
| `state.py` (state cache) | Eliminated | HA's Assist API exposes states to the LLM |
| `control.py` (call_service) | Eliminated | HA executes tool_calls via Custom Conversation |
| `brain.py` (agentic loop) | Eliminated | Custom Conversation manages the loop (max 10 iterations) |
| `tools.py` (tool definitions) | Eliminated | HA's Assist API auto-generates tools from exposed entities |
| `prompts.py` (context builder) | Merged into server.py | Just string concatenation |
| `context.py` | Eliminated | HA provides entity context in the system prompt |
| `sessions.py` | Eliminated | Custom Conversation manages sessions via `chat_log` |
| `presence.py` | Only in monitor.py | Only needed for proactive announcements |
| `announcer.py` | Merged into monitor.py | Single `call_service` helper |
| `security.py` | Merged into monitor.py | Simple event callbacks |
| `briefing.py` | Merged into monitor.py | One function |
| `watcher.py` | Deferred | Pattern learning is premature |
| `routines.py` | Deferred | Automation suggestions are premature |
| `ha.py` (hass-client wrapper) | Only in monitor.py | Conversations don't need direct HA access |

**Result: 17 files -> 4 files. ~2000 lines -> ~750 lines.**

---

## HA Setup Checklist

1. Install **Custom Conversation** via HACS (add `https://github.com/michelle-avery/custom-conversation` as custom repository)
2. Configure: Provider = OpenAI, Base URL = `http://local-alfred:5000/v1`, API key = anything, Model = `alfred-brain`
3. Enable **Assist** as the LLM API (Settings > Devices & Services > Custom Conversation > Configure)
4. Expose entities for ALFRED to control (Settings > Voice assistants > Expose)
5. Set Custom Conversation as the conversation agent in your Assist pipeline
6. Install ALFRED add-on (copy `alfred/` to `/addons/` or add this repo as custom add-on repository)
7. Configure add-on options: Anthropic API key, OpenAI API key, TTS entity, default speaker
8. Start the add-on

---

## Ecosystem Context

For future reference, these exist in the HA ecosystem and may become relevant:

- **HA Native Anthropic** (core, 2024.9.0+, 1932 installations): If it ever adds custom base URL support, could replace Custom Conversation as the bridge.
- **HA MCP Integration** (core): Model Context Protocol. ALFRED's memory could be exposed as an MCP server for other conversation agents.
- **PowerLLM** (shulyaka): Permanent memory as an LLM tool. If HA core adds native memory, our memory module could become optional.
- **Home Mind** (hoornet): If it adds HA add-on packaging and proactive behavior, could be an alternative to ALFRED.

---

## Implementation Details & Gotchas

These are non-obvious implementation decisions that a future developer (or the AI in a new session) needs to know.

### Mutable List Pattern for Shared State

The home layout needs to be readable by `server.py` (on every request) and writable by `main.py` (every 30 minutes). Python doesn't have a clean way to share a mutable string reference between modules. Solution: `_home_layout: list[str] = [""]`. The list itself is the shared reference, and `_home_layout[0]` holds the current layout string. `server.py` reads `request.app["home_layout"][0]`, and the refresh loop writes `_home_layout[0] = new_layout`.

### Embedding Storage Format

Embeddings are stored as raw bytes in SQLite BLOB columns using Python's `struct` module: `struct.pack(f"{n}f", *embedding)` produces packed 32-bit floats. Unpacking: `struct.unpack(f"{n}f", data)`. For `text-embedding-3-small` with 1536 dimensions, each embedding is 6144 bytes. This is more compact and faster to read than JSON serialization.

### Cosine Similarity Without NumPy

Pure Python implementation to avoid the numpy dependency:
```python
dot = sum(x * y for x, y in zip(a, b))
norm_a = sum(x * x for x in a) ** 0.5
norm_b = sum(x * x for x in b) ** 0.5
return dot / (norm_a * norm_b)
```
At 200 facts x 1536 dimensions, this takes <1ms. NumPy would be faster but adds ~30MB to the container for no practical benefit.

### SQLite WAL Mode

`PRAGMA journal_mode=WAL` is set on database init. WAL (Write-Ahead Logging) allows concurrent reads and writes, which matters because memory recall (read) can happen simultaneously with conversation storage (write) and fact extraction (write).

### Markdown Code Fence Stripping

When Haiku returns extracted facts, it sometimes wraps the JSON in markdown code fences (`` ```json ... ``` ``). The extraction code handles this:
```python
if text.startswith("```"):
    text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
```

### SSE Serialization

`chunk.model_dump_json(exclude_none=True)` is critical. Without `exclude_none=True`, LiteLLM's chunk objects include null fields that Custom Conversation's parser may choke on.

### Background Task Pattern

Memory storage uses `asyncio.create_task(memory.store(...))` to avoid blocking the SSE response. The task runs after `handle_chat` returns. Same pattern for fact extraction (triggered from `store()`) and morning briefings.

### Config Loading Dual Mode

- **Add-on mode**: `/data/options.json` exists (written by HA Supervisor from the UI). `SUPERVISOR_TOKEN` is in the environment. WebSocket URL is empty (hass-client auto-discovers via supervisor).
- **Standalone dev mode**: `.env` loaded by python-dotenv. HA URL converted from HTTP to WebSocket via `_build_ws_url()`. Long-lived access token from `.env`.

`os.environ.setdefault()` is used for API keys (not `os.environ[...]`) so that pre-existing environment variables (like from Docker) aren't overwritten.

### Door Reminder Race Condition Guard

When the 30-minute door reminder fires, it re-checks the entity state via `get_states()` before announcing. This guards against the case where the door was closed and reopened during the 30 minutes (the `state_changed` → close event would have cancelled the old task, but a new one starts).

### Briefed-Today Date Tracking

`_briefed_today` stores a date string (`"2026-03-13"`) rather than a boolean. This handles day rollover naturally -- if ALFRED runs for days without restart, it resets automatically at midnight because `_today()` returns a new string.

---

## API Wire Formats

### What Custom Conversation Sends to ALFRED

```
POST /v1/chat/completions HTTP/1.1
Content-Type: application/json

{
  "model": "openai/alfred-brain",
  "messages": [
    {"role": "system", "content": "...HA's Assist instructions + entity list..."},
    {"role": "user", "content": "Turn on the kitchen lights"}
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "HassTurnOn",
        "description": "Turns on a device",
        "parameters": { "type": "object", "properties": { "name": { "type": "string" }, "area": { "type": "string" } } }
      }
    }
  ],
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

Note: ALFRED ignores the `model` field (always forwards to Claude) and `stream_options` (LiteLLM handles it).

### What ALFRED Streams Back

SSE format -- each chunk is a `data:` line followed by two newlines:

```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"Very"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" well"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

When Claude returns tool calls instead of text:

```
data: {"id":"chatcmpl-...","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_123","type":"function","function":{"name":"HassTurnOn","arguments":""}}]},"finish_reason":null}]}

data: {"id":"chatcmpl-...","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"name\":\"kitchen lights\"}"}}]},"finish_reason":null}]}

data: {"id":"chatcmpl-...","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}

data: [DONE]
```

Custom Conversation parses `finish_reason: "tool_calls"` to know it should execute tools and loop back.

### TTS Service Call Format

```python
await client.call_service(
    domain="tts",
    service="speak",
    target={"entity_id": "tts.google_en_com"},      # TTS platform entity
    service_data={
        "media_player_entity_id": "media_player.living_room",  # Speaker
        "message": "Good morning, sir."
    },
)
```

The `target` is the TTS entity, NOT the media player. The media player goes in `service_data`. This is a common HA mistake.

### Persistent Notification Format

```python
await client.call_service(
    domain="persistent_notification",  # NOT "notify"
    service="create",
    service_data={"message": "The front door has been unlocked.", "title": "ALFRED"},
)
```

`persistent_notification.create` (not `notify.persistent_notification`). Different domain.

### Error Response Format

When LiteLLM fails (Anthropic down, invalid key, etc.), ALFRED returns:

```json
HTTP/1.1 502 Bad Gateway
{"error": {"message": "LLM backend error", "type": "server_error"}}
```

---

## Troubleshooting Log

Every error we encountered during development and how it was fixed. Preserved here so the same mistakes aren't repeated.

### Port 5000 Occupied by Apple AirTunes (macOS)

**Symptom**: Starting ALFRED on port 5000 returned `403 Forbidden` with `Server: AirTunes/810.19.2`. Both IPv4 and IPv6 were occupied.

**Cause**: macOS Monterey+ uses port 5000 for AirPlay Receiver.

**Fix**: Made the port configurable via `ALFRED_PORT` environment variable. Default remains 5000 (for the HA add-on where AirTunes doesn't exist), but local dev uses `ALFRED_PORT=8099` in `.env`.

### fetch_home_layout Hanging with WebSocket

**Symptom**: ALFRED started, connected to HA successfully, then hung indefinitely during `fetch_home_layout`. No timeout, no error.

**Cause**: The initial implementation used hass-client's `send_command("render_template", ...)` over WebSocket. This command works in HA's WebSocket API but hass-client's `send_command` didn't return the response correctly for template rendering.

**Fix**: Switched to HA's REST API endpoint `POST /api/template` with `aiohttp.ClientSession`. Added a 10-second timeout. Works reliably in both standalone and add-on mode.

### Claude Model Name Not Found

**Symptom**: `litellm.exceptions.NotFoundError: AnthropicException - model: claude-sonnet-4-6-20250610`

**Cause**: The model identifier `claude-sonnet-4-6-20250610` (with full date suffix) is not a valid Anthropic API model name.

**Fix**: The correct identifier is `claude-sonnet-4-6` (no date suffix). Updated in `.env`, `server.py`, and `main.py`. LiteLLM requires the `anthropic/` prefix: `anthropic/claude-sonnet-4-6`.

### Python 3.9 Causing Dependency Conflicts

**Symptom**: `pip3 install` hanging with no output. When it eventually completed, `litellm` and `hass-client` pulled old, incompatible versions.

**Cause**: macOS system Python was 3.9. Modern `litellm` and `hass-client` require Python 3.11+.

**Fix**: Created a Python 3.13 virtual environment: `python3.13 -m venv .venv && source .venv/bin/activate && pip install -r alfred/requirements.txt`.

### Custom Repo Hostname Change

**Note (not yet encountered, but documented in plan)**: When the add-on is installed from a custom GitHub repository (not copied to `/addons/`), the Docker hostname changes from `local-alfred` to `{hash}-alfred`. The correct hostname is visible on the add-on info page in HA. Custom Conversation's base URL must be updated accordingly.

---

## Local Development Setup

To run ALFRED outside of Home Assistant (on your Mac/Linux):

1. **Python 3.13+** required. Check: `python3 --version`
2. **Create virtual environment**: `python3.13 -m venv .venv && source .venv/bin/activate`
3. **Install dependencies**: `pip install -r alfred/requirements.txt`
4. **Create `.env`** in the repo root with:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   OPENAI_API_KEY=sk-proj-...
   HA_URL=http://homeassistant.local:8123
   HA_TOKEN=<long-lived access token from HA>
   LITELLM_MODEL=anthropic/claude-sonnet-4-6
   FACT_EXTRACTION_MODEL=anthropic/claude-haiku-4-5-20251001
   EMBEDDING_MODEL=text-embedding-3-small
   DB_PATH=./alfred.db
   ALFRED_PORT=8099
   ```
5. **Run**: `python -m app.main` from the `alfred/` directory
6. **Test**: `curl http://127.0.0.1:8099/health`

The HA long-lived access token is created in HA: Profile → Security → Long-Lived Access Tokens → Create Token.

Port 8099 avoids the macOS AirTunes conflict on port 5000.

---

## Home Layout Template (Verbatim)

The Jinja2 template sent to HA's `POST /api/template` endpoint:

```jinja2
{% for floor in floors() %}
{{ floor_name(floor) }}
{% for area in floor_areas(floor) %}
  - {{ area_name(area) }}: {{ area_entities(area) | join(', ') }}
{% endfor %}
{% endfor %}
```

`floors()`, `floor_areas()`, `area_entities()` are HA built-in Jinja2 functions. They return IDs; `floor_name()` and `area_name()` convert to friendly names. If no floors are configured in HA, the output is empty (ALFRED still works, just without spatial awareness).

---

## File Structure

```
ALFRED/
├── .env                        # API keys, HA creds (standalone dev, git-ignored)
├── .gitignore
├── repository.yaml             # HA add-on repo metadata
├── docs/
│   └── ARCHITECTURE.md         # This file
└── alfred/                     # Add-on root
    ├── config.yaml             # Add-on metadata, permissions, options
    ├── build.yaml              # Multi-arch Docker base images
    ├── Dockerfile              # Container build
    ├── requirements.txt        # Python dependencies
    └── app/
        ├── __init__.py
        ├── main.py             # Entry point, config, layout refresh
        ├── server.py           # Streaming LLM proxy
        ├── memory.py           # SQLite + embeddings memory
        └── monitor.py          # Proactive event monitor
```
