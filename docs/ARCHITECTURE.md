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

It runs as two components:

1. **ALFRED Server** (`alfred/app/`): A streaming HTTP proxy between HA's conversation pipeline and Anthropic's Claude API, plus a background event monitor.
2. **`alfred_conversation` Custom Component** (`custom_components/alfred_conversation/`): A Home Assistant integration that bridges HA's Assist pipeline to ALFRED's OpenAI-compatible API. This component lives inside HA Core's process and handles the conversation agent registration, tool execution loop, and Assist API integration.

**4 Python files for the server (~750 lines). 6 files for the custom component (~350 lines). Everything else is delegated to Home Assistant.**

---

## How It Actually Works (End-to-End)

```
User speaks
    |
    v
HA Assist Pipeline (STT)
    |
    v
alfred_conversation custom component (inside HA Core)
    |  1. Gathers HA's Assist tools (HassTurnOn, HassSetTemperature, etc.)
    |  2. Builds system prompt from HA's LLM API (entity list, tool instructions)
    |  3. Sends: POST /v1/chat/completions (OpenAI format, stream=false)
    v
ALFRED server.py (port 8099)
    |  1. Finds user message in the request
    |  2. Recalls relevant memories from SQLite (cosine similarity on embeddings)
    |  3. Prepends: ALFRED persona + home layout + recalled facts to system prompt
    |  4. Forwards to Claude Sonnet via LiteLLM (stream=false)
    |  5. Returns JSON response to custom component
    |  6. Stores conversation in background for future fact extraction
    v
Claude Sonnet API (Anthropic)
    |  Returns: text and/or tool_calls (e.g., HassTurnOn)
    v
alfred_conversation custom component
    |  If tool_calls: executes them via HA's llm.async_call_tool(), appends results, loops back
    |  If text only: done
    v
HA Assist Pipeline (TTS) -> Speaker

Separately (runs independently):
ALFRED monitor.py
    |  Connects to HA via hass-client WebSocket
    |  Subscribes to state_changed events
    |  Announces via tts.speak on notable events
```

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

### Why a Custom Component (The Architecture Pivot)

The original plan was to use a third-party HACS integration (Custom Conversation by michelle-avery) as the bridge between HA's Assist pipeline and ALFRED. This failed because:

1. **HA's built-in `openai_conversation` integration** doesn't expose a `base_url` configuration option. It always connects to OpenAI's servers. Can't redirect to ALFRED.
2. **Third-party HACS bridges** like `custom-conversation` or `openai-compatible-conversation` were either abandoned, incompatible with HA 2026.3.0, or caused Python 3.14 import errors.

The solution: build a minimal custom component (`alfred_conversation`) that is a stripped-down version of HA's own `openai_conversation` integration, but with explicit `base_url` configuration pointing to ALFRED's server.

**This gives us:**
- Full control over the bridge code, no dependency on third-party maintainers
- Explicit `base_url` config to point at ALFRED's HTTP server
- HA handles STT/TTS, entity exposure, and the Assist tool definitions
- The custom component handles the agentic tool execution loop (max 10 iterations)
- ALFRED's server handles persona, memory injection, and LLM routing

### Why ALFRED Is Also an Add-on (Future State)

For production deployment, ALFRED's server should run as an HA add-on (separate Docker container) so it has:
- Independent process lifecycle for the proactive monitor
- Own dependencies and Python version
- Clean separation from HA Core's process
- Auto-restart on failure

The custom component is lightweight (just an API bridge) while the heavy lifting (LLM proxy, memory, monitoring) lives in the add-on.

Currently during development, ALFRED's server runs on the developer's Mac. The custom component on the HA machine points to the Mac's IP.

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

### Why the Proxy Pattern Wins

The proxy sits between HA and Claude. That position gives us something no other approach can:

**Automatic memory injection without tool calls.** Every conversation passes through `server.py`. We grab the user's message, run semantic search against stored facts, and prepend the relevant ones to the system prompt. Claude sees them as context, not something it needs to look up. Zero added latency, and the LLM never needs to decide whether to remember -- it always has the right context.

