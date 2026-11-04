"""What AIDB mode does at run time (see aidb_mode.py for the switch and the setup): incident texts and their similarity search, and questions in
natural language. Every public function here degrades to the `app` behaviour (or a clear refusal) instead of raising into the caller.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC

import psycopg
from apgkit import aidb, retrieval

from . import aidb_mode as mode
from . import aidb_objects
from .catalog import district_name
from .db import connect

log = logging.getLogger("citypulse.aidb")
AGENT_TIMEOUT = 90.0       # seconds, wall clock for one agent call
MAX_ROWS = 200             # rows of the answer's table returned to the caller
MAX_SQL = 4000
ASK_LOG_KEEP = "90 days"
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def short_hash(value) -> str:
    """First 12 hex of SHA-256: what the ask ledger and the log keep of a question (and of nothing else about it)."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:12]


# ---- incident texts: a derived text per troubled window --------------------------------------------------------------------------
def _num(x) -> float:
    return float(x) if x is not None else 0.0


def _words(verdict: str) -> str:
    return (verdict or "unknown").replace("_", " ")


CAUSES = (("offline_pct", "offline", "sensors went offline and stopped reporting"), ("stale_pct", "stale", "sensors reported late (stale)"),
          ("fault_pct", "fault", "sensors reported impossible values (fault)"), ("stuck_pct", "stuck", "sensors repeated the same value (stuck)"))


def narrative(row: dict) -> str:
    """A short text for one cp_ask.incidents row (or a row shaped like one), built from the row alone, so from derived values only. Deterministic:
    the same row gives the same text, and an incident whose outcome became known gives a longer one. It opens with the dominant failure mode in
    words, because a small embedding model separates incidents by that and gets lost in a long template."""
    shares = sorted(((_num(row.get(key)), short, phrase) for key, short, phrase in CAUSES), reverse=True)
    top = shares[0]
    head = (f"{top[0]:g}% of the {row['sensors']} {top[2]}" if top[0] > 0 else f"None of the {row['sensors']} sensors failed outright")
    parts = [f"{head.capitalize()} in district {district_name(row['district'])} ({row['district']}) of city {row['city']}: "
             f"{_words(row['verdict'])}, {_num(row['health_pct']):g}% healthy, {row['sensors_not_ok']} sensors lost."]
    others = [f"{short} {v:g}%" for v, short, _ in shares[1:] if v > 0]
    if others:
        parts.append("Also " + ", ".join(others) + ".")
    if _num(row.get("burst_active_pct")) > 0:
        parts.append(f"A burst (an injected fault) was active for {_num(row['burst_active_pct']):g}% of the window.")
    end = row.get("window_end")
    if end is not None:
        t = end.astimezone(UTC)
        parts.append(f"The window ended at {t.hour:02d}:00 UTC on a {WEEKDAYS[t.weekday()]}.")
    if row.get("verdict_1h"):
        parts.append(f"The district was {_words(row['verdict_1h'])} an hour later.")
    if row.get("duration_min") is not None:
        parts.append(f"The incident lasted {_num(row['duration_min']):g} minutes.")
    return " ".join(parts)


def district_row(d: dict) -> dict:
    """A cp_ask.districts row (counts, now) shaped like an incident row, to describe a district's current state in the same words."""
    n = max(1, d["sensors"])
    return {"city": d["city"], "district": d["district"], "verdict": d["verdict"], "health_pct": d["health_pct"], "sensors": d["sensors"],
            "sensors_not_ok": d["sensors"] - d["sensors_ok"], "offline_pct": round(100 * d["sensors_offline"] / n, 1),
            "stale_pct": round(100 * d["sensors_stale"] / n, 1), "fault_pct": round(100 * d["sensors_fault"] / n, 1),
            "stuck_pct": round(100 * d["sensors_stuck"] / n, 1)}


_UPSERT = ("INSERT INTO cp_ai.incident_text (episode_id, narrative) VALUES (%s, %s) ON CONFLICT (episode_id) DO UPDATE "
           "SET narrative = EXCLUDED.narrative, updated_at = now() WHERE cp_ai.incident_text.narrative IS DISTINCT FROM EXCLUDED.narrative")
