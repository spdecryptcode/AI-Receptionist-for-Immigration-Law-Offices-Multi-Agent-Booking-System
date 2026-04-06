"""
Dashboard UI — username + password protected analytics view at /dashboard/
"""
from __future__ import annotations

import asyncio
import hmac
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Cookie, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.config import settings
from app.dependencies import get_redis_client, get_supabase_client

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_SESSION_TTL = 8 * 3600
_SESSION_KEY = "dash:session:"


# ── Auth helpers ──────────────────────────────────────────────────────────────

async def _create_session() -> str:
    redis = get_redis_client()
    token = secrets.token_urlsafe(32)
    await redis.setex(f"{_SESSION_KEY}{token}", _SESSION_TTL, "1")
    return token


async def _valid_session(token: str | None) -> bool:
    if not token:
        return False
    redis = get_redis_client()
    return await redis.get(f"{_SESSION_KEY}{token}") == "1"


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
async def dashboard_root():
    return RedirectResponse("/dashboard/", status_code=302)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page():
    return HTMLResponse(_LOGIN_HTML)


class _LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login", include_in_schema=False)
async def do_login(body: _LoginBody, response: Response):
    username_ok = hmac.compare_digest(
        body.username.strip().lower().encode(),
        settings.dashboard_username.strip().lower().encode(),
    )
    password_ok = hmac.compare_digest(
        body.password.encode("utf-8"),
        settings.dashboard_password.encode("utf-8"),
    )
    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = await _create_session()
    response.set_cookie(
        key="dashboard_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=_SESSION_TTL,
        secure=False,
    )
    return {"ok": True}


@router.get("/logout", include_in_schema=False)
async def logout(
    response: Response,
    dashboard_session: Optional[str] = Cookie(default=None),
):
    if dashboard_session:
        redis = get_redis_client()
        await redis.delete(f"{_SESSION_KEY}{dashboard_session}")
    response.delete_cookie("dashboard_session")
    return RedirectResponse("/dashboard/login", status_code=302)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page(
    dashboard_session: Optional[str] = Cookie(default=None),
):
    if not await _valid_session(dashboard_session):
        return RedirectResponse("/dashboard/login", status_code=302)
    return HTMLResponse(_DASHBOARD_HTML.replace("{{USERNAME}}", settings.dashboard_username))


@router.get("/api/stats")
async def api_stats(
    dashboard_session: Optional[str] = Cookie(default=None),
):
    if not await _valid_session(dashboard_session):
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = await _fetch_stats()
    return JSONResponse(data)


@router.get("/api/transcript/{call_sid}")
async def api_transcript(
    call_sid: str,
    dashboard_session: Optional[str] = Cookie(default=None),
):
    if not await _valid_session(dashboard_session):
        raise HTTPException(status_code=401, detail="Not authenticated")

    loop = asyncio.get_event_loop()

    def _fetch():
        supabase = get_supabase_client()
        r = supabase.table("conversation_messages").select(
            "turn_index,role,content,phase,latency_ms,created_at"
        ).eq("call_sid", call_sid).order("turn_index").execute()
        return r.data or []

    messages = await loop.run_in_executor(None, _fetch)
    return JSONResponse({"call_sid": call_sid, "messages": messages})


# ── DB queries ────────────────────────────────────────────────────────────────

async def _fetch_stats() -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_stats_sync)