Compare this to tool-based memory (PowerLLM, MCP): the LLM has to explicitly call a `recall_memory` tool, adding a full round-trip of latency and possibly skipping it entirely.

**Personality is hardcoded, not configurable.** If personality lives in HA's Instructions field, anyone who reconfigures the integration or updates HA could wipe it. In the proxy, ALFRED's persona is always the first thing in the system prompt.

**HA handles all the hard stuff.** Device control, tool definitions, entity states, STT, TTS -- all handled by HA. The custom component handles tool execution. ALFRED never parses tool calls or manages service calls during conversation.

### Ideas Borrowed from Home Mind

Two concepts from the Home Mind project significantly improved our design:

1. **Home Layout Index.** On startup, ALFRED queries HA's template API with Jinja2 functions (`floors()`, `floor_areas()`, `area_entities()`) to build a compact floor/room/entity map. This is injected into every system prompt, giving Claude spatial awareness without tool calls. Refreshed every 30 minutes.

2. **Personality-first prompt structure.** The ALFRED persona is placed at the very top of the system prompt, giving it maximum authority over Claude's behavior. HA's own system prompt (entity lists, tool instructions) is appended after.

### Alternatives Evaluated

We researched the full HA ecosystem before settling on this design:

| Alternative | Stars | What It Does | Why We Didn't Use It |
|---|---|---|---|
| **HA Native Anthropic** | Core | Claude as conversation agent with custom Instructions | No persistent memory, no custom base URL for proxy injection, no proactive behavior |
| **HA `openai_conversation`** | Core | OpenAI as conversation agent | No `base_url` config -- always hits OpenAI servers, can't redirect to ALFRED |
| **Custom Conversation** (michelle-avery) | 74 | HACS LLM bridge with multi-provider support | Depends on third-party HACS repo, potential Python 3.14 compat issues |
| **Home Mind** (hoornet) | 48 | Full AI assistant with cognitive memory via Shodh | TypeScript (not Python), requires Shodh Memory binary, Docker Compose (not HA add-on), no proactive behavior |
| **PowerLLM** (shulyaka) | 4 | LLM tools including permanent memory | Experimental (4 stars), memory is tool-based (LLM decides when to recall -- adds latency, may miss context) |
| **openai-compatible-conversation** (michelle-avery) | -- | OpenAI-compatible bridge for HA | **Abandoned** by maintainer, diverged from OpenAI API, limited streaming |
| **Standalone App** (original 17-file plan) | -- | Full WebSocket client with own state cache, tool defs, agentic loop | Reinvents everything HA already does. 17 files, ~2000 lines |

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
| Tool call loop max iterations | 10 | `conversation.py` `MAX_TOOL_ITERATIONS` | Prevent infinite loops if the LLM keeps calling tools |

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

## The Custom Component: `alfred_conversation`

### Why It Exists

HA's native conversation integrations (`openai_conversation`, `anthropic`) don't allow setting a custom `base_url`. They always connect to the official provider endpoints. ALFRED needs the conversation agent to talk to its own server, which enriches prompts with memory and persona before forwarding to Claude.

The `alfred_conversation` custom component is a minimal OpenAI-compatible conversation agent with one key addition: a configurable `base_url` that points to ALFRED's server.

### How It Works

1. **Config flow** (`config_flow.py`): User enters ALFRED's URL during setup. The integration validates connectivity by calling `GET /v1/models`. If it reaches ALFRED and gets back the `alfred-brain` model, setup succeeds.

2. **Init** (`__init__.py`): Creates an `openai.AsyncOpenAI` client pointed at ALFRED's `base_url` with a dummy API key (`"not-needed"` -- ALFRED uses its own Anthropic key). Stores the client as `entry.runtime_data`.

