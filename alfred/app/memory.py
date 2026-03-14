"""Persistent memory for ALFRED -- SQLite + embeddings for semantic recall."""

import asyncio
import json
import logging
import struct
import time
from pathlib import Path

import aiosqlite
import litellm
import openai

log = logging.getLogger(__name__)

FACT_EXTRACTION_PROMPT = """\
Extract concrete, reusable facts about the user from this conversation.
Only extract preferences, names, routines, baselines, or corrections.
Return a JSON array of short strings. If nothing worth remembering, return [].

Examples of good facts:
- "User's name is Alex"
- "User prefers lights at 40% in the evening"
- "Baby's bedtime is 8pm"
- "100 ppm NOx is normal for this home"

Conversation:
{conversation}"""


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _pack_embedding(embedding: list[float]) -> bytes:
    return struct.pack(f"{len(embedding)}f", *embedding)


def _unpack_embedding(data: bytes) -> list[float]:
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


class Memory:
    def __init__(self, db_path: str, embedding_model: str, fact_model: str):
        self._db_path = db_path
        self._embedding_model = embedding_model
        self._fact_model = fact_model
        self._db: aiosqlite.Connection | None = None
        self._store_count = 0
        self._extract_interval = 5  # extract facts every N conversation turns

    async def init(self):
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS facts "
            "(id INTEGER PRIMARY KEY, content TEXT, embedding BLOB, created_at REAL)"
        )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS conversations "
            "(id INTEGER PRIMARY KEY, role TEXT, content TEXT, timestamp REAL)"
        )
        await self._db.commit()
        log.info("Memory initialized at %s", self._db_path)

    async def store(self, user_msg: str, assistant_response: str):
        """Save a conversation exchange and periodically extract facts."""
        now = time.time()
        await self._db.execute(
            "INSERT INTO conversations (role, content, timestamp) VALUES (?, ?, ?)",
            ("user", user_msg, now),
        )
        await self._db.execute(
            "INSERT INTO conversations (role, content, timestamp) VALUES (?, ?, ?)",
            ("assistant", assistant_response, now),
        )
        await self._db.commit()

        self._store_count += 1
        if self._store_count % self._extract_interval == 0:
            asyncio.create_task(self._extract_facts())

    async def recall(self, query: str, top_k: int = 5) -> str:
        """Retrieve the most relevant stored facts for a query."""
        rows = await self._db.execute_fetchall(
            "SELECT content, embedding FROM facts"
        )
        if not rows:
            return ""

        try:
            query_embedding = await self._embed(query)
        except Exception:
            log.warning("Embedding failed for recall, returning empty", exc_info=True)
            return ""

        scored = []
        for content, emb_blob in rows:
            stored_emb = _unpack_embedding(emb_blob)
            score = _cosine_similarity(query_embedding, stored_emb)
            scored.append((score, content))

        scored.sort(reverse=True)
        top_facts = [content for _, content in scored[:top_k]]
        if not top_facts:
            return ""
        return "THINGS YOU REMEMBER ABOUT THE USER:\n" + "\n".join(
            f"- {f}" for f in top_facts
        )

    async def _extract_facts(self):
        """Use Haiku to extract facts from recent conversations, embed and store."""
        try:
            rows = await self._db.execute_fetchall(
                "SELECT role, content FROM conversations "
                "ORDER BY timestamp DESC LIMIT 20"
            )
            if not rows:
                return

            conversation_text = "\n".join(
                f"{role}: {content}" for role, content in reversed(rows)
            )
            response = await litellm.acompletion(
                model=self._fact_model,
                messages=[
                    {
                        "role": "user",
                        "content": FACT_EXTRACTION_PROMPT.format(
                            conversation=conversation_text
                        ),
                    }
                ],
                temperature=0,
                max_tokens=500,
            )

            text = response.choices[0].message.content.strip()
            # Parse JSON array from response (handle markdown code fences)
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            facts = json.loads(text)
            if not isinstance(facts, list):
                return

            existing = {
                row[0]
                for row in await self._db.execute_fetchall(
                    "SELECT content FROM facts"
                )
            }

            now = time.time()
            for fact in facts:
                if not isinstance(fact, str) or not fact.strip():
                    continue
                fact = fact.strip()
                if fact in existing:
                    continue
                try:
                    embedding = await self._embed(fact)
                    await self._db.execute(
                        "INSERT INTO facts (content, embedding, created_at) "
                        "VALUES (?, ?, ?)",
                        (fact, _pack_embedding(embedding), now),
                    )
                except Exception:
                    log.warning("Failed to embed fact: %s", fact, exc_info=True)

            await self._db.commit()
            log.info("Extracted %d new facts", len(facts))
        except Exception:
            log.warning("Fact extraction failed", exc_info=True)

    async def _embed(self, text: str) -> list[float]:
        client = openai.AsyncOpenAI()
        response = await client.embeddings.create(
            model=self._embedding_model, input=text
        )
        return response.data[0].embedding

    async def close(self):
        if self._db:
            await self._db.close()
