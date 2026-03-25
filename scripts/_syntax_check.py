import ast
import sys
import pathlib

files = [
    "app/telephony/call_transfer.py",
    "app/telephony/twiml_responses.py",
    "app/telephony/voicemail.py",
    "app/telephony/outbound_callback.py",
    "app/logging_analytics/call_logger.py",
    "app/logging_analytics/sentiment_scorer.py",
    "app/logging_analytics/structured_data.py",
    "app/logging_analytics/cost_tracker.py",
    "app/logging_analytics/db_worker.py",
    "app/social/webhook_handler.py",
    "app/social/channel_router.py",
    "app/compliance/middleware.py",
    "app/agent/llm_agent.py",
    "app/voice/websocket_handler.py",
    "app/voice/context_manager.py",
    "app/telephony/call_router.py",
    "app/config.py",
    "app/main.py",
    "app/webhooks/twilio_webhooks.py",
    "app/database/migrations/versions/0001_initial.py",
    "app/database/migrations/versions/0002_compliance_tables.py",
    "app/database/migrations/env.py",
    "scripts/purge_pii.py",
    "scripts/retry_failed_tasks.py",
    "scripts/generate_fillers.py",
    "tests/test_audio_utils.py",
    "tests/test_call_router.py",
    "tests/test_cost_tracker.py",
    "tests/test_twiml_responses.py",
    "tests/test_conversation_state.py",
    "tests/test_intake_flow.py",
    "tests/test_llm_agent.py",
    "tests/test_context_manager.py",
    "tests/test_urgency_classifier.py",
    "tests/test_lead_scorer.py",
    "tests/test_ghl_webhooks.py",
    "tests/test_compliance_middleware.py",
    "tests/test_websocket_handler.py",
    "tests/test_resilience.py",
    "tests/test_crm.py",
    "tests/test_scheduling.py",
    "tests/test_sentiment_scorer.py",
    "tests/test_structured_data.py",
    "tests/test_call_logger.py",
    "tests/test_db_worker.py",
    "tests/test_telephony.py",
    "tests/test_social.py",
    "tests/test_twilio_webhooks.py",
    "tests/test_stt_tts.py",
]

errors = []
for f in files:
    p = pathlib.Path(f)
    if not p.exists():
        errors.append(f"MISSING: {f}")
        continue
    try:
        ast.parse(p.read_text())
        print(f"OK  {f}")
    except SyntaxError as e:
        errors.append(f"SYNTAX ERROR in {f}: {e}")

print()
if errors:
    for e in errors:
        print("FAIL", e)
    sys.exit(1)
else:
    print("All files pass syntax check.")