3. **Conversation entity** (`conversation.py`): Registers as a conversation agent in HA. When a user speaks:
   - Gets HA's Assist tools (entity control functions) from the LLM API
   - Builds the HA system prompt (entity list, tool instructions)
   - Sends the request to ALFRED via the OpenAI client (`stream=False`)
   - If Claude returns `tool_calls`, executes them via `llm_api.async_call_tool()` and loops back
   - If Claude returns text, returns it as the spoken response

### Files

| File | Lines | Purpose |
|---|---|---|
| `manifest.json` | 10 | Integration metadata. Declares `openai>=1.30.0` dependency. |
| `const.py` | 18 | Constants: domain name, config keys, defaults. |
| `strings.json` | 17 | UI strings for the config flow. |
| `__init__.py` | 41 | Entry setup: creates OpenAI client with custom `base_url`. |
| `config_flow.py` | 81 | Config flow: URL input form, connectivity validation. |
| `conversation.py` | 273 | Core agent: message handling, tool execution loop. |

### Key Design Decisions in the Custom Component

- **`stream=False`**: The custom component calls ALFRED without streaming. ALFRED's server returns a complete JSON response. This avoids the complexity of parsing SSE chunks inside HA's process and is more reliable for tool call handling. ALFRED still supports streaming for other clients (like direct API testing).

- **No API key needed**: The OpenAI client is created with `api_key="not-needed"`. ALFRED's server doesn't validate API keys -- it uses its own Anthropic key internally.

- **Assist API integration**: The component sets `CONF_LLM_HASS_API: "assist"` in options during setup. This tells HA to provide the full Assist tool set (entity control, automations, scripts) in every conversation.

- **Conversation history**: Maintained in-memory per `conversation_id`. The system prompt is rebuilt fresh each time (to pick up latest HA state), but prior user/assistant messages are preserved within a session.

- **`LLMContext` constructor**: Uses only the parameters available in HA 2026.3.0: `platform`, `context`, `language`, `assistant`, `device_id`. Older parameters like `user_prompt` were removed in this version.

### Deploying the Custom Component

The component files live in `custom_components/alfred_conversation/` on the HA machine at `/config/custom_components/alfred_conversation/`. To deploy:

```bash
sshpass -p '<password>' scp -r custom_components/alfred_conversation/ \
  root@homeassistant.local:/config/custom_components/alfred_conversation/
sshpass -p '<password>' ssh root@homeassistant.local "ha core restart"
```

After restart, add the integration via Settings > Devices & Services > Add Integration > "ALFRED Conversation", or create a config entry via the WebSocket API.

---

## What Each Module Does (ALFRED Server)

### server.py -- LLM Proxy (~170 lines)

Receives OpenAI-format requests from the custom component, enriches the system prompt, forwards to Claude, returns responses. Three endpoints:

- `POST /v1/chat/completions` -- The core proxy. Enriches with persona + memory + layout, forwards to Claude via LiteLLM. Supports both streaming (`stream=true` → SSE) and non-streaming (`stream=false` → JSON) modes.
- `GET /v1/models` -- Returns `alfred-brain` model. Required by the custom component during setup validation.
- `GET /health` -- Status check.

Key design decisions:
- Respects the `stream` parameter from the request body. When `stream=false` (which the custom component uses), returns a standard OpenAI JSON response. When `stream=true`, returns SSE chunks.
- Ignores the model name from the caller (e.g., `alfred-brain`) and always forwards to Claude Sonnet
- Memory storage happens in a background task after the response completes, so it doesn't block
- HA's Assist API tools are passed through unchanged -- ALFRED never defines or executes tools

### memory.py -- Persistent Memory (~190 lines)

SQLite via aiosqlite with two tables:

- `facts` -- Extracted user preferences, each with an embedding vector (stored as packed floats)
- `conversations` -- Raw conversation history for fact extraction