def _fetch_stats_sync() -> dict:
    supabase = get_supabase_client()
    now = datetime.now(timezone.utc)
    cutoff_30 = (now - timedelta(days=30)).isoformat()
    cutoff_60 = (now - timedelta(days=60)).isoformat()

    # ── Summary KPIs ─────────────────────────────────────────────────────────
    r = supabase.table("conversations").select("call_sid", count="exact").gte("updated_at", cutoff_30).execute()
    total_calls = r.count or 0

    r = supabase.table("conversations").select("call_sid", count="exact").gte("updated_at", cutoff_30).not_.is_("scheduled_at", "null").execute()
    bookings = r.count or 0

    r = supabase.table("conversations").select("caller_phone").not_.is_("caller_phone", "null").execute()
    phones = {row["caller_phone"] for row in (r.data or []) if row.get("caller_phone")}
    total_clients = len(phones)

    r = supabase.table("conversations").select("duration_seconds").gte("updated_at", cutoff_30).not_.is_("duration_seconds", "null").execute()
    durations = [row["duration_seconds"] for row in (r.data or []) if row.get("duration_seconds")]
    avg_duration = int(sum(durations) / len(durations)) if durations else 0

    r = supabase.table("conversations").select("call_sid", count="exact").gte("scheduled_at", now.isoformat()).execute()
    upcoming_appts = r.count or 0

    r = supabase.table("conversations").select("call_sid", count="exact").gte("updated_at", cutoff_60).lt("updated_at", cutoff_30).execute()
    calls_prev = r.count or 0

    r = supabase.table("conversations").select("call_sid", count="exact").gte("updated_at", cutoff_60).lt("updated_at", cutoff_30).not_.is_("scheduled_at", "null").execute()
    bookings_prev = r.count or 0

    # ── Calls by day ─────────────────────────────────────────────────────────
    r = supabase.table("conversations").select("started_at,updated_at").gte("updated_at", cutoff_30).execute()
    day_counts: dict[str, int] = {}
    for row in (r.data or []):
        ts = row.get("started_at") or row.get("updated_at") or ""
        if ts:
            day = ts[:10]
            day_counts[day] = day_counts.get(day, 0) + 1
    calls_by_day = [{"day": d, "count": c} for d, c in sorted(day_counts.items())]

    # ── Outcomes ─────────────────────────────────────────────────────────────
    r = supabase.table("conversations").select("call_outcome,scheduled_at,transferred_at").execute()
    outcome_counts: dict[str, int] = {}
    for row in (r.data or []):
        outcome = row.get("call_outcome")
        if not outcome:
            if row.get("scheduled_at"):
                outcome = "booking_made"
            elif row.get("transferred_at"):
                outcome = "transferred_to_staff"
        if outcome:
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
    outcomes = [{"label": k, "count": v} for k, v in sorted(outcome_counts.items(), key=lambda x: -x[1])]

    # ── Case types ───────────────────────────────────────────────────────────
    r = supabase.table("immigration_intakes").select("case_type").not_.is_("case_type", "null").execute()
    ct_counts: dict[str, int] = {}
    for row in (r.data or []):
        ct = row.get("case_type")
        if ct:
            ct_counts[ct] = ct_counts.get(ct, 0) + 1
    case_types = [{"label": k, "count": v} for k, v in sorted(ct_counts.items(), key=lambda x: -x[1])]

    # ── Language breakdown (replaces client-status chart) ────────────────────
    r = supabase.table("conversations").select("language_detected").execute()
    lang_counts: dict[str, int] = {}
    for row in (r.data or []):
        lang = row.get("language_detected") or "en"
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
    client_statuses = [{"label": k, "count": v} for k, v in sorted(lang_counts.items(), key=lambda x: -x[1])]

    # ── Urgency levels ───────────────────────────────────────────────────────
    r = supabase.table("conversations").select("urgency_label").not_.is_("urgency_label", "null").execute()
    urg_counts: dict[str, int] = {}
    for row in (r.data or []):
        ul = row.get("urgency_label")
        if ul:
            urg_counts[ul] = urg_counts.get(ul, 0) + 1
    urgency_levels = [{"label": k, "count": v} for k, v in sorted(urg_counts.items(), key=lambda x: -x[1])]

    # ── Channel breakdown ────────────────────────────────────────────────────
    r = supabase.table("conversations").select("channel").execute()
    ch_counts: dict[str, int] = {}
    for row in (r.data or []):
        ch = row.get("channel") or "phone"
        ch_counts[ch] = ch_counts.get(ch, 0) + 1
    channels = [{"label": k, "count": v} for k, v in sorted(ch_counts.items(), key=lambda x: -x[1])]

    # ── call_sids that have transcript messages ───────────────────────────────
    tm_r = supabase.table("conversation_messages").select("call_sid").execute()
    transcript_sids: set[str] = {row["call_sid"] for row in (tm_r.data or []) if row.get("call_sid")}

    # ── Recent calls ─────────────────────────────────────────────────────────
    r = supabase.table("conversations").select(
        "call_sid,caller_phone,caller_name,started_at,updated_at,duration_seconds,call_outcome,channel,scheduled_at,transferred_at"
    ).order("started_at", desc=True).limit(200).execute()
    recent_calls = []
    seen_sids: set[str] = set()
    for row in (r.data or []):
        sid = row.get("call_sid") or ""
        seen_sids.add(sid)
        outcome = row.get("call_outcome")
        if not outcome:
            if row.get("scheduled_at"):
                outcome = "booking_made"
            elif row.get("transferred_at"):
                outcome = "transferred_to_staff"
        recent_calls.append({
            "sid": sid,
            "name": row.get("caller_name") or "Unknown",
            "phone": row.get("caller_phone") or "",
            "started_at": row.get("started_at") or row.get("updated_at") or "",
            "duration": int(row.get("duration_seconds") or 0),
            "outcome": outcome or "\u2014",
            "channel": row.get("channel") or "phone",
            "has_transcript": sid in transcript_sids,
        })

    # Append any transcript-bearing calls not already in the top-20 list
    extra_sids = [s for s in transcript_sids if s not in seen_sids]
    if extra_sids:
        ex_r = supabase.table("conversations").select(
            "call_sid,caller_phone,caller_name,started_at,updated_at,duration_seconds,call_outcome,channel,scheduled_at,transferred_at"
        ).in_("call_sid", list(extra_sids)).execute()
        for row in (ex_r.data or []):
            sid = row.get("call_sid") or ""
            outcome = row.get("call_outcome")
            if not outcome:
                if row.get("scheduled_at"):
                    outcome = "booking_made"
                elif row.get("transferred_at"):
                    outcome = "transferred_to_staff"
            recent_calls.append({
                "sid": sid,
                "name": row.get("caller_name") or "Unknown",
                "phone": row.get("caller_phone") or "",
                "started_at": row.get("started_at") or row.get("updated_at") or "",
                "duration": int(row.get("duration_seconds") or 0),
                "outcome": outcome or "\u2014",
                "channel": row.get("channel") or "phone",
                "has_transcript": True,
            })

    # Sort all calls newest-first by started_at, then trim to 200
    recent_calls.sort(key=lambda c: c.get("started_at") or "", reverse=True)
    recent_calls = recent_calls[:200]

    # ── Intake records ───────────────────────────────────────────────────────
    r = supabase.table("immigration_intakes").select(
        "call_sid,full_name,caller_phone,case_type,urgency_reason,current_immigration_status,created_at"
    ).order("created_at", desc=True).limit(200).execute()
    intake_sids = [row["call_sid"] for row in (r.data or []) if row.get("call_sid")]
    urgency_map: dict[str, str] = {}
    if intake_sids:
        conv_r = supabase.table("conversations").select("call_sid,urgency_label").in_("call_sid", intake_sids).execute()
        for conv in (conv_r.data or []):
            if conv.get("urgency_label"):
                urgency_map[conv["call_sid"]] = conv["urgency_label"]
    intake_records = []
    for row in (r.data or []):
        sid = row.get("call_sid") or ""
        intake_records.append({
            "name": row.get("full_name") or "Unknown",
            "phone": row.get("caller_phone") or "",
            "case_type": row.get("case_type") or "",
            "urgency": urgency_map.get(sid, ""),
            "urgency_reason": row.get("urgency_reason") or "",
            "court_date": "",
            "status": row.get("current_immigration_status") or "",
            "detained": False,
            "completeness": 0,
            "started_at": row.get("created_at") or "",
        })

    # ── Conversation intelligence (phase / intent / latency) ─────────────────
    all_msgs_r = supabase.table("conversation_messages").select(
        "call_sid,role,phase,intent,latency_ms"
    ).execute()
    phase_counts: dict[str, int] = {}
    intent_counts: dict[str, int] = {}
    latency_by_phase: dict[str, list[int]] = {}
    turns_per_call: dict[str, int] = {}
    for row in (all_msgs_r.data or []):
        phase = row.get("phase") or "unknown"
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        intent = row.get("intent")
        if intent and row.get("role") != "system":
            intent_counts[intent] = intent_counts.get(intent, 0) + 1
        if row.get("role") == "assistant" and (row.get("latency_ms") or 0) > 0:
            latency_by_phase.setdefault(phase, []).append(row["latency_ms"])
        sid = row.get("call_sid") or ""
        if sid:
            turns_per_call[sid] = turns_per_call.get(sid, 0) + 1
    phase_dist = [{"label": k, "count": v} for k, v in sorted(phase_counts.items(), key=lambda x: -x[1])]
    intent_dist = [{"label": k, "count": v} for k, v in sorted(intent_counts.items(), key=lambda x: -x[1])[:12]]
    latency_dist = [{"label": k, "avg": int(sum(v) / len(v))} for k, v in sorted(latency_by_phase.items(), key=lambda x: -(sum(x[1]) / len(x[1]))) if v]

    # ── Lead scoring / retention analytics ───────────────────────────────────
    all_convs_r = supabase.table("conversations").select(
        "call_sid,lead_score,urgency_label,call_outcome,caller_phone,scheduled_at,transferred_at"
    ).execute()
    lead_buckets: dict[str, int] = {"0-20": 0, "21-40": 0, "41-60": 0, "61-80": 0, "81-100": 0}
    turns_by_outcome: dict[str, list[int]] = {}
    urg_outcome: dict[str, dict[str, int]] = {}
    phone_counts_all: dict[str, int] = {}
    conv_lead_map: dict[str, int] = {}
    for row in (all_convs_r.data or []):
        score = row.get("lead_score") or 0
        if score <= 20: lead_buckets["0-20"] += 1
        elif score <= 40: lead_buckets["21-40"] += 1
        elif score <= 60: lead_buckets["41-60"] += 1
        elif score <= 80: lead_buckets["61-80"] += 1
        else: lead_buckets["81-100"] += 1
        oc = row.get("call_outcome") or ""
        if not oc:
            if row.get("scheduled_at"): oc = "booking_made"
            elif row.get("transferred_at"): oc = "transferred_to_staff"
        sid = row.get("call_sid") or ""
        t = turns_per_call.get(sid, 0)
        if t > 0 and oc:
            turns_by_outcome.setdefault(oc, []).append(t)
        ul = row.get("urgency_label") or "unknown"
        if oc:
            urg_outcome.setdefault(ul, {})
            urg_outcome[ul][oc] = urg_outcome[ul].get(oc, 0) + 1
        phone = row.get("caller_phone")
        if phone:
            phone_counts_all[phone] = phone_counts_all.get(phone, 0) + 1
        if sid and row.get("lead_score") is not None:
            conv_lead_map[sid] = row["lead_score"]
    lead_score_buckets = [{"label": k, "count": v} for k, v in lead_buckets.items()]
    avg_turns_by_outcome = [
        {"label": k, "avg": round(sum(v) / len(v), 1)}
        for k, v in sorted(turns_by_outcome.items(), key=lambda x: -(sum(x[1]) / len(x[1]))) if v
    ]
    repeat_callers = sum(1 for v in phone_counts_all.values() if v > 1)
    total_unique_callers = len(phone_counts_all)
    urgency_vs_outcome = [
        {"urgency": ul, "outcome": oc, "count": cnt}
        for ul, outcomes in urg_outcome.items()
        for oc, cnt in outcomes.items()
    ]

    # ── Demographics & risk signals ───────────────────────────────────────────
    all_intakes_r = supabase.table("immigration_intakes").select(
        "call_sid,country_of_birth,case_type,has_attorney,prior_deportation,criminal_history,preferred_language"
    ).execute()
    country_counts: dict[str, int] = {}
    attorney_by_case: dict[str, dict[str, int]] = {}
    lang_pref_counts: dict[str, int] = {}
    lead_by_case: dict[str, list[int]] = {}
    risk_total = 0; risk_deported = 0; risk_criminal = 0
    for row in (all_intakes_r.data or []):
        country = row.get("country_of_birth") or "Unknown"
        country_counts[country] = country_counts.get(country, 0) + 1
        ct = row.get("case_type") or "other"
        has_atty = bool(row.get("has_attorney"))
        attorney_by_case.setdefault(ct, {"yes": 0, "no": 0})
        attorney_by_case[ct]["yes" if has_atty else "no"] += 1
        lang = row.get("preferred_language") or "en"
        lang_pref_counts[lang] = lang_pref_counts.get(lang, 0) + 1
        risk_total += 1
        if row.get("prior_deportation"): risk_deported += 1
        if row.get("criminal_history"): risk_criminal += 1
        sid = row.get("call_sid") or ""
        if sid and sid in conv_lead_map:
            lead_by_case.setdefault(ct, []).append(conv_lead_map[sid])
    country_dist = [{"label": k, "count": v} for k, v in sorted(country_counts.items(), key=lambda x: -x[1])[:12]]
    attorney_rate = [
        {"label": k, "with_atty": v["yes"], "without_atty": v["no"]}
        for k, v in sorted(attorney_by_case.items(), key=lambda x: -(x[1]["yes"] + x[1]["no"]))
    ]
    avg_lead_by_case = [
        {"label": k, "avg": round(sum(v) / len(v))}
        for k, v in sorted(lead_by_case.items(), key=lambda x: -(sum(x[1]) / len(x[1]))) if v
    ]
    risk_flags = {
        "total": risk_total,
        "prior_deportation": risk_deported,
        "criminal_history": risk_criminal,
        "deportation_pct": round(risk_deported / risk_total * 100) if risk_total else 0,
        "criminal_pct": round(risk_criminal / risk_total * 100) if risk_total else 0,
    }

    return {
        "summary": {
            "total_calls_30d": total_calls,
            "bookings_30d": bookings,
            "active_clients": total_clients,
            "total_clients": total_clients,
            "avg_duration_sec": avg_duration,
            "upcoming_appointments": upcoming_appts,
            "calls_prev_30d": calls_prev,
            "bookings_prev_30d": bookings_prev,
        },
        "calls_by_day": calls_by_day,
        "outcomes": outcomes,
        "case_types": case_types,
        "client_statuses": client_statuses,
        "urgency_levels": urgency_levels,
        "channels": channels,
        "recent_calls": recent_calls,
        "intake_records": intake_records,
        # ── new analytics ──────────────────────────────────────────────────
        "phase_distribution": phase_dist,
        "intent_distribution": intent_dist,
        "latency_by_phase": latency_dist,
        "avg_turns_by_outcome": avg_turns_by_outcome,
        "lead_score_buckets": lead_score_buckets,
        "avg_lead_by_case": avg_lead_by_case,
        "urgency_vs_outcome": urgency_vs_outcome,
        "country_distribution": country_dist,
        "attorney_rate": attorney_rate,
        "risk_flags": risk_flags,
        "repeat_callers": repeat_callers,
        "total_unique_callers": total_unique_callers,
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Aria \u2014 Sign In</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%236366f1'/%3E%3Cpath d='M5 28L16 4L27 28' stroke='white' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' fill='none'/%3E%3Crect x='11.5' y='17' width='2' height='6' rx='1' fill='white'/%3E%3Crect x='15' y='14.5' width='2' height='8.5' rx='1' fill='white'/%3E%3Crect x='18.5' y='17' width='2' height='6' rx='1' fill='white'/%3E%3C/svg%3E">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', -apple-system, sans-serif;
      background: #0f172a;
      min-height: 100vh;
      display: flex; align-items: center; justify-content: center;
      position: relative; overflow: hidden;
    }
    body::before {
      content: ''; position: absolute;
      width: 600px; height: 600px; border-radius: 50%;
      background: radial-gradient(circle, rgba(99,102,241,0.15) 0%, transparent 70%);
      top: -200px; left: -200px;
    }
    body::after {
      content: ''; position: absolute;
      width: 500px; height: 500px; border-radius: 50%;
      background: radial-gradient(circle, rgba(139,92,246,0.1) 0%, transparent 70%);
      bottom: -150px; right: -150px;
    }
    .card {
      background: #1e293b; border: 1px solid #334155; border-radius: 16px;
      padding: 40px; width: 100%; max-width: 400px; margin: 20px;
      position: relative; z-index: 1; box-shadow: 0 25px 50px rgba(0,0,0,0.5);
    }
    .logo-wrap { display: flex; align-items: center; justify-content: center; gap: 12px; margin-bottom: 32px; }
    .logo-icon {
      width: 44px; height: 44px;
      background: linear-gradient(135deg, #6366f1, #8b5cf6);
      border-radius: 12px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
    }
    .logo-icon svg { width: 22px; height: 22px; fill: white; }
    .logo-text h1 { font-size: 17px; font-weight: 700; color: #f1f5f9; }
    .logo-text p { font-size: 12px; color: #64748b; margin-top: 1px; }
    .divider { height: 1px; background: #334155; margin-bottom: 28px; }
    .signin-title { font-size: 15px; font-weight: 600; color: #e2e8f0; margin-bottom: 6px; }
    .signin-sub { font-size: 13px; color: #64748b; margin-bottom: 24px; }
    label { display: block; font-size: 12px; font-weight: 500; color: #94a3b8; margin-bottom: 6px; letter-spacing: 0.3px; }
    .input-wrap { position: relative; margin-bottom: 16px; }
    .input-wrap .icon { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: #475569; pointer-events: none; }
    input[type=text], input[type=password] {
      width: 100%; background: #0f172a; border: 1px solid #334155; border-radius: 8px;
      padding: 11px 12px 11px 38px; font-size: 14px; font-family: inherit;
      color: #e2e8f0; outline: none; transition: border-color 0.15s, box-shadow 0.15s;
    }
    input:focus { border-color: #6366f1; box-shadow: 0 0 0 3px rgba(99,102,241,0.15); }
    input::placeholder { color: #334155; }
    .error-box {
      display: none; background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3);
      border-radius: 8px; padding: 10px 12px; font-size: 13px; color: #f87171;
      margin-bottom: 16px; align-items: center; gap: 8px;
    }
    .error-box.show { display: flex; }
    .btn {
      width: 100%; background: linear-gradient(135deg, #6366f1, #8b5cf6);
      border: none; border-radius: 8px; padding: 12px;
      font-size: 14px; font-weight: 600; font-family: inherit;
      color: white; cursor: pointer; transition: opacity 0.15s, transform 0.1s; margin-top: 4px;
    }
    .btn:hover { opacity: 0.9; }
    .btn:active { transform: scale(0.99); }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .footer { margin-top: 28px; text-align: center; font-size: 12px; color: #475569; }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo-wrap">
      <div class="logo-icon">
        <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M4 20.5L12 3.5L20 20.5" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          <rect x="8.9" y="13.5" width="1.4" height="3.5" rx="0.7" fill="white"/>
          <rect x="11.3" y="11.5" width="1.4" height="5.5" rx="0.7" fill="white"/>
          <rect x="13.7" y="13.5" width="1.4" height="3.5" rx="0.7" fill="white"/>
        </svg>
      </div>
      <div class="logo-text">
        <h1>Aria</h1>
        <p>AI Intake Agent</p>
      </div>
    </div>
    <div class="divider"></div>
    <p class="signin-title">Welcome back</p>
    <p class="signin-sub">Sign in to your dashboard account</p>

    <div class="error-box" id="errorBox">
      <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
      </svg>
      <span id="errorMsg">Invalid username or password.</span>
    </div>

    <form id="loginForm" autocomplete="on">
      <div>
        <label for="username">Username</label>
        <div class="input-wrap">
          <span class="icon">
            <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/>
            </svg>
          </span>
          <input type="text" id="username" name="username" placeholder="Enter username" autocomplete="username" autofocus>
        </div>
      </div>
      <div>
        <label for="password">Password</label>
        <div class="input-wrap">
          <span class="icon">
            <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/>
            </svg>
          </span>
          <input type="password" id="password" name="password" placeholder="Enter password" autocomplete="current-password">
        </div>
      </div>
      <button type="submit" class="btn" id="submitBtn">Sign In</button>
    </form>
    <div class="footer">Authorized access only &nbsp;&middot;&nbsp; Session expires in 8 hours</div>
  </div>

  <script>
    document.getElementById('loginForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const username = document.getElementById('username').value.trim();
      const password = document.getElementById('password').value;
      const btn = document.getElementById('submitBtn');
      const errBox = document.getElementById('errorBox');
      errBox.classList.remove('show');
      if (!username || !password) {
        document.getElementById('errorMsg').textContent = 'Please enter both username and password.';
        errBox.classList.add('show'); return;
      }
      btn.textContent = 'Signing in\u2026'; btn.disabled = true;
      try {
        const res = await fetch('/dashboard/login', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({username, password}), credentials: 'same-origin',
        });
        if (res.ok) { window.location.href = '/dashboard/'; return; }
        document.getElementById('errorMsg').textContent = 'Invalid username or password. Please try again.';
        errBox.classList.add('show');
        document.getElementById('password').value = '';
        document.getElementById('password').focus();
      } catch {
        document.getElementById('errorMsg').textContent = 'Connection error. Please try again.';
        errBox.classList.add('show');
      } finally { btn.textContent = 'Sign In'; btn.disabled = false; }
    });
  </script>
</body>
</html>"""


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Aria \u2014 Dashboard</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%236366f1'/%3E%3Cpath d='M5 28L16 4L27 28' stroke='white' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' fill='none'/%3E%3Crect x='11.5' y='17' width='2' height='6' rx='1' fill='white'/%3E%3Crect x='15' y='14.5' width='2' height='8.5' rx='1' fill='white'/%3E%3Crect x='18.5' y='17' width='2' height='6' rx='1' fill='white'/%3E%3C/svg%3E">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #f8fafc; --surface: #ffffff; --border: #e2e8f0;
      --text: #0f172a; --text-2: #475569; --text-3: #94a3b8;
      --accent: #6366f1; --sidebar: #0f172a; --radius: 12px;
    }
    html, body { height: 100%; font-family: 'Inter', -apple-system, sans-serif; background: var(--bg); color: var(--text); font-size: 14px; }
    .app { display: flex; height: 100vh; overflow: hidden; }
    /* Sidebar */
    .sidebar { width: 240px; flex-shrink: 0; background: var(--sidebar); display: flex; flex-direction: column; border-right: 1px solid #1e293b; }
    .sidebar-header { padding: 20px 20px 16px; border-bottom: 1px solid #1e293b; }
    .sidebar-logo { display: flex; align-items: center; gap: 10px; }
    .sidebar-logo-icon { width: 36px; height: 36px; background: linear-gradient(135deg,#6366f1,#8b5cf6); border-radius: 9px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    .sidebar-logo-icon svg { width: 18px; height: 18px; fill: white; }
    .sidebar-logo-text span:first-child { display: block; font-size: 13px; font-weight: 700; color: #f1f5f9; }
    .sidebar-logo-text span:last-child { font-size: 11px; color: #475569; }
    .sidebar-nav { flex: 1; padding: 12px 10px; overflow-y: auto; }
    .nav-section { margin-bottom: 20px; }
    .nav-label { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.8px; color: #334155; padding: 0 10px 8px; }
    .nav-item { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: 8px; cursor: pointer; color: #94a3b8; font-size: 13px; font-weight: 500; transition: background 0.12s, color 0.12s; margin-bottom: 2px; }
    .nav-item:hover { background: #1e293b; color: #e2e8f0; }
    .nav-item.active { background: rgba(99,102,241,0.15); color: #818cf8; }
    .nav-item svg { width: 16px; height: 16px; flex-shrink: 0; }
    .sidebar-footer { padding: 12px 10px; border-top: 1px solid #1e293b; }
    .user-card { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: 8px; }
    .user-avatar { width: 30px; height: 30px; border-radius: 50%; background: linear-gradient(135deg,#6366f1,#8b5cf6); display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 700; color: white; flex-shrink: 0; }
    .user-info span:first-child { display: block; font-size: 12px; font-weight: 600; color: #e2e8f0; }
    .user-info span:last-child { font-size: 11px; color: #475569; }
    .logout-btn { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: 8px; color: #475569; font-size: 13px; font-weight: 500; cursor: pointer; text-decoration: none; transition: background 0.12s, color 0.12s; margin-top: 2px; }
    .logout-btn:hover { background: rgba(239,68,68,0.1); color: #f87171; }
    .logout-btn svg { width: 16px; height: 16px; }
    /* Main */
    .main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
    .topbar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 0 24px; height: 60px; display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
    .page-title { font-size: 16px; font-weight: 700; color: var(--text); }
    .page-sub { font-size: 12px; color: var(--text-3); }
    .topbar-right { display: flex; align-items: center; gap: 12px; }
    .badge-live { display: flex; align-items: center; gap: 6px; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 20px; padding: 4px 10px; font-size: 11px; font-weight: 600; color: #16a34a; }
    .badge-live .dot { width: 7px; height: 7px; border-radius: 50%; background: #22c55e; animation: pulse 2s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
    .refresh-btn { display: flex; align-items: center; gap: 6px; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 7px 12px; font-size: 12px; font-weight: 500; color: var(--text-2); cursor: pointer; font-family: inherit; transition: border-color 0.12s, color 0.12s; }
    .refresh-btn:hover { border-color: var(--accent); color: var(--accent); }
    .refresh-btn svg { width: 13px; height: 13px; }
    .last-updated { font-size: 11px; color: var(--text-3); }
    /* Content */
    .content { flex: 1; overflow: hidden; display: flex; flex-direction: column; }
    #analyticsView { flex: 1; overflow-y: auto; padding: 24px; }
    .kpi-grid { display: grid; grid-template-columns: repeat(3,1fr); gap: 16px; margin-bottom: 24px; }
    @media (max-width: 1200px) { .kpi-grid { grid-template-columns: repeat(2,1fr); } }
    .kpi-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; }
    .kpi-top { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 12px; }
    .kpi-icon { width: 40px; height: 40px; border-radius: 10px; display: flex; align-items: center; justify-content: center; }
    .kpi-icon svg { width: 18px; height: 18px; }
    .kpi-trend { display: flex; align-items: center; gap: 3px; font-size: 11px; font-weight: 600; padding: 3px 7px; border-radius: 20px; }
    .kpi-trend.up { color: #16a34a; background: #f0fdf4; }
    .kpi-trend.down { color: #dc2626; background: #fef2f2; }
    .kpi-trend.neutral { color: #64748b; background: #f1f5f9; }
    .kpi-value { font-size: 28px; font-weight: 700; color: var(--text); line-height: 1; margin-bottom: 4px; }
    .kpi-label { font-size: 12px; font-weight: 500; color: var(--text-3); }
    .chart-grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
    .chart-grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-bottom: 16px; }
    @media (max-width: 1100px) { .chart-grid-2,.chart-grid-3 { grid-template-columns: 1fr; } }
    .chart-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; }
    .chart-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
    .chart-title { font-size: 13px; font-weight: 600; color: var(--text); }
    .chart-sub { font-size: 11px; color: var(--text-3); margin-top: 1px; }
    .empty-state { text-align: center; padding: 40px 20px; color: var(--text-3); font-size: 13px; }
    .table-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; margin-bottom: 16px; }
    .table-header-bar { display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-bottom: 1px solid var(--border); }
    table { width: 100%; border-collapse: collapse; }
    thead tr { background: #f8fafc; }
    th { text-align: left; padding: 11px 16px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-3); border-bottom: 1px solid var(--border); white-space: nowrap; }
    td { padding: 13px 16px; font-size: 13px; color: var(--text-2); border-bottom: 1px solid #f1f5f9; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #fafbff; }
    .td-main { font-weight: 500; color: var(--text); }
    .badge { display: inline-flex; align-items: center; padding: 3px 9px; border-radius: 20px; font-size: 11px; font-weight: 600; white-space: nowrap; }
    .mono { font-family: 'SF Mono','Fira Code',monospace; font-size: 12px; }
    .loading-overlay { position: fixed; inset: 0; background: var(--bg); display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px; z-index: 100; }
    .spinner { width: 36px; height: 36px; border-radius: 50%; border: 3px solid var(--border); border-top-color: var(--accent); animation: spin 0.8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .loading-text { font-size: 13px; color: var(--text-3); font-weight: 500; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }
    .typing-dots{display:flex;align-items:center;gap:5px;padding:10px 14px;
      background:var(--surface);border:1px solid var(--border);
      border-radius:4px 16px 16px 16px;width:fit-content}
    .typing-dots span{width:7px;height:7px;border-radius:50%;background:#a5b4fc;
      animation:tdBounce 1.3s ease infinite}
    .typing-dots span:nth-child(2){animation-delay:.18s}
    .typing-dots span:nth-child(3){animation-delay:.36s}
    @keyframes tdBounce{0%,60%,100%{transform:translateY(0);background:#a5b4fc}
      30%{transform:translateY(-6px);background:#6366f1}}
  </style>
</head>
<body>

<div id="loadingOverlay" class="loading-overlay">
  <div class="spinner"></div>
  <p class="loading-text">Loading dashboard\u2026</p>
</div>

<div class="app" id="appShell" style="display:none">
  <aside class="sidebar">
    <div class="sidebar-header">
      <div class="sidebar-logo">
        <div class="sidebar-logo-icon">
          <svg viewBox="0 0 24 24" fill="none"><path d="M4 20.5L12 3.5L20 20.5" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><rect x="8.9" y="13.5" width="1.4" height="3.5" rx="0.7" fill="white"/><rect x="11.3" y="11.5" width="1.4" height="5.5" rx="0.7" fill="white"/><rect x="13.7" y="13.5" width="1.4" height="3.5" rx="0.7" fill="white"/></svg>
        </div>
        <div class="sidebar-logo-text">
          <span>Aria</span>
          <span>AI Intake Agent</span>
        </div>
      </div>
    </div>
    <nav class="sidebar-nav">
      <div class="nav-section">
        <div class="nav-label">Overview</div>
        <div class="nav-item active" id="navDashboard" onclick="showView('analytics')">
          <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
          Dashboard
        </div>
        <div class="nav-item" id="navChat" onclick="showView('chat')">
          <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>
          AI Chat
        </div>
      </div>
      <div class="nav-section">
        <div class="nav-label">Analytics</div>
        <div class="nav-item" onclick="document.getElementById('callsSection').scrollIntoView({behavior:'smooth'})">
          <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
          Call Volume
        </div>
        <div class="nav-item" onclick="document.getElementById('chartsSection').scrollIntoView({behavior:'smooth'})">
          <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M18 20V10M12 20V4M6 20v-6"/></svg>
          Case Analytics
        </div>
        <div class="nav-item" onclick="document.getElementById('tableSection').scrollIntoView({behavior:'smooth'})">
          <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z"/></svg>
          Recent Calls
        </div>
        <div class="nav-item" onclick="document.getElementById('intakeSection').scrollIntoView({behavior:'smooth'})">
          <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
          Intake Records
        </div>
        <div class="nav-item" onclick="document.getElementById('convIntelSection').scrollIntoView({behavior:'smooth'})">
          <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 3"/></svg>
          Conv. Intelligence
        </div>
        <div class="nav-item" onclick="document.getElementById('leadSection').scrollIntoView({behavior:'smooth'})">
          <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>
          Lead Scoring
        </div>
        <div class="nav-item" onclick="document.getElementById('demographicsSection').scrollIntoView({behavior:'smooth'})">
          <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/></svg>
          Demographics
        </div>
      </div>
    </nav>
    <div class="sidebar-footer">
      <div class="user-card">
        <div class="user-avatar" id="userAvatar">A</div>
        <div class="user-info">
          <span id="userNameDisplay">{{USERNAME}}</span>
          <span>Administrator</span>
        </div>
      </div>
      <a class="logout-btn" href="/dashboard/logout">
        <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/></svg>
        Sign out
      </a>
    </div>
  </aside>

  <div class="main">
    <header class="topbar">
      <div>
        <div class="page-title" id="pageTitle">Analytics Overview</div>
        <div class="page-sub" id="dateRange">Last 30 days</div>
      </div>
      <div class="topbar-right" id="topbarRight">
        <div class="badge-live"><span class="dot"></span>Live</div>
        <span class="last-updated" id="lastUpdated"></span>
        <button class="refresh-btn" onclick="loadData()">
          <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg>
          Refresh
        </button>
      </div>
    </header>

    <div class="content">
      <div id="analyticsView">
      <div class="kpi-grid" id="kpiGrid"></div>

      <div class="chart-card" id="callsSection" style="margin-bottom:16px">
        <div class="chart-header">
          <div><div class="chart-title">Call Volume</div><div class="chart-sub">Daily calls over the last 30 days</div></div>
        </div>
        <canvas id="callsChart" height="65"></canvas>
        <div class="empty-state" id="callsEmpty" style="display:none">No call data yet</div>
      </div>

      <div class="chart-grid-2" id="chartsSection">
        <div class="chart-card">
          <div class="chart-header"><div><div class="chart-title">Call Outcomes</div><div class="chart-sub">All time distribution</div></div></div>
          <div style="display:flex;justify-content:center;align-items:center;min-height:220px">
            <canvas id="outcomesChart" style="max-width:260px;max-height:260px"></canvas>
            <div class="empty-state" id="outcomesEmpty" style="display:none">No data yet</div>
          </div>
        </div>
        <div class="chart-card">
          <div class="chart-header"><div><div class="chart-title">Case Types</div><div class="chart-sub">Intake classification</div></div></div>
          <canvas id="caseTypesChart"></canvas>
          <div class="empty-state" id="caseTypesEmpty" style="display:none">No intake data yet</div>
        </div>
      </div>

      <div class="chart-grid-3">
        <div class="chart-card">
          <div class="chart-header"><div><div class="chart-title">Client Pipeline</div><div class="chart-sub">Status distribution</div></div></div>
          <canvas id="statusChart"></canvas>
          <div class="empty-state" id="statusEmpty" style="display:none">No clients yet</div>
        </div>
        <div class="chart-card">
          <div class="chart-header"><div><div class="chart-title">Urgency Levels</div><div class="chart-sub">Triage classification</div></div></div>
          <div style="display:flex;justify-content:center;align-items:center;min-height:180px">
            <canvas id="urgencyChart" style="max-width:200px;max-height:200px"></canvas>
            <div class="empty-state" id="urgencyEmpty" style="display:none">No urgency data</div>
          </div>
        </div>
        <div class="chart-card">
          <div class="chart-header"><div><div class="chart-title">Channels</div><div class="chart-sub">Contact source mix</div></div></div>
          <div style="display:flex;justify-content:center;align-items:center;min-height:180px">
            <canvas id="channelsChart" style="max-width:200px;max-height:200px"></canvas>
            <div class="empty-state" id="channelsEmpty" style="display:none">No channel data</div>
          </div>
        </div>
      </div>

      <div class="table-card" id="tableSection">
        <div class="table-header-bar">
          <div>
            <div class="chart-title">Recent Calls</div>
            <div class="chart-sub" style="margin-top:2px">Click any column header to sort &nbsp;&middot;&nbsp; 20 per page</div>
          </div>
          <div style="position:relative;flex-shrink:0">
            <span style="position:absolute;left:10px;top:50%;transform:translateY(-50%);color:#64748b;pointer-events:none">
              <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            </span>
            <input id="callsSearch" type="text" placeholder="Search calls…" oninput="_callsSearchInput()" style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:7px 10px 7px 32px;font-size:13px;font-family:inherit;color:var(--text);outline:none;width:210px;transition:border-color 0.15s" onfocus="this.style.borderColor='#6366f1'" onblur="this.style.borderColor='var(--border)'">
          </div>
        </div>
        <div style="overflow-x:auto">
          <table id="callsTable">
            <thead><tr>
              <th data-col="name" onclick="sortCalls('name')" style="cursor:pointer;user-select:none">Caller <span class="sh"></span></th>
              <th data-col="phone" onclick="sortCalls('phone')" style="cursor:pointer;user-select:none">Phone <span class="sh"></span></th>
              <th data-col="channel" onclick="sortCalls('channel')" style="cursor:pointer;user-select:none">Channel <span class="sh"></span></th>
              <th data-col="outcome" onclick="sortCalls('outcome')" style="cursor:pointer;user-select:none">Outcome <span class="sh"></span></th>
              <th data-col="duration" onclick="sortCalls('duration')" style="cursor:pointer;user-select:none">Duration <span class="sh"></span></th>
              <th data-col="started_at" onclick="sortCalls('started_at')" style="cursor:pointer;user-select:none;color:#6366f1">Time <span class="sh" style="color:#6366f1">&darr;</span></th>
              <th>Transcript</th>
            </tr></thead>
            <tbody id="callsTableBody">
              <tr><td colspan="7" class="empty-state">Loading\u2026</td></tr>
            </tbody>
          </table>
        </div>
        <div id="callsPager"></div>
      </div>

      <div class="table-card" id="intakeSection" style="margin-bottom:16px">
        <div class="table-header-bar">
          <div>
            <div class="chart-title">Immigration Intake Records</div>
            <div class="chart-sub" style="margin-top:2px">Click any column header to sort &nbsp;&middot;&nbsp; 20 per page</div>
          </div>
          <div style="position:relative;flex-shrink:0">
            <span style="position:absolute;left:10px;top:50%;transform:translateY(-50%);color:#64748b;pointer-events:none">
              <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            </span>
            <input id="intakeSearch" type="text" placeholder="Search intakes…" oninput="_intakeSearchInput()" style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:7px 10px 7px 32px;font-size:13px;font-family:inherit;color:var(--text);outline:none;width:210px;transition:border-color 0.15s" onfocus="this.style.borderColor='#6366f1'" onblur="this.style.borderColor='var(--border)'">
          </div>
        </div>
        <div style="overflow-x:auto">
          <table id="intakeTable">
            <thead><tr>
              <th data-col="name" onclick="sortIntake('name')" style="cursor:pointer;user-select:none">Caller <span class="sh"></span></th>
              <th data-col="phone" onclick="sortIntake('phone')" style="cursor:pointer;user-select:none">Phone <span class="sh"></span></th>
              <th data-col="case_type" onclick="sortIntake('case_type')" style="cursor:pointer;user-select:none">Case Type <span class="sh"></span></th>
              <th data-col="urgency" onclick="sortIntake('urgency')" style="cursor:pointer;user-select:none">Urgency <span class="sh"></span></th>
              <th>Court Date</th>
              <th data-col="status" onclick="sortIntake('status')" style="cursor:pointer;user-select:none">Imm. Status <span class="sh"></span></th>
              <th>Detained</th><th>Complete</th>
              <th data-col="started_at" onclick="sortIntake('started_at')" style="cursor:pointer;user-select:none;color:#6366f1">Time <span class="sh" style="color:#6366f1">&darr;</span></th>
            </tr></thead>
            <tbody id="intakeTableBody">
              <tr><td colspan="9" class="empty-state">Loading\u2026</td></tr>
            </tbody>
          </table>
        </div>
        <div id="intakePager"></div>
      </div>

      <!-- ── Conversation Intelligence ─────────────────────────────────── -->
      <div id="convIntelSection">
        <div style="font-size:15px;font-weight:700;color:var(--text);margin:28px 0 16px;display:flex;align-items:center;gap:8px">
          <div style="width:4px;height:20px;background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:2px"></div>
          Conversation Intelligence
          <span style="font-size:11px;font-weight:500;color:#94a3b8;background:#f1f5f9;padding:3px 8px;border-radius:20px;margin-left:4px">Training Data</span>
        </div>
        <div class="chart-grid-2">
          <div class="chart-card">
            <div class="chart-header"><div><div class="chart-title">Phase Distribution</div><div class="chart-sub">Turn count per conversation phase</div></div></div>
            <canvas id="phaseDist"></canvas>
            <div class="empty-state" id="phaseEmpty" style="display:none">No data</div>
          </div>
          <div class="chart-card">
            <div class="chart-header"><div><div class="chart-title">Top Caller Intents</div><div class="chart-sub">Most frequent classified intents</div></div></div>
            <canvas id="intentDist"></canvas>
            <div class="empty-state" id="intentEmpty" style="display:none">No data</div>
          </div>
        </div>
        <div class="chart-grid-2" style="margin-bottom:0">
          <div class="chart-card">
            <div class="chart-header"><div><div class="chart-title">AI Response Latency by Phase</div><div class="chart-sub">Average milliseconds per conversation phase</div></div></div>
            <canvas id="latencyPhase"></canvas>
            <div class="empty-state" id="latencyEmpty" style="display:none">No data</div>
          </div>
          <div class="chart-card">
            <div class="chart-header"><div><div class="chart-title">Avg Turns to Resolution</div><div class="chart-sub">Message exchanges grouped by call outcome</div></div></div>
            <canvas id="turnsOutcome"></canvas>
            <div class="empty-state" id="turnsEmpty" style="display:none">No data</div>
          </div>
        </div>
      </div>

      <!-- ── Lead Scoring Analytics ──────────────────────────────────────── -->
      <div id="leadSection">
        <div style="font-size:15px;font-weight:700;color:var(--text);margin:28px 0 16px;display:flex;align-items:center;gap:8px">
          <div style="width:4px;height:20px;background:linear-gradient(135deg,#10b981,#0891b2);border-radius:2px"></div>
          Lead Scoring Analytics
          <span style="font-size:11px;font-weight:500;color:#94a3b8;background:#f1f5f9;padding:3px 8px;border-radius:20px;margin-left:4px">Model Validation</span>
        </div>
        <div class="chart-grid-2">
          <div class="chart-card">
            <div class="chart-header"><div><div class="chart-title">Lead Score Distribution</div><div class="chart-sub">How scores spread across all callers (0\u2013100)</div></div></div>
            <canvas id="leadScoreDist"></canvas>
            <div class="empty-state" id="leadEmpty" style="display:none">No data</div>
          </div>
          <div class="chart-card">
            <div class="chart-header"><div><div class="chart-title">Avg Lead Score by Case Type</div><div class="chart-sub">Mean score per immigration category</div></div></div>
            <canvas id="leadByCase"></canvas>
            <div class="empty-state" id="leadCaseEmpty" style="display:none">No data</div>
          </div>
        </div>
        <div class="chart-card" style="margin-bottom:0">
          <div class="chart-header"><div><div class="chart-title">Urgency Level vs Call Outcome</div><div class="chart-sub">How triage classification correlates with resolution</div></div></div>
          <canvas id="urgencyOutcome" height="55"></canvas>
          <div class="empty-state" id="urgencyOutcomeEmpty" style="display:none">No data</div>
        </div>
      </div>

      <!-- ── Demographics & Risk Signals ────────────────────────────────── -->
      <div id="demographicsSection" style="padding-bottom:32px">
        <div style="font-size:15px;font-weight:700;color:var(--text);margin:28px 0 16px;display:flex;align-items:center;gap:8px">
          <div style="width:4px;height:20px;background:linear-gradient(135deg,#f59e0b,#ec4899);border-radius:2px"></div>
          Caller Demographics &amp; Risk Signals
          <span style="font-size:11px;font-weight:500;color:#94a3b8;background:#f1f5f9;padding:3px 8px;border-radius:20px;margin-left:4px">Training Context</span>
        </div>
        <div id="riskKpiGrid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:16px"></div>
        <div class="chart-grid-2">
          <div class="chart-card">
            <div class="chart-header"><div><div class="chart-title">Country of Origin</div><div class="chart-sub">Top 12 caller birth countries</div></div></div>
            <canvas id="countryDist"></canvas>
            <div class="empty-state" id="countryEmpty" style="display:none">No data</div>
          </div>
          <div class="chart-card">
            <div class="chart-header"><div><div class="chart-title">Prior Attorney Representation</div><div class="chart-sub">Callers who already had counsel, by case type</div></div></div>
            <canvas id="attorneyRate"></canvas>
            <div class="empty-state" id="attorneyEmpty" style="display:none">No data</div>
          </div>
        </div>
      </div>

      </div><!-- /analyticsView -->

      <!-- ── Chat View ──────────────────────────────────────── -->
      <div id="chatView" style="display:none;flex:1;overflow:hidden;flex-direction:column">
        <div style="background:var(--surface);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
          <div style="display:flex;align-items:center;gap:10px">
            <div style="width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;flex-shrink:0">
              <svg width="17" height="17" fill="white" viewBox="0 0 24 24"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>
            </div>
            <div>
              <div style="font-size:13px;font-weight:600;color:var(--text)">Staff AI Assistant</div>
              <div id="chatStatus" style="font-size:11px;color:#94a3b8">Ready</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:10px">
            <div style="display:flex;border:1px solid var(--border);border-radius:8px;overflow:hidden">
              <button id="langEN" onclick="setChatLang('en')" style="padding:6px 12px;font-size:12px;font-weight:500;border:none;cursor:pointer;font-family:inherit;background:#6366f1;color:#fff;transition:background 0.1s">EN</button>
              <button id="langES" onclick="setChatLang('es')" style="padding:6px 12px;font-size:12px;font-weight:500;border:none;cursor:pointer;font-family:inherit;background:transparent;color:var(--text-2);transition:background 0.1s">ES</button>
            </div>
            <button onclick="newChatSession()" style="display:flex;align-items:center;gap:6px;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:7px 12px;font-size:12px;font-weight:500;color:var(--text-2);cursor:pointer;font-family:inherit;transition:border-color 0.12s">
              <svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
              New Chat
            </button>
          </div>
        </div>
        <div id="chatMessages" style="flex:1;overflow-y:auto;padding:24px;display:flex;flex-direction:column;gap:14px;background:var(--bg)">
          <div id="chatWelcome" style="text-align:center;padding:48px 20px">
            <div style="width:56px;height:56px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;margin:0 auto 16px">
              <svg width="24" height="24" fill="white" viewBox="0 0 24 24"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>
            </div>
            <div style="font-size:16px;font-weight:600;color:var(--text);margin-bottom:8px">Staff AI Assistant</div>
            <div style="font-size:13px;color:var(--text-3);max-width:400px;margin:0 auto;line-height:1.6">Internal tool for attorneys, paralegals, and receptionists. Ask about caller situations, urgency triage, case types, or look up recent calls and intake records.</div>
            <div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:20px;max-width:520px;margin-left:auto;margin-right:auto">
              <button onclick="useSuggestion(this)" style="background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:7px 14px;font-size:12px;color:var(--text-2);cursor:pointer;font-family:inherit;transition:border-color 0.12s">What urgency flags should I watch for?</button>
              <button onclick="useSuggestion(this)" style="background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:7px 14px;font-size:12px;color:var(--text-2);cursor:pointer;font-family:inherit;transition:border-color 0.12s">Explain DACA renewal eligibility</button>
              <button onclick="useSuggestion(this)" style="background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:7px 14px;font-size:12px;color:var(--text-2);cursor:pointer;font-family:inherit;transition:border-color 0.12s">What intake questions to ask for a removal defense case?</button>
              <button onclick="useSuggestion(this)" style="background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:7px 14px;font-size:12px;color:var(--text-2);cursor:pointer;font-family:inherit;transition:border-color 0.12s">Summarise recent high-urgency calls</button>
            </div>
          </div>
        </div>
        <div style="background:var(--surface);border-top:1px solid var(--border);padding:16px 24px;flex-shrink:0">
          <div style="display:flex;gap:10px;align-items:flex-end;max-width:900px;margin:0 auto">
            <textarea id="chatInput" disabled placeholder="Ask about a caller, case type, urgency triage, or immigration procedure\u2026" oninput="autoResizeTextarea(this)" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChatMessage();}" style="flex:1;resize:none;border:1px solid var(--border);border-radius:10px;padding:10px 14px;font-size:13px;font-family:inherit;color:var(--text);background:var(--bg);outline:none;line-height:1.5;min-height:42px;max-height:120px;overflow-y:auto;transition:border-color 0.12s"></textarea>
            <button id="chatSendBtn" disabled onclick="sendChatMessage()" style="height:42px;width:42px;border-radius:10px;background:#6366f1;border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;opacity:0.5;transition:opacity 0.12s">
              <svg width="16" height="16" fill="none" stroke="white" stroke-width="2.5" viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
            </button>
          </div>
          <div style="text-align:center;font-size:11px;color:#cbd5e1;margin-top:8px">Internal staff tool \u00b7 Shift+Enter for new line \u00b7 Not a substitute for legal advice</div>
        </div>
      </div><!-- /chatView -->

    </div>
  </div>
</div>

<script>
Chart.defaults.font.family = "'Inter', -apple-system, sans-serif";
Chart.defaults.color = '#94a3b8';

const OUTCOME_C={booking_made:'#22c55e',transferred_to_staff:'#3b82f6',callback_requested:'#f59e0b',info_only:'#8b5cf6',dropped:'#ef4444',voicemail:'#64748b',no_answer:'#94a3b8'};
const CASE_C={family_sponsorship:'#6366f1',employment_visa:'#8b5cf6',asylum:'#ec4899',removal_defense:'#ef4444',daca:'#f59e0b',tps:'#10b981',naturalization:'#0891b2',other:'#64748b'};
const STATUS_C={new_lead:'#3b82f6',intake_scheduled:'#8b5cf6',intake_complete:'#6d28d9',active_client:'#10b981',closed:'#94a3b8',do_not_contact:'#ef4444'};
const URGENCY_C={critical:'#dc2626',high:'#ea580c',medium:'#d97706',routine:'#16a34a'};
const CHANNEL_C={phone:'#6366f1',sms:'#8b5cf6',whatsapp:'#22c55e',facebook:'#1d4ed8',instagram:'#ec4899',web_chat:'#0891b2'};
const FB=['#6366f1','#8b5cf6','#ec4899','#ef4444','#f59e0b','#10b981','#0891b2','#64748b'];
const OUTCOME_B={booking_made:'background:#f0fdf4;color:#16a34a;border:1px solid #bbf7d0',transferred_to_staff:'background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe',callback_requested:'background:#fffbeb;color:#b45309;border:1px solid #fde68a',dropped:'background:#fef2f2;color:#dc2626;border:1px solid #fecaca',voicemail:'background:#f8fafc;color:#475569;border:1px solid #e2e8f0',info_only:'background:#f5f3ff;color:#6d28d9;border:1px solid #ddd6fe',no_answer:'background:#f8fafc;color:#64748b;border:1px solid #e2e8f0'};
let _ch={};
function kC(){Object.values(_ch).forEach(c=>c.destroy());_ch={};}
function col(m,k){return m[k]||FB[0];}
function fmt(s){return(s||'').replace(/_/g,' ').replace(/\\b\\w/g,c=>c.toUpperCase());}
function fmtD(s){if(!s)return'\u2014';const m=Math.floor(s/60),r=s%60;return m?`${m}m ${r}s`:`${r}s`;}
function fmtT(iso){if(!iso)return'\u2014';try{return new Date(iso).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});}catch{return iso;}}

const KPI=[
  {k:'total_calls_30d',l:'Total Calls',s:'Last 30 days',p:'calls_prev_30d',ib:'#eff6ff',ic:'#2563eb',
   i:`<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z"/></svg>`},
  {k:'bookings_30d',l:'Consultations Booked',s:'Last 30 days',p:'bookings_prev_30d',ib:'#f0fdf4',ic:'#16a34a',
   i:`<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>`},
  {k:'active_clients',l:'Active Clients',s:'Current pipeline',ib:'#f5f3ff',ic:'#7c3aed',
   i:`<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>`},
  {k:'total_clients',l:'Total Clients',s:'All time',ib:'#fff7ed',ic:'#c2410c',
   i:`<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`},
  {k:'avg_duration_sec',l:'Avg Call Duration',s:'Last 30 days',f:fmtD,ib:'#fefce8',ic:'#a16207',
   i:`<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`},
  {k:'upcoming_appointments',l:'Upcoming Appointments',s:'Scheduled & confirmed',ib:'#fdf2f8',ic:'#be185d',
   i:`<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="15" x2="15" y2="15"/></svg>`},
];

function renderKPIs(s){
  const br=s.total_calls_30d?Math.round(s.bookings_30d/s.total_calls_30d*100):0;
  document.getElementById('kpiGrid').innerHTML=KPI.map(d=>{
    const raw=s[d.k]??0,val=d.f?d.f(raw):raw.toLocaleString();
    let tr='';
    if(d.p){const prev=s[d.p]??0,diff=raw-prev,pct=prev?Math.abs(Math.round(diff/prev*100)):0,cls=diff>0?'up':diff<0?'down':'neutral';tr=`<div class="kpi-trend ${cls}">${diff>=0?'\u2191':'\u2193'} ${pct}%</div>`;}
    if(d.k==='bookings_30d')tr=`<div class="kpi-trend ${br>=30?'up':br>0?'neutral':'down'}">${br}% rate</div>`;
    return`<div class="kpi-card"><div class="kpi-top"><div class="kpi-icon" style="background:${d.ib};color:${d.ic}">${d.i}</div>${tr}</div><div class="kpi-value">${val}</div><div class="kpi-label">${d.l} <span style="color:#cbd5e1;margin:0 4px">&middot;</span><span style="font-weight:400">${d.s}</span></div></div>`;
  }).join('');
}

function renderLine(data){
  if(!data.length){document.getElementById('callsChart').style.display='none';document.getElementById('callsEmpty').style.display='block';return;}
  _ch.calls=new Chart(document.getElementById('callsChart'),{type:'line',
    data:{labels:data.map(d=>new Date(d.day+'T00:00:00').toLocaleDateString(undefined,{month:'short',day:'numeric'})),
      datasets:[{label:'Calls',data:data.map(d=>d.count),borderColor:'#6366f1',
        backgroundColor:c=>{const g=c.chart.ctx.createLinearGradient(0,0,0,c.chart.height);g.addColorStop(0,'rgba(99,102,241,0.15)');g.addColorStop(1,'rgba(99,102,241,0)');return g;},
        borderWidth:2.5,tension:0.4,pointRadius:3,pointBackgroundColor:'#6366f1',pointBorderColor:'#fff',pointBorderWidth:2,fill:true}]},
    options:{responsive:true,plugins:{legend:{display:false},tooltip:{backgroundColor:'#0f172a',titleColor:'#e2e8f0',bodyColor:'#94a3b8',borderColor:'#334155',borderWidth:1,padding:10}},
      scales:{y:{beginAtZero:true,ticks:{stepSize:1,color:'#94a3b8'},grid:{color:'#f1f5f9'},border:{dash:[4,4]}},x:{ticks:{color:'#94a3b8',maxTicksLimit:12},grid:{display:false}}}}});
}
function donut(id,eid,data,cmap){
  if(!data.length){document.getElementById(id).style.display='none';document.getElementById(eid).style.display='block';return;}
  _ch[id]=new Chart(document.getElementById(id),{type:'doughnut',
    data:{labels:data.map(d=>fmt(d.label)),datasets:[{data:data.map(d=>d.count),backgroundColor:data.map(d=>col(cmap,d.label)),borderWidth:3,borderColor:'#fff',hoverOffset:8}]},
    options:{responsive:true,cutout:'65%',plugins:{legend:{position:'bottom',labels:{boxWidth:10,padding:14,font:{size:11}}},tooltip:{backgroundColor:'#0f172a',titleColor:'#e2e8f0',bodyColor:'#94a3b8',borderColor:'#334155',borderWidth:1,padding:10}}}});
}
function hbar(id,eid,data,cmap){
  if(!data.length){document.getElementById(id).style.display='none';document.getElementById(eid).style.display='block';return;}
  _ch[id]=new Chart(document.getElementById(id),{type:'bar',
    data:{labels:data.map(d=>fmt(d.label)),datasets:[{data:data.map(d=>d.count),backgroundColor:data.map(d=>col(cmap,d.label)+'22'),borderColor:data.map(d=>col(cmap,d.label)),borderWidth:1.5,borderRadius:5,borderSkipped:false}]},
    options:{indexAxis:'y',responsive:true,plugins:{legend:{display:false},tooltip:{backgroundColor:'#0f172a',titleColor:'#e2e8f0',bodyColor:'#94a3b8',borderColor:'#334155',borderWidth:1,padding:10}},
      scales:{x:{beginAtZero:true,ticks:{stepSize:1,color:'#94a3b8'},grid:{color:'#f1f5f9'},border:{dash:[4,4]}},y:{ticks:{color:'#374151',font:{size:11}},grid:{display:false}}}}});
}
// ── Table paging + sorting + search state ────────────────────────────────────
let _callsData=[],_callsPage=0,_callsSort={col:'started_at',dir:-1},_callsQ='';
let _intakeData=[],_intakePage=0,_intakeSort={col:'started_at',dir:-1},_intakeQ='';
const _PAGE=20;
function _matchQ(row,q){
  if(!q) return true;
  const words=q.toLowerCase().split(/\s+/).filter(Boolean);
  const haystack=Object.values(row).map(v=>v==null?'':String(v).toLowerCase()).join(' ');
  return words.every(w=>haystack.includes(w));
}
function _callsSearchInput(){_callsQ=document.getElementById('callsSearch').value;_callsPage=0;_renderCallsPage();}
function _intakeSearchInput(){_intakeQ=document.getElementById('intakeSearch').value;_intakePage=0;_renderIntakePage();}
function _filtered(data,q){return q?data.filter(r=>_matchQ(r,q)):data;}
function _cmp(a,b,col,dir){
  const av=a[col]??'',bv=b[col]??'';
  if(typeof av==='number'&&typeof bv==='number') return dir*(av-bv);
  return dir*String(av).localeCompare(String(bv));
}
function _sortIcon(key,s){
  if(s.col!==key) return '<span style="color:#cbd5e1;margin-left:3px">\u21c5</span>';
  return s.dir===1?'<span style="color:#6366f1;margin-left:3px">\u2191</span>':'<span style="color:#6366f1;margin-left:3px">\u2193</span>';
}
function _mkPager(page,total,prevFn,nextFn,el){
  if(!el) return;
  const pages=Math.ceil(total/_PAGE)||1;
  const from=total?page*_PAGE+1:0,to=Math.min((page+1)*_PAGE,total);
  const prevDis=page===0,nextDis=page>=pages-1;
  const btnS=(dis)=>`background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:5px 13px;font-size:12px;cursor:${dis?'default':'pointer'};color:${dis?'#94a3b8':'var(--text)'};font-family:inherit`;
  el.innerHTML=`<div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-top:1px solid var(--border);font-size:13px;color:var(--text-2)"><span style="font-size:12px">${from}\u2013${to} of ${total}</span><div style="display:flex;align-items:center;gap:6px"><button onclick="${prevFn}" ${prevDis?'disabled':''} style="${btnS(prevDis)}">&lsaquo; Prev</button><span style="background:#f1f5f9;border-radius:6px;padding:5px 10px;font-size:12px;color:#475569;white-space:nowrap">Page ${page+1} / ${pages}</span><button onclick="${nextFn}" ${nextDis?'disabled':''} style="${btnS(nextDis)}">Next &rsaquo;</button></div></div>`;
}

// ── Calls table ───────────────────────────────────────────────────────────────
function sortCalls(col){
  if(_callsSort.col===col)_callsSort.dir*=-1;else{_callsSort.col=col;_callsSort.dir=1;}
  _callsPage=0;_renderCallsPage();
}
function _callsPrev(){if(_callsPage>0){_callsPage--;_renderCallsPage();}}
function _callsNext(){if((_callsPage+1)*_PAGE<_filtered(_callsData,_callsQ).length){_callsPage++;_renderCallsPage();}}
function _renderCallsPage(){
  const filtered=_filtered(_callsData,_callsQ);
  const sorted=[...filtered].sort((a,b)=>_cmp(a,b,_callsSort.col,_callsSort.dir));
  const page=sorted.slice(_callsPage*_PAGE,(_callsPage+1)*_PAGE);
  const tb=document.getElementById('callsTableBody');
  document.querySelectorAll('#callsTable th[data-col]').forEach(th=>{
    const c=th.dataset.col;
    th.style.cssText=_callsSort.col===c?'cursor:pointer;user-select:none;color:#6366f1':'cursor:pointer;user-select:none';
    th.querySelector('.sh').innerHTML=_sortIcon(c,_callsSort);
  });
  const noMsg=_callsQ?`No calls match \u201c${_esc(_callsQ)}\u201d`:'No calls recorded yet';
  if(!page.length){tb.innerHTML=`<tr><td colspan="7" class="empty-state" style="padding:40px">${noMsg}</td></tr>`;}
  else tb.innerHTML=page.map(r=>{
    const bs=OUTCOME_B[r.outcome]||'background:#f8fafc;color:#475569;border:1px solid #e2e8f0';
    const txBtn=r.has_transcript
      ?`<button onclick="viewTranscript('${r.sid}','${(r.name||'').replace(/'/g,"\\'")}','${r.phone||''}')" style="background:#eef2ff;color:#4338ca;border:1px solid #c7d2fe;border-radius:6px;padding:4px 10px;font-size:12px;font-weight:500;cursor:pointer;font-family:inherit;white-space:nowrap">View</button>`
      :`<span style="color:#94a3b8;font-size:12px">\u2014</span>`;
    return`<tr><td class="td-main">${r.name}</td><td><span class="mono">${r.phone||'\u2014'}</span></td><td><span class="badge" style="background:#eef2ff;color:#4338ca;border:1px solid #c7d2fe">${fmt(r.channel)}</span></td><td><span class="badge" style="${bs}">${fmt(r.outcome)}</span></td><td>${fmtD(r.duration)}</td><td>${fmtT(r.started_at)}</td><td>${txBtn}</td></tr>`;
  }).join('');
  _mkPager(_callsPage,filtered.length,'_callsPrev()','_callsNext()',document.getElementById('callsPager'));
}
function renderTable(data){_callsData=data||[];_callsQ='';const el=document.getElementById('callsSearch');if(el)el.value='';_callsPage=0;_renderCallsPage();}

// ── Intake table ──────────────────────────────────────────────────────────────
const URGENCY_B={critical:'background:#fef2f2;color:#dc2626;border:1px solid #fecaca',high:'background:#fff7ed;color:#c2410c;border:1px solid #fed7aa',medium:'background:#fffbeb;color:#b45309;border:1px solid #fde68a',routine:'background:#f0fdf4;color:#16a34a;border:1px solid #bbf7d0'};
function sortIntake(col){
  if(_intakeSort.col===col)_intakeSort.dir*=-1;else{_intakeSort.col=col;_intakeSort.dir=1;}
  _intakePage=0;_renderIntakePage();
}
function _intakePrev(){if(_intakePage>0){_intakePage--;_renderIntakePage();}}
function _intakeNext(){if((_intakePage+1)*_PAGE<_filtered(_intakeData,_intakeQ).length){_intakePage++;_renderIntakePage();}}
function _renderIntakePage(){
  const filtered=_filtered(_intakeData,_intakeQ);
  const sorted=[...filtered].sort((a,b)=>_cmp(a,b,_intakeSort.col,_intakeSort.dir));
  const page=sorted.slice(_intakePage*_PAGE,(_intakePage+1)*_PAGE);
  const tb=document.getElementById('intakeTableBody');
  document.querySelectorAll('#intakeTable th[data-col]').forEach(th=>{
    const c=th.dataset.col;
    th.style.cssText=_intakeSort.col===c?'cursor:pointer;user-select:none;color:#6366f1':'cursor:pointer;user-select:none';
    th.querySelector('.sh').innerHTML=_sortIcon(c,_intakeSort);
  });
  const noMsg=_intakeQ?`No records match \u201c${_esc(_intakeQ)}\u201d`:'No intake records yet';
  if(!page.length){tb.innerHTML=`<tr><td colspan="9" class="empty-state" style="padding:40px">${noMsg}</td></tr>`;}
  else tb.innerHTML=page.map(r=>{
    const cs=CASE_C[r.case_type]||FB[7];
    const caseBadge=r.case_type?`<span class="badge" style="background:${cs}22;color:${cs};border:1px solid ${cs}44">${fmt(r.case_type)}</span>`:'\u2014';
    const us=URGENCY_B[r.urgency]||'background:#f8fafc;color:#475569;border:1px solid #e2e8f0';
    const urgBadge=r.urgency?`<span class="badge" style="${us}">${fmt(r.urgency)}</span>`:'\u2014';
    const detained=r.detained?'<span class="badge" style="background:#fef2f2;color:#dc2626;border:1px solid #fecaca">Yes</span>':'<span style="color:#94a3b8">No</span>';
    const pct=r.completeness||0,pctColor=pct>=80?'#16a34a':pct>=50?'#d97706':'#dc2626';
    const complete=`<div style="display:flex;align-items:center;gap:6px"><div style="width:52px;height:6px;border-radius:3px;background:#e2e8f0;overflow:hidden"><div style="height:100%;width:${pct}%;background:${pctColor};border-radius:3px"></div></div><span style="font-size:11px;color:${pctColor};font-weight:600">${pct}%</span></div>`;
    const courtDate=r.court_date?new Date(r.court_date+'T00:00:00').toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'}):'\u2014';
    return`<tr><td class="td-main">${r.name}</td><td><span class="mono">${r.phone||'\u2014'}</span></td><td>${caseBadge}</td><td>${urgBadge}</td><td>${courtDate}</td><td>${fmt(r.status)||'\u2014'}</td><td>${detained}</td><td>${complete}</td><td>${fmtT(r.started_at)}</td></tr>`;
  }).join('');
  _mkPager(_intakePage,filtered.length,'_intakePrev()','_intakeNext()',document.getElementById('intakePager'));
}
function renderIntakeTable(data){_intakeData=data||[];_intakeQ='';const el=document.getElementById('intakeSearch');if(el)el.value='';_intakePage=0;_renderIntakePage();}

// ── Helpers for new analytics charts ─────────────────────────────────────────
function hbarAvg(id,eid,data,labelKey,valKey,unit,color){
  if(!data||!data.length){document.getElementById(id).style.display='none';if(eid)document.getElementById(eid).style.display='block';return;}
  _ch[id]=new Chart(document.getElementById(id),{type:'bar',
    data:{labels:data.map(d=>fmt(d[labelKey])),datasets:[{data:data.map(d=>d[valKey]),
      backgroundColor:color+'22',borderColor:color,borderWidth:1.5,borderRadius:5,borderSkipped:false}]},
    options:{indexAxis:'y',responsive:true,plugins:{legend:{display:false},
      tooltip:{callbacks:{label:ctx=>`${ctx.parsed.x}${unit}`},backgroundColor:'#0f172a',titleColor:'#e2e8f0',bodyColor:'#94a3b8',borderColor:'#334155',borderWidth:1,padding:10}},
      scales:{x:{beginAtZero:true,ticks:{color:'#94a3b8'},grid:{color:'#f1f5f9'},border:{dash:[4,4]}},
              y:{ticks:{color:'#374151',font:{size:11}},grid:{display:false}}}}});
}
function vbar(id,eid,data,labelKey,valKey,colors){
  if(!data||!data.length){document.getElementById(id).style.display='none';if(eid)document.getElementById(eid).style.display='block';return;}
  const cs=Array.isArray(colors)?colors:data.map((_,i)=>FB[i%FB.length]);
  _ch[id]=new Chart(document.getElementById(id),{type:'bar',
    data:{labels:data.map(d=>d[labelKey]),datasets:[{data:data.map(d=>d[valKey]),
      backgroundColor:cs.map(c=>c+'44'),borderColor:cs,borderWidth:1.5,borderRadius:6,borderSkipped:false}]},
    options:{responsive:true,plugins:{legend:{display:false},
      tooltip:{backgroundColor:'#0f172a',titleColor:'#e2e8f0',bodyColor:'#94a3b8',borderColor:'#334155',borderWidth:1,padding:10}},
      scales:{y:{beginAtZero:true,ticks:{stepSize:1,color:'#94a3b8'},grid:{color:'#f1f5f9'},border:{dash:[4,4]}},
              x:{ticks:{color:'#374151',font:{size:11}},grid:{display:false}}}}});
}
function stackedBar(id,eid,data){
  if(!data||!data.length){document.getElementById(id).style.display='none';if(eid)document.getElementById(eid).style.display='block';return;}
  const urgencies=['critical','high','medium','routine','unknown'].filter(u=>data.some(d=>d.urgency===u));
  const outcomes=[...new Set(data.map(d=>d.outcome))];
  const datasets=outcomes.map(oc=>({
    label:fmt(oc),
    data:urgencies.map(u=>{const m=data.find(d=>d.urgency===u&&d.outcome===oc);return m?m.count:0;}),
    backgroundColor:(OUTCOME_C[oc]||FB[0])+'bb',borderColor:OUTCOME_C[oc]||FB[0],borderWidth:1,borderRadius:3,
  }));
  _ch[id]=new Chart(document.getElementById(id),{type:'bar',
    data:{labels:urgencies.map(fmt),datasets},
    options:{responsive:true,plugins:{legend:{position:'bottom',labels:{boxWidth:10,padding:12,font:{size:11}}},
      tooltip:{backgroundColor:'#0f172a',titleColor:'#e2e8f0',bodyColor:'#94a3b8',borderColor:'#334155',borderWidth:1,padding:10}},
      scales:{x:{stacked:true,ticks:{color:'#374151',font:{size:11}},grid:{display:false}},
              y:{stacked:true,beginAtZero:true,ticks:{stepSize:1,color:'#94a3b8'},grid:{color:'#f1f5f9'},border:{dash:[4,4]}}}}});
}
function stackedBarAtty(id,eid,data){
  if(!data||!data.length){document.getElementById(id).style.display='none';if(eid)document.getElementById(eid).style.display='block';return;}
  _ch[id]=new Chart(document.getElementById(id),{type:'bar',
    data:{labels:data.map(d=>fmt(d.label)),datasets:[
      {label:'With Attorney',data:data.map(d=>d.with_atty),backgroundColor:'#6366f1bb',borderColor:'#6366f1',borderWidth:1,borderRadius:3},
      {label:'Without',data:data.map(d=>d.without_atty),backgroundColor:'#e2e8f0',borderColor:'#94a3b8',borderWidth:1,borderRadius:3},
    ]},
    options:{indexAxis:'y',responsive:true,plugins:{legend:{position:'bottom',labels:{boxWidth:10,padding:12,font:{size:11}}},
      tooltip:{backgroundColor:'#0f172a',titleColor:'#e2e8f0',bodyColor:'#94a3b8',borderColor:'#334155',borderWidth:1,padding:10}},
      scales:{x:{stacked:true,beginAtZero:true,ticks:{color:'#94a3b8'},grid:{color:'#f1f5f9'},border:{dash:[4,4]}},
              y:{stacked:true,ticks:{color:'#374151',font:{size:11}},grid:{display:false}}}}});
}
function renderConvIntel(d){
  hbar('phaseDist','phaseEmpty',d.phase_distribution,CASE_C);
  hbar('intentDist','intentEmpty',d.intent_distribution,{});
  hbarAvg('latencyPhase','latencyEmpty',d.latency_by_phase,'label','avg',' ms','#8b5cf6');
  hbarAvg('turnsOutcome','turnsEmpty',d.avg_turns_by_outcome,'label','avg',' turns','#0891b2');
}
function renderLeadScoring(d){
  const bucketColors=['#ef4444','#f59e0b','#6366f1','#10b981','#16a34a'];
  vbar('leadScoreDist','leadEmpty',d.lead_score_buckets,'label','count',bucketColors);
  hbarAvg('leadByCase','leadCaseEmpty',d.avg_lead_by_case,'label','avg','','#10b981');
  stackedBar('urgencyOutcome','urgencyOutcomeEmpty',d.urgency_vs_outcome);
}
function renderRiskKpis(rf,repeatCallers,totalCallers){
  document.getElementById('riskKpiGrid').innerHTML=[
    {icon:'<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>',bg:'#eff6ff',ic:'#2563eb',val:totalCallers.toLocaleString(),lbl:'Unique Callers',sub:'All time'},
    {icon:'<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/></svg>',bg:'#f0fdf4',ic:'#16a34a',val:repeatCallers.toLocaleString(),lbl:'Repeat Callers',sub:'Called more than once'},
    {icon:'<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>',bg:'#fff7ed',ic:'#c2410c',val:(rf.deportation_pct||0)+'%',lbl:'Prior Deportation',sub:`${rf.prior_deportation||0} of ${rf.total||0} intakes`},
    {icon:'<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>',bg:'#fef2f2',ic:'#dc2626',val:(rf.criminal_pct||0)+'%',lbl:'Criminal History',sub:`${rf.criminal_history||0} of ${rf.total||0} intakes`},
  ].map(c=>`<div class="kpi-card"><div class="kpi-top"><div class="kpi-icon" style="background:${c.bg};color:${c.ic}">${c.icon}</div></div><div class="kpi-value" style="font-size:22px">${c.val}</div><div class="kpi-label">${c.lbl} <span style="color:#cbd5e1;margin:0 4px">&middot;</span><span style="font-weight:400">${c.sub}</span></div></div>`).join('');
}
function renderDemographics(d){
  renderRiskKpis(d.risk_flags||{},d.repeat_callers||0,d.total_unique_callers||0);
  hbar('countryDist','countryEmpty',d.country_distribution,{});
  stackedBarAtty('attorneyRate','attorneyEmpty',d.attorney_rate);
}

async function loadData(){
  try{
    const res=await fetch('/dashboard/api/stats',{credentials:'same-origin'});
    if(res.status===401){window.location.href='/dashboard/login';return;}
    if(!res.ok)throw new Error('HTTP '+res.status);
    const d=await res.json();
    kC();
    renderKPIs(d.summary);
    renderLine(d.calls_by_day);
    donut('outcomesChart','outcomesEmpty',d.outcomes,OUTCOME_C);
    hbar('caseTypesChart','caseTypesEmpty',d.case_types,CASE_C);
    hbar('statusChart','statusEmpty',d.client_statuses,STATUS_C);
    donut('urgencyChart','urgencyEmpty',d.urgency_levels,URGENCY_C);
    donut('channelsChart','channelsEmpty',d.channels,CHANNEL_C);
    renderTable(d.recent_calls);
    renderIntakeTable(d.intake_records);
    renderConvIntel(d);
    renderLeadScoring(d);
    renderDemographics(d);
    const now=new Date(),past=new Date(now);past.setDate(past.getDate()-30);
    document.getElementById('dateRange').textContent=past.toLocaleDateString(undefined,{month:'short',day:'numeric'})+' \u2013 '+now.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});
    document.getElementById('lastUpdated').textContent='Updated '+now.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});
    const u=document.getElementById('userNameDisplay').textContent;
    document.getElementById('userAvatar').textContent=(u[0]||'A').toUpperCase();
    document.getElementById('loadingOverlay').style.display='none';
    document.getElementById('appShell').style.display='flex';
  }catch(err){
    document.getElementById('loadingOverlay').innerHTML=`<div style="text-align:center"><p style="font-size:15px;font-weight:600;margin-bottom:8px">Failed to load</p><p style="font-size:13px;color:#64748b;margin-bottom:16px">${err.message}</p><button onclick="loadData()" style="background:#6366f1;color:#fff;border:none;border-radius:8px;padding:9px 18px;font-size:13px;cursor:pointer;font-family:inherit">Try again</button></div>`;
  }
}

loadData();
setInterval(loadData,5*60*1000);

// ── View switching ────────────────────────────────────────────────────────
let _currentView='analytics';
function showView(name){
  _currentView=name;
  document.getElementById('analyticsView').style.display=name==='analytics'?'':'none';
  const cv=document.getElementById('chatView');
  cv.style.display=name==='chat'?'flex':'none';
  document.getElementById('navDashboard').classList.toggle('active',name==='analytics');
  document.getElementById('navChat').classList.toggle('active',name==='chat');
  if(name==='chat'){
    document.getElementById('pageTitle').textContent='AI Chat';
    document.getElementById('dateRange').style.display='none';
    document.getElementById('topbarRight').style.display='none';
    if(!chatSessionId) initChatSession(chatLang);
  } else {
    document.getElementById('pageTitle').textContent='Analytics Overview';
    document.getElementById('dateRange').style.display='';
    document.getElementById('topbarRight').style.display='';
  }
}

// ── Chat ──────────────────────────────────────────────────────────────────
let chatWs=null,chatSessionId=null,chatLang='en',_aiBubble=null,_chatBusy=false,_typingBubble=null;

async function initChatSession(lang){
  chatLang=lang;
  document.getElementById('chatStatus').textContent='Connecting\u2026';
  document.getElementById('chatSendBtn').disabled=true;
  document.getElementById('chatSendBtn').style.opacity='0.5';
  document.getElementById('chatInput').disabled=true;
  try{
    const res=await fetch('/chat/session',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({language:lang,mode:'staff'})});
    if(!res.ok) throw new Error('HTTP '+res.status);
    const {session_id,ws_token}=await res.json();
    chatSessionId=session_id;
    const proto=location.protocol==='https:'?'wss':'ws';
    const ws=new WebSocket(`${proto}://${location.host}/chat/ws/${session_id}?token=${ws_token}`);
    ws.onopen=()=>{
      chatWs=ws;
      document.getElementById('chatStatus').textContent='Connected';
      document.getElementById('chatSendBtn').disabled=false;
      document.getElementById('chatSendBtn').style.opacity='1';
      document.getElementById('chatInput').disabled=false;
      document.getElementById('chatInput').focus();
    };
    ws.onmessage=onChatMsg;
    ws.onclose=()=>{
      chatWs=null;
      document.getElementById('chatStatus').textContent='Disconnected \u2014 click New Chat to reconnect';
      document.getElementById('chatSendBtn').disabled=true;
      document.getElementById('chatSendBtn').style.opacity='0.5';
    };
    ws.onerror=()=>document.getElementById('chatStatus').textContent='Connection error';
  }catch(e){
    document.getElementById('chatStatus').textContent='Error: '+e.message;
  }
}

function _showTyping(){
  if(_typingBubble) return;
  const msgs=document.getElementById('chatMessages');
  const wrap=document.createElement('div');
  wrap.style.cssText='display:flex;align-items:flex-start;gap:10px;margin-bottom:2px';
  wrap.innerHTML=`<div style="width:30px;height:30px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:2px"><svg width="13" height="13" fill="white" viewBox="0 0 24 24"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg></div><div class="typing-dots"><span></span><span></span><span></span></div>`;
  msgs.appendChild(wrap);
  _typingBubble=wrap;
  requestAnimationFrame(()=>{ msgs.scrollTop=msgs.scrollHeight; });
}
function _hideTyping(){
  if(_typingBubble){ _typingBubble.remove(); _typingBubble=null; }
}

function onChatMsg(event){
  const d=JSON.parse(event.data);
  if(d.type==='token'){
    if(!_aiBubble){ _hideTyping(); _aiBubble=_mkAiBubble(); }
    _aiBubble.textContent+=d.content;
    _scrollChat();
  } else if(d.type==='done'){
    _hideTyping(); _aiBubble=null; _chatBusy=false;
    document.getElementById('chatSendBtn').disabled=false;
    document.getElementById('chatSendBtn').style.opacity='1';
  } else if(d.type==='error'){
    _hideTyping(); _aiBubble=null; _chatBusy=false;
    _chatSysMsg('\u26a0 '+d.detail);
    document.getElementById('chatSendBtn').disabled=false;
    document.getElementById('chatSendBtn').style.opacity='1';
  }
}

function sendChatMessage(){
  const inp=document.getElementById('chatInput');
  const txt=inp.value.trim();
  if(!txt||!chatWs||chatWs.readyState!==WebSocket.OPEN||_chatBusy) return;
  inp.value=''; autoResizeTextarea(inp);
  _mkUserBubble(txt);
  chatWs.send(JSON.stringify({message:txt}));
  _chatBusy=true;
  document.getElementById('chatSendBtn').disabled=true;
  document.getElementById('chatSendBtn').style.opacity='0.5';
  _showTyping();
}

function _mkUserBubble(text){
  const msgs=document.getElementById('chatMessages');
  const d=document.createElement('div');
  d.style.cssText='display:flex;justify-content:flex-end;margin-bottom:2px';
  d.innerHTML=`<div style="max-width:72%;background:#6366f1;color:#fff;border-radius:16px 16px 4px 16px;padding:10px 14px;font-size:13px;line-height:1.5;white-space:pre-wrap">${_esc(text)}</div>`;
  msgs.appendChild(d); _scrollChat();
}

function _mkAiBubble(){
  const msgs=document.getElementById('chatMessages');
  const wrap=document.createElement('div');
  wrap.style.cssText='display:flex;align-items:flex-start;gap:10px;margin-bottom:2px';
  wrap.innerHTML=`<div style="width:30px;height:30px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:2px"><svg width="13" height="13" fill="white" viewBox="0 0 24 24"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg></div><div class="ai-b" style="max-width:75%;background:var(--surface);border:1px solid var(--border);border-radius:4px 16px 16px 16px;padding:10px 14px;font-size:13px;line-height:1.6;color:var(--text);white-space:pre-wrap"></div>`;
  msgs.appendChild(wrap); _scrollChat();
  return wrap.querySelector('.ai-b');
}

function _chatSysMsg(t){
  const msgs=document.getElementById('chatMessages');
  const d=document.createElement('div');
  d.style.cssText='text-align:center;font-size:12px;color:#94a3b8;padding:4px 0';
  d.textContent=t; msgs.appendChild(d); _scrollChat();
}

function _scrollChat(){const m=document.getElementById('chatMessages');m.scrollTop=m.scrollHeight;}
function _esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

function setChatLang(lang){
  if(lang===chatLang) return;
  chatLang=lang;
  document.getElementById('langEN').style.background=lang==='en'?'#6366f1':'transparent';
  document.getElementById('langEN').style.color=lang==='en'?'#fff':'var(--text-2)';
  document.getElementById('langES').style.background=lang==='es'?'#6366f1':'transparent';
  document.getElementById('langES').style.color=lang==='es'?'#fff':'var(--text-2)';
  newChatSession();
}

function newChatSession(){
  if(chatWs){chatWs.close();chatWs=null;}
  chatSessionId=null; _aiBubble=null; _chatBusy=false;
  document.getElementById('chatMessages').innerHTML=`<div id="chatWelcome" style="text-align:center;padding:48px 20px"><div style="width:56px;height:56px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;margin:0 auto 16px"><svg width="24" height="24" fill="white" viewBox="0 0 24 24"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg></div><div style="font-size:16px;font-weight:600;color:var(--text);margin-bottom:8px">Staff AI Assistant</div><div style="font-size:13px;color:var(--text-3);max-width:400px;margin:0 auto;line-height:1.6">Internal tool for attorneys, paralegals, and receptionists. Ask about caller situations, urgency triage, case types, or look up recent calls and intake records.</div><div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:20px;max-width:520px;margin-left:auto;margin-right:auto"><button onclick="useSuggestion(this)" style="background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:7px 14px;font-size:12px;color:var(--text-2);cursor:pointer;font-family:inherit">What urgency flags should I watch for?</button><button onclick="useSuggestion(this)" style="background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:7px 14px;font-size:12px;color:var(--text-2);cursor:pointer;font-family:inherit">Explain DACA renewal eligibility</button><button onclick="useSuggestion(this)" style="background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:7px 14px;font-size:12px;color:var(--text-2);cursor:pointer;font-family:inherit">What intake questions to ask for a removal defense case?</button><button onclick="useSuggestion(this)" style="background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:7px 14px;font-size:12px;color:var(--text-2);cursor:pointer;font-family:inherit">Summarise recent high-urgency calls</button></div></div>`;
  initChatSession(chatLang);
}

function useSuggestion(btn){
  const inp=document.getElementById('chatInput');
  inp.value=btn.textContent;
  autoResizeTextarea(inp);
  inp.focus();
}

function autoResizeTextarea(el){
  el.style.height='auto';
  el.style.height=Math.min(el.scrollHeight,120)+'px';
}

// ── Transcript modal ──────────────────────────────────────────────────────
async function viewTranscript(callSid, name, phone){
  const modal=document.getElementById('transcriptModal');
  const title=document.getElementById('transcriptTitle');
  const sub=document.getElementById('transcriptSub');
  const body=document.getElementById('transcriptBody');
  title.textContent=name||'Unknown Caller';
  sub.textContent=(phone||callSid||'')+'  \u00b7  '+callSid;
  body.innerHTML='<div style="text-align:center;padding:40px;color:#64748b">Loading\u2026</div>';
  modal.style.display='flex';
  try{
    const res=await fetch('/dashboard/api/transcript/'+encodeURIComponent(callSid),{credentials:'same-origin'});
    if(!res.ok) throw new Error('HTTP '+res.status);
    const data=await res.json();
    const msgs=data.messages||[];
    if(!msgs.length){body.innerHTML='<div style="text-align:center;padding:40px;color:#64748b">No transcript available for this call.</div>';return;}
    const seen=new Set();
    body.innerHTML=msgs.map(m=>{
      const key=m.turn_index+'|'+m.role;
      if(seen.has(key))return'';
      seen.add(key);
      const isUser=m.role==='caller'||m.role==='user';
      const roleLabel=isUser?'Caller':'AI Assistant';
      const roleBg=isUser?'#f0fdf4':'#eef2ff';
      const roleColor=isUser?'#16a34a':'#4338ca';
      const ms=m.latency_ms?`<span style="font-size:10px;color:#94a3b8;margin-left:6px">${m.latency_ms}ms</span>`:'';
      const phase=m.phase&&m.phase!=='greeting'?`<span style="font-size:10px;color:#94a3b8;margin-left:6px;text-transform:capitalize">${m.phase}</span>`:'';
      const content=(m.content||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return`<div style="display:flex;flex-direction:column;align-items:${isUser?'flex-start':'flex-end'};margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
          <span style="font-size:11px;font-weight:600;color:${roleColor};background:${roleBg};padding:2px 7px;border-radius:4px">${roleLabel}</span>${phase}${ms}
        </div>
        <div style="max-width:85%;background:${isUser?'#f8fafc':'#eef2ff'};border:1px solid ${isUser?'#e2e8f0':'#c7d2fe'};border-radius:${isUser?'4px 12px 12px 12px':'12px 4px 12px 12px'};padding:10px 14px;font-size:13px;line-height:1.5;color:#1e293b;white-space:pre-wrap">${content}</div>
      </div>`;
    }).join('');
  }catch(e){
    body.innerHTML='<div style="text-align:center;padding:40px;color:#ef4444">Failed to load transcript: '+e.message+'</div>';
  }
}
function closeTranscript(){
  document.getElementById('transcriptModal').style.display='none';
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeTranscript();});
</script>

<!-- Transcript modal -->
<div id="transcriptModal" style="display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,0.6);align-items:center;justify-content:center;padding:16px">
  <div style="background:#1e293b;border:1px solid #334155;border-radius:16px;width:100%;max-width:680px;max-height:90vh;display:flex;flex-direction:column;box-shadow:0 25px 50px rgba(0,0,0,0.5)">
    <!-- Header -->
    <div style="padding:20px 24px 16px;border-bottom:1px solid #334155;display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-shrink:0">
      <div>
        <div id="transcriptTitle" style="font-size:16px;font-weight:700;color:#f1f5f9"></div>
        <div id="transcriptSub" style="font-size:12px;color:#64748b;margin-top:3px;font-family:monospace"></div>
      </div>
      <button onclick="closeTranscript()" style="background:none;border:none;color:#94a3b8;cursor:pointer;padding:4px;border-radius:6px;line-height:1;font-size:18px;flex-shrink:0">\u2715</button>
    </div>
    <!-- Body -->
    <div id="transcriptBody" style="padding:24px;overflow-y:auto;flex:1"></div>
  </div>
</div>

</body>
</html>"""