_PENDING = """
SELECT i.* FROM cp_ask.incidents i LEFT JOIN cp_ai.incident_text t ON t.episode_id = i.episode_id
WHERE t.episode_id IS NULL
   OR (i.verdict_1h IS NOT NULL AND t.narrative NOT LIKE '%% an hour later.%%')
   OR (i.duration_min IS NOT NULL AND t.narrative NOT LIKE '%% lasted %%')
ORDER BY i.window_end DESC LIMIT %s
"""


def touch(conn, limit: int = 500) -> int:
    """Write the text of every incident that has none, and refresh those whose outcome became known, in the caller's transaction, when AIDB mode is
    on. A failure is logged and never reaches the caller (the text is derived; the pattern-memory pass it rides on must not fail for it). Returns the
    number of texts written."""
    if not mode.narratives_on():
        return 0
    try:
        with conn.transaction():
            rows = conn.execute(_PENDING, (limit,)).fetchall()
            for r in rows:
                conn.execute(_UPSERT, (r["episode_id"], narrative(r)))
            return len(rows)
    except psycopg.Error as exc:
        log.warning("incident texts not written: %s", str(exc).splitlines()[0][:160])
        return 0


def backfill(conn, limit: int = 100_000) -> tuple[int, int]:
    """Texts for every incident that has none or an outdated one (the operator setup runs this; repeating it changes nothing). Returns (incidents
    looked at, texts written). Caller commits."""
    rows = conn.execute("SELECT * FROM cp_ask.incidents ORDER BY window_end DESC LIMIT %s", (limit,)).fetchall()
    written = sum(conn.execute(_UPSERT, (r["episode_id"], narrative(r))).rowcount for r in rows)
    return len(rows), written


def backfill_narratives(conn, limit: int = 100_000) -> int:
    """`backfill`, returning only the number of incidents looked at."""
    return backfill(conn, limit)[0]


# ---- retrieval: similar past incidents ---------------------------------------------------------------------------------------
_selected: dict = {"t": 0.0, "r": None, "backend": ""}
_selected_lock = threading.Lock()


def app_retrieval() -> retrieval.AppRetrieval:
    return retrieval.AppRetrieval(aidb_objects.NARRATIVE_TABLE, key_column="episode_id", text_column="narrative")


def reset_retriever() -> None:
    with _selected_lock:
        _selected.update(t=0.0, r=None, backend="")


def retriever(conn) -> retrieval.Retrieval:
    """The backend CITYPULSE_RETRIEVAL_BACKEND names, when it is usable (AIDB there, set up, seals intact, the knowledge base exists); otherwise the
    app backend, with an ERROR in the log naming why. Chosen once per mode.TTL seconds, not per request."""
    want = mode.retrieval_backend()
    with _selected_lock:
        if _selected["r"] is not None and _selected["backend"] == want and time.monotonic() - _selected["t"] < mode.TTL:
            return _selected["r"]
    app = app_retrieval()
    chosen: retrieval.Retrieval = app
    if want == "aidb":
        ok, why = mode.usable("retrieval", conn)
        if not ok:
            log.error("RETRIEVAL BACKEND FALLBACK: %s=aidb but %s; using the app backend", mode.BACKEND_VAR, why)
            mode.calls.labels("retrieval", "fallback").inc()
        else:
            chosen = retrieval.select(mode.PREFIX, app=app, aidb=retrieval.AidbRetrieval(mode.KB), connect=connect)
    with _selected_lock:
        _selected.update(t=time.monotonic(), r=chosen, backend=want)
    return chosen


class NoSuchDistrict(LookupError):
    pass