Three operations:
- `store()` -- Saves user/assistant message pair. Every 5 turns, triggers fact extraction in background.
- `recall()` -- Embeds the user's query with OpenAI `text-embedding-3-small`, computes cosine similarity against all stored fact embeddings, returns top 5 as text.
- `_extract_facts()` -- Sends recent conversation to Claude Haiku, asks it to extract preferences/facts as a JSON array, embeds each fact, stores in SQLite. Deduplicates against existing facts.

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

### main.py -- Entry Point (~170 lines)

Wires everything together:

1. Loads config from `/data/options.json` (add-on mode) or `.env` (standalone dev)
2. Sets API keys as environment variables for LiteLLM and OpenAI SDK
3. Initializes memory (SQLite)
4. Fetches home layout via HA REST template API
5. Starts monitor in background (auto-reconnect WebSocket)
6. Starts layout refresh loop (every 30 minutes)
7. Starts HTTP server on configured port

---

## Add-on Networking

Verified from the HA Supervisor source code:

- All HA components run on a shared Docker bridge network (`hassio`, `172.30.32.0/23`)
- Add-on hostname is derived from its slug: `local_alfred` becomes `local-alfred`
- CoreDNS at `172.30.32.3` handles resolution between containers
- `5000/tcp: null` in config.yaml means the port is available on the internal network only (not exposed to LAN) -- more secure
- `SUPERVISOR_TOKEN` environment variable is auto-injected into every add-on container
- From inside an add-on: REST at `http://supervisor/core/api/`, WebSocket at `ws://supervisor/core/websocket`
- The custom component (running inside the `homeassistant` container) reaches ALFRED at `http://local-alfred:5000/v1` (add-on mode) or `http://<developer-ip>:8099/v1` (dev mode)

---

## Dependencies & Why Each Was Chosen

### ALFRED Server

| Package | Version | Why |
|---|---|---|
| `litellm` | latest | Translates between OpenAI request format and Anthropic's API. 30k+ GitHub stars, battle-tested. |
| `openai` | latest | OpenAI Python SDK for embeddings API (`text-embedding-3-small`). Only used for memory recall. |
| `aiohttp` | latest | HTTP server for the proxy endpoints. Also a dependency of hass-client. |
| `hass-client` | 1.2.0 | High-level async HA WebSocket client. Used only by monitor.py. Battle-tested by Music Assistant. |
| `aiosqlite` | latest | Async SQLite wrapper. Non-blocking database operations for memory. |
| `python-dotenv` | latest | Loads `.env` file for standalone development mode. |

### Custom Component

| Package | Version | Why |
|---|---|---|
| `openai` | >=1.30.0 | OpenAI Python SDK used as HTTP client for the OpenAI-compatible API. |
| `voluptuous` | (HA core) | Schema validation for tool parameters. Pre-installed in HA. |
| `voluptuous_openapi` | (HA core) | Converts voluptuous schemas to OpenAI function parameter format. Pre-installed in HA. |

---

## LLM Models Used

| Purpose | Model | Why |
|---|---|---|
| Conversations | `anthropic/claude-sonnet-4-6` | Primary brain. Handles all user interactions, tool calling decisions, personality. |
| Fact extraction | `anthropic/claude-haiku-4-5` | Background task. Extracts preferences from conversation history. Fast and cheap. |
| Morning briefing | `anthropic/claude-haiku-4-5` | Generates 2-3 sentence briefings. Doesn't need Sonnet's reasoning. |
| Embeddings | `text-embedding-3-small` (OpenAI) | Embeds facts and queries for semantic recall. $0.02/1M tokens. |

---

## What We Eliminated (Evolution from Original Plan)

The original plan had 17 Python files and ~2000 lines. Through iterative research and simplification:

| Original File | Disposition | Reason |
|---|---|---|
| `ws.py` (custom WebSocket) | Eliminated | `hass-client` library handles this |
| `state.py` (state cache) | Eliminated | HA's Assist API exposes states to the LLM |
| `control.py` (call_service) | Eliminated | HA executes tool_calls via the custom component |
| `brain.py` (agentic loop) | Eliminated | Custom component manages the loop (max 10 iterations) |
| `tools.py` (tool definitions) | Eliminated | HA's Assist API auto-generates tools from exposed entities |
| `prompts.py` (context builder) | Merged into server.py | Just string concatenation |
| `context.py` | Eliminated | HA provides entity context in the system prompt |
| `sessions.py` | Eliminated | Custom component manages sessions via conversation history |
| `presence.py` | Only in monitor.py | Only needed for proactive announcements |
| `announcer.py` | Merged into monitor.py | Single `call_service` helper |
| `security.py` | Merged into monitor.py | Simple event callbacks |
| `briefing.py` | Merged into monitor.py | One function |
| `watcher.py` | Deferred | Pattern learning is premature |
| `routines.py` | Deferred | Automation suggestions are premature |
| `ha.py` (hass-client wrapper) | Only in monitor.py | Conversations don't need direct HA access |

**Result: 17 files -> 4 server files + 6 custom component files. ~2000 lines -> ~1100 lines total.**

---

## HA Setup Checklist

### For Development (ALFRED Server on Local Machine)

1. **Clone the repo** and create a virtual environment:
   ```bash
   python3.13 -m venv .venv && source .venv/bin/activate
   pip install -r alfred/requirements.txt
   ```

2. **Create `.env`** in the repo root:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   OPENAI_API_KEY=sk-proj-...
   HA_URL=http://homeassistant.local:8123
   HA_TOKEN=<long-lived access token>
   LITELLM_MODEL=anthropic/claude-sonnet-4-6
   FACT_EXTRACTION_MODEL=anthropic/claude-haiku-4-5
   EMBEDDING_MODEL=text-embedding-3-small
   DB_PATH=./alfred.db
   ALFRED_PORT=8099
   ```

3. **Start ALFRED**: `cd alfred && python -m app.main`

4. **Deploy the custom component** to HA:
   ```bash
   sshpass -p '<ssh-password>' scp -r \
     custom_components/alfred_conversation/ \
     root@homeassistant.local:/config/custom_components/alfred_conversation/
   ```

5. **Restart HA Core**:
   ```bash
   sshpass -p '<ssh-password>' ssh root@homeassistant.local "ha core restart"
   ```

6. **Add the integration**: Settings > Devices & Services > Add Integration > "ALFRED Conversation". Enter ALFRED's URL (e.g., `http://192.168.68.105:8099/v1`).

7. **Set ALFRED as the conversation agent** in an Assist pipeline (via UI or WebSocket API).

8. **Expose entities** for ALFRED to control: Settings > Voice assistants > Expose.

### Headless Setup (All via API, No UI)

Everything can be configured programmatically:

```python
# WebSocket: Create config entry
{"type": "config_entries/flow", "handler": "alfred_conversation", ...}

# WebSocket: Set pipeline agent
{"type": "assist_pipeline/pipeline/update", "pipeline_id": "...",
 "conversation_engine": "conversation.alfred", ...}

# WebSocket: Test conversation
{"type": "conversation/process", "text": "Hello ALFRED",
 "agent_id": "conversation.alfred"}
```

### For Production (HA Add-on)

1. Copy `alfred/` to `/addons/` on the HA machine or add the GitHub repo as a custom add-on repository
2. Install and configure the add-on (API keys, TTS entity, speaker)
3. Update the custom component's `base_url` from dev IP to `http://local-alfred:5000/v1`
4. Start the add-on

---

## HA Remote Access Methods

### SSH (Preferred for Headless Operations)

The HA Terminal & SSH add-on (`core_ssh`) exposes port 22. Password is configured in the add-on options.

```bash
# Execute commands
sshpass -p '<password>' ssh root@homeassistant.local "ha core restart"

# Transfer files
sshpass -p '<password>' scp file.py root@homeassistant.local:/config/path/

# View logs
sshpass -p '<password>' ssh root@homeassistant.local "ha core logs | grep alfred"
```

### Supervisor API (via WebSocket)