def similar_incidents(conn, *, city: str | None = None, district: str | None = None, q: str | None = None, k: int = 5) -> dict:
    """Past incidents like a district's current state (`city` and `district`) or like a free-text description (`q`), from the backend the switch
    names. Raises NoSuchDistrict when the district has no sensors."""
    text = q
    current = None
    if not text:
        d = conn.execute("SELECT * FROM cp_ask.districts WHERE city = %s AND district = %s", (city, district)).fetchone()
        if not d:
            raise NoSuchDistrict(f"no sensors report for district {district!r} of city {city!r}")
        current = {"city": city, "district": district, "verdict": d["verdict"], "health_pct": float(d["health_pct"])}
        text = narrative(district_row(d))
    out = {"requested": mode.retrieval_backend(), "backend": "app", "query": text if q is None else None, "query_chars": len(text), "current": current,
           "items": []}
    hits, fallback = [], ""
    want = k + 6                                                  # room for the district's own recent windows, which are dropped below
    try:
        r = retriever(conn)
        out["backend"] = r.name
        hits = r.retrieve(conn, text, want)
    except Exception as exc:  # noqa: BLE001  (AIDB failing at query time must not take the route down: the app backend answers)
        conn.rollback()
        fallback = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120] if str(exc) else ''}"
        log.error("RETRIEVAL BACKEND FALLBACK: %s failed at query time (%s); using the app backend", out["backend"], fallback)
        mode.calls.labels("retrieval", "error").inc()
        out["backend"] = "app"
        hits = app_retrieval().retrieve(conn, text, want)
    ids = [int(h.key) for h in hits]
    rows = {r["episode_id"]: r for r in conn.execute(
        "SELECT episode_id, city, district, window_end, verdict, health_pct, sensors_not_ok, verdict_1h, episode_start, ended_at, duration_min "
        "FROM cp_ask.incidents WHERE episode_id = ANY(%s)", (ids,)).fetchall()} if ids else {}
    items = []
    recent = _hours_ago(conn, 3) if current else None
    for h in hits:
        r = rows.get(int(h.key))
        if r is None:
            continue
        if current and r["city"] == city and r["district"] == district and (not r["ended_at"] or r["window_end"] > recent):
            continue                                              # the same incident still going (or just over), not a precedent
        items.append({"episode_id": r["episode_id"], "city": r["city"], "district": r["district"], "window_end": r["window_end"], "verdict": r["verdict"],
                      "health_pct": float(r["health_pct"]), "sensors_not_ok": r["sensors_not_ok"], "score": round(h.score, 4),
                      "distance": None if h.distance is None else round(h.distance, 4),
                      "outcome": {"verdict_1h": r["verdict_1h"], "lasted_min": None if r["duration_min"] is None else float(r["duration_min"]),
                                  "ended_at": r["ended_at"]},
                      "narrative": h.text})
        if len(items) >= k:
            break
    out["items"] = items
    if fallback:
        out["fallback"] = fallback
    mode.calls.labels("retrieval", "ok").inc()
    return out


def _hours_ago(conn, h: int):
    return conn.execute("SELECT now() - make_interval(hours => %s) AS t", (h,)).fetchone()["t"]


# ---- questions ---------------------------------------------------------------------------------------------------------------
@dataclass
class AgentCall:
    text: str | None
    conversation_id: str | None
    error: str = ""
    ms: int = 0