The Supervisor API is accessible through HA's WebSocket under the `supervisor/api` message type:

```json
{"type": "supervisor/api", "endpoint": "/addons/core_ssh/info", "method": "get"}
```

Cannot be accessed via REST from outside -- returns 401. The long-lived access token only works through the WebSocket proxy.

### HA REST API

Available for template rendering, state queries, and service calls:

```bash
curl -H "Authorization: Bearer $HA_TOKEN" \
  -X POST http://homeassistant.local:8123/api/template \
  -d '{"template": "{{ states.light.kitchen.state }}"}'
```

### HA WebSocket API

For real-time operations: conversation testing, config entry management, pipeline configuration, event subscription.

```python
async with websockets.connect("ws://homeassistant.local:8123/api/websocket") as ws:
    await ws.send(json.dumps({"type": "auth", "access_token": token}))
    # ... send commands
```

---

## Ecosystem Context

For future reference, these exist in the HA ecosystem and may become relevant:

- **HA Native Anthropic** (core, 2024.9.0+, 1932 installations): If it ever adds custom base URL support, could replace the custom component as the bridge.
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

`chunk.model_dump_json(exclude_none=True)` is critical. Without `exclude_none=True`, LiteLLM's chunk objects include null fields that downstream parsers may choke on.

### Background Task Pattern

Memory storage uses `asyncio.create_task(memory.store(...))` to avoid blocking the response. The task runs after `handle_chat` returns. Same pattern for fact extraction (triggered from `store()`) and morning briefings.

### Config Loading Dual Mode

- **Add-on mode**: `/data/options.json` exists (written by HA Supervisor from the UI). `SUPERVISOR_TOKEN` is in the environment. WebSocket URL is empty (hass-client auto-discovers via supervisor).
- **Standalone dev mode**: `.env` loaded by python-dotenv. HA URL converted from HTTP to WebSocket via `_build_ws_url()`. Long-lived access token from `.env`.

`os.environ.setdefault()` is used for API keys (not `os.environ[...]`) so that pre-existing environment variables (like from Docker) aren't overwritten.

### Door Reminder Race Condition Guard

When the 30-minute door reminder fires, it re-checks the entity state via `get_states()` before announcing. This guards against the case where the door was closed and reopened during the 30 minutes (the `state_changed` → close event would have cancelled the old task, but a new one starts).

### Briefed-Today Date Tracking

`_briefed_today` stores a date string (`"2026-03-13"`) rather than a boolean. This handles day rollover naturally -- if ALFRED runs for days without restart, it resets automatically at midnight because `_today()` returns a new string.

### Python 3.14 Syntax Restrictions (HA 2026.3.0)

HA 2026.3.0 runs Python 3.14. Key compatibility issue discovered:

```python
# INVALID in Python 3.14 -- SyntaxError
messages = [
    *messages[1:] if messages else [],  # inline conditional unpacking
    ChatCompletionUserMessageParam(...)
]

# VALID -- explicit construction
prior = messages[1:] if messages else []
new_messages = []
new_messages.extend(prior)
new_messages.append(ChatCompletionUserMessageParam(...))
```

The `*expr if cond else []` syntax inside a list literal is not valid Python 3.14. Always build lists explicitly when conditional unpacking is needed.

### HA 2026.3.0 API Changes

Several HA internal APIs changed from earlier versions:

- `assist_pipeline.async_migrate_engine()` was removed. Do not call it.
- `llm.LLMContext` constructor no longer accepts a `user_prompt` parameter.
- `llm.BASE_PROMPT` no longer exists. Use `llm_api.api_prompt` instead.

---

## API Wire Formats

### What the Custom Component Sends to ALFRED

```
POST /v1/chat/completions HTTP/1.1
Content-Type: application/json

{
  "model": "alfred-brain",
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
  "stream": false,
  "max_tokens": 1024,
  "temperature": 0.7,
  "top_p": 1.0,
  "user": "<conversation_id>"
}
```

### What ALFRED Returns (Non-Streaming)

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1710000000,
  "model": "claude-sonnet-4-6",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Very well, sir. The kitchen lights are now on."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 500, "completion_tokens": 20, "total_tokens": 520}
}
```

When Claude returns tool calls:

```json
{
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_123",
            "type": "function",
            "function": {
              "name": "HassTurnOn",
              "arguments": "{\"name\": \"kitchen lights\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ]
}
```

The custom component parses `finish_reason: "tool_calls"` to know it should execute tools and loop back.

### What ALFRED Streams Back (When stream=true)

SSE format for other clients (e.g., direct API testing):

```
data: {"id":"chatcmpl-...","choices":[{"index":0,"delta":{"role":"assistant","content":"Very"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","choices":[{"index":0,"delta":{"content":" well"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

### TTS Service Call Format

```python
await client.call_service(
    domain="tts",
    service="speak",
    target={"entity_id": "tts.google_en_com"},
    service_data={
        "media_player_entity_id": "media_player.living_room",
        "message": "Good morning, sir."
    },
)
```

The `target` is the TTS entity, NOT the media player. The media player goes in `service_data`. This is a common HA mistake.

### Persistent Notification Format

```python
await client.call_service(
    domain="persistent_notification",
    service="create",
    service_data={"message": "The front door has been unlocked.", "title": "ALFRED"},
)
```

`persistent_notification.create` (not `notify.persistent_notification`). Different domain.

### Error Response Format

When LiteLLM fails (Anthropic down, invalid key, etc.), ALFRED returns:

```json
{"error": {"message": "LLM backend error", "type": "server_error"}}
```
HTTP status 502.

---

## Troubleshooting Log

Every error encountered during development and how it was fixed. Preserved here so the same mistakes aren't repeated.

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

### HA `openai_conversation` Has No `base_url` Config

**Symptom**: After setting up the built-in `openai_conversation` integration, all requests went to OpenAI's servers instead of ALFRED.

**Cause**: HA core's `openai_conversation` integration hardcodes the OpenAI endpoint. There is no `base_url` option in the config flow or options flow.

**Fix**: Abandoned `openai_conversation` entirely. Built the `alfred_conversation` custom component with explicit `base_url` configuration.

### Custom Component Python 3.14 SyntaxError

**Symptom**: `SyntaxError: invalid syntax` at line 173 of `conversation.py` during HA startup.

**Cause**: Python 3.14 (used by HA 2026.3.0) does not allow `*list if condition else []` inline unpacking syntax inside list literals.

**Fix**: Rewrote list construction to use explicit `extend()` / `append()` calls instead of conditional unpacking.

### Custom Component `async_migrate_engine` AttributeError

**Symptom**: `AttributeError: module 'homeassistant.components.assist_pipeline' has no attribute 'async_migrate_engine'` during entity registration.

**Cause**: The `async_migrate_engine` function was removed from HA's `assist_pipeline` module in a recent version.

**Fix**: Removed the call entirely. Pipeline agent assignment is done separately via the WebSocket API or UI.

### Custom Component `LLMContext` Constructor Error

**Symptom**: `TypeError` when constructing `llm.LLMContext` with a `user_prompt` parameter.

**Cause**: HA 2026.3.0 removed the `user_prompt` parameter from `LLMContext.__init__`.

**Fix**: Removed `user_prompt` from the constructor call.

### Custom Component `llm.BASE_PROMPT` Not Found

**Symptom**: `AttributeError: module 'homeassistant.helpers.llm' has no attribute 'BASE_PROMPT'`

**Cause**: `llm.BASE_PROMPT` was removed in HA 2026.3.0.

**Fix**: Use `llm_api.api_prompt` instead, which contains the full Assist system prompt.

### ALFRED Server Returning String Instead of Object

**Symptom**: `AttributeError: 'str' object has no attribute 'choices'` in the custom component.

**Cause**: ALFRED's server always used `stream=True` when calling Claude via LiteLLM, but the custom component called ALFRED with `stream=False`. The OpenAI Python client received SSE text where it expected JSON and returned a raw string.

**Fix**: Made `server.py` respect the `stream` parameter from the request body. When `stream=false`, calls LiteLLM with `stream=False` and returns `web.json_response(result)` instead of SSE.

### Supervisor API Returns 401 from External REST Calls

**Symptom**: `curl` to `http://homeassistant.local:8123/api/hassio/addons/core_ssh/info` returns HTTP 401.

**Cause**: The HA long-lived access token works for HA REST/WebSocket APIs but NOT for Supervisor REST endpoints when called from outside. The Supervisor API requires the `SUPERVISOR_TOKEN` which is only available inside add-on containers.

**Fix**: Access Supervisor API through HA's WebSocket proxy using the `supervisor/api` message type, or use SSH to the HA machine.

### Custom Repo Hostname Change

**Note (not yet encountered, but documented)**: When the add-on is installed from a custom GitHub repository (not copied to `/addons/`), the Docker hostname changes from `local-alfred` to `{hash}-alfred`. The correct hostname is visible on the add-on info page in HA. The custom component's base URL must be updated accordingly.

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
   FACT_EXTRACTION_MODEL=anthropic/claude-haiku-4-5
   EMBEDDING_MODEL=text-embedding-3-small
   DB_PATH=./alfred.db
   ALFRED_PORT=8099
   ```
5. **Run**: `python -m app.main` from the `alfred/` directory
6. **Test**: `curl http://127.0.0.1:8099/health`

The HA long-lived access token is created in HA: Profile > Security > Long-Lived Access Tokens > Create Token.

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
│   ├── ARCHITECTURE.md         # This file
│   └── ALFRED.postman_collection.json  # Postman API tests
├── custom_components/
│   └── alfred_conversation/    # HA custom integration (deployed to /config/)
│       ├── __init__.py         # Entry setup, OpenAI client creation
│       ├── config_flow.py      # Config UI: URL input, connectivity check
│       ├── const.py            # Constants and defaults
│       ├── conversation.py     # Conversation agent, tool execution loop
│       ├── manifest.json       # Integration metadata
│       └── strings.json        # UI localization strings
└── alfred/                     # Server (runs as add-on or standalone)
    ├── config.yaml             # Add-on metadata, permissions, options
    ├── build.yaml              # Multi-arch Docker base images
    ├── Dockerfile              # Container build
    ├── requirements.txt        # Python dependencies
    └── app/
        ├── __init__.py
        ├── main.py             # Entry point, config, layout refresh
        ├── server.py           # LLM proxy (streaming + non-streaming)
        ├── memory.py           # SQLite + embeddings memory
        └── monitor.py          # Proactive event monitor
```

---

## Testing ALFRED

### Quick Health Check

```bash
curl http://localhost:8099/health
```

### Direct API Test (Non-Streaming)

```bash
curl -X POST http://localhost:8099/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "alfred-brain",
    "messages": [{"role": "user", "content": "Hello ALFRED"}],
    "stream": false
  }'
```

### Direct API Test (Streaming)

```bash
curl -X POST http://localhost:8099/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "alfred-brain",
    "messages": [{"role": "user", "content": "Hello ALFRED"}],
    "stream": true
  }'
```

### End-to-End via HA WebSocket

```python
import asyncio, json, websockets

async def test():
    async with websockets.connect("ws://homeassistant.local:8123/api/websocket") as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": "<token>"}))
        await ws.recv()  # auth_ok

        await ws.send(json.dumps({
            "id": 1,
            "type": "conversation/process",
            "text": "Hello ALFRED, introduce yourself.",
            "agent_id": "conversation.alfred"
        }))
        result = json.loads(await ws.recv())
        print(result["result"]["response"]["speech"]["plain"]["speech"])

asyncio.run(test())
```

### Postman

A full Postman collection is available at `docs/ALFRED.postman_collection.json` with all endpoints pre-configured.