def call_agent(conn, agent: str, prompt: str, conversation_id: str | None = None) -> AgentCall:
    """One `aidb.agent_converse`, bounded in time. Never raises: a failure is in `error` (first line only, no SQL, no values)."""
    t0 = time.monotonic()
    try:
        conn.commit()                                        # nothing of the caller's stays open across a call that can take a minute
        reply = aidb.converse(conn, agent, prompt, conversation_id=conversation_id, timeout=AGENT_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return AgentCall(None, None, f"{type(exc).__name__}", int((time.monotonic() - t0) * 1000))
    ms = int((time.monotonic() - t0) * 1000)
    if reply.error or reply.message is None:
        return AgentCall(None, reply.conversation_id, (reply.error or "the agent returned no message").strip().splitlines()[0][:160], ms)
    return AgentCall(reply.message, reply.conversation_id, "", ms)


_ask_slots = threading.BoundedSemaphore(2)       # each question can cost a model call and a minute of a database session


class AskRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def executed(conn, conversation_id: str | None, since) -> list[dict]:
    """What the agent actually ran in this call, from AIDB's action log (the runtime role reads its own rows there, nothing else): one dict per
    tool call in order: {"tool", "sql" (run_sql_query only), "ok", "rows" (run_sql_query only), "error"}. Empty when the log cannot be read."""
    if not conversation_id:
        return []
    try:
        rows = conn.execute("SELECT action_type, request_payload, response_payload FROM aidb_internal.action_log "
                            "WHERE conversation_id = %s::uuid AND action_type IN ('tool_call', 'tool_response') AND created_at >= %s "
                            "ORDER BY created_at, id", (conversation_id, since)).fetchall()
        conn.commit()
    except psycopg.Error as exc:
        conn.rollback()
        log.warning("the agent's action log could not be read: %s", str(exc).splitlines()[0][:120])
        return []
    calls: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        if r["action_type"] == "tool_call":
            p = r["request_payload"] or {}
            cid = p.get("call_id") or f"n{len(order)}"
            calls[cid] = {"tool": p.get("tool_name"), "sql": (p.get("arguments") or {}).get("query") if p.get("tool_name") == "run_sql_query" else None,
                          "ok": None, "rows": None, "error": ""}
            order.append(cid)
        else:
            p = r["response_payload"] or {}
            c = calls.get(p.get("call_id"))
            if c is None:
                continue
            if p.get("error"):
                c["ok"], c["error"] = False, str(p["error"]).splitlines()[0][:300]
            else:
                c["ok"] = True
                if c["tool"] == "run_sql_query":
                    try:
                        parsed = json.loads(p.get("contents") or "[]")
                        c["rows"] = parsed if isinstance(parsed, list) else [parsed]
                    except ValueError:
                        c["rows"], c["error"] = None, "the result could not be parsed"
    return [calls[c] for c in order]


def _record(conn, actor: str, question: str, outcome: str, ms: int, steps: int, rows: int, conversation_id: str | None) -> None:
    """The ask ledger: when, who, a short hash and the length of the question (never its text), how it ended. Also one log line."""
    h = short_hash(question)
    log.info("ask outcome=%s question=%s chars=%d steps=%d rows=%d ms=%d", outcome, h, len(question), steps, rows, ms)
    try:
        with conn.transaction():
            conn.execute("INSERT INTO cp_ai.ask_log (actor, question_hash, question_chars, outcome, steps, rows_returned, ms, conversation_id) "
                         "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (actor[:60], h, len(question), outcome, steps, rows, ms, conversation_id))
            conn.execute("DELETE FROM cp_ai.ask_log WHERE asked_at < now() - %s::interval", (ASK_LOG_KEEP,))
    except psycopg.Error as exc:
        log.warning("ask ledger not written: %s", str(exc).splitlines()[0][:120])


def ask(conn, question: str, actor: str, conversation_id: str | None = None) -> dict:
    """Answer a question with the AIDB agent: semantic search over the cp_ask views, then one read-only SELECT under a purpose that can read those
    views and nothing else. Returns the answer, the SQL the agent ran (read back from AIDB's action log, so it is what ran, not what the model says
    it ran) and the rows that SQL returned. Every question is ledgered (a short hash and the length, never the text)."""
    if not mode.any_requested():
        raise AskRefused(503, f"questions are switched off (set {mode.BACKEND_VAR}=aidb after `citypulse aidb-setup`)")
    ok, why = mode.usable("ask", conn)
    if not ok:
        mode.calls.labels("ask", "refused").inc()
        raise AskRefused(503, f"questions are not available: {why}")
    if not _ask_slots.acquire(blocking=False):
        raise AskRefused(429, "two questions are already being answered; try again in a moment")
    try:
        since = conn.execute("SELECT clock_timestamp() - interval '2 seconds' AS t").fetchone()["t"]
        call = call_agent(conn, mode.AGENT, question, conversation_id)
    finally:
        _ask_slots.release()
    ran = executed(conn, call.conversation_id, since) if call.text else []
    sql_runs = [c for c in ran if c["tool"] == "run_sql_query"]
    good = [c for c in sql_runs if c["ok"] and c["rows"] is not None]
    last = good[-1] if good else (sql_runs[-1] if sql_runs else None)
    rows = (last["rows"] or [])[:MAX_ROWS] if last else []
    outcome = "ok" if call.text else "error"
    mode.calls.labels("ask", outcome).inc()
    _record(conn, actor, question, outcome, call.ms, len(ran), len(rows), call.conversation_id)
    conn.commit()
    if not call.text:
        raise AskRefused(502, f"the agent could not answer: {call.error}")
    return {"answer": call.text, "sql": last["sql"][:MAX_SQL] if last and last["sql"] else None, "rows": rows,
            "row_count": len(last["rows"] or []) if last else 0, "truncated": bool(last and len(last["rows"] or []) > MAX_ROWS),
            "sql_error": last["error"] if last and not last["ok"] else "",
            "steps": [{"tool": c["tool"], "ok": c["ok"]} for c in ran], "conversation_id": call.conversation_id, "agent": mode.AGENT, "backend": "aidb"}
