.DEFAULT_GOAL := help

# ─────────────────────────────────────────────────────────────────────────────
# Targets
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: help run test test-unit lint syntax migrate migrate-head db-shell \
        docker-up docker-down docker-logs clean

help:           ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── App ─────────────────────────────────────────────────────────────────────

run:            ## Start FastAPI dev server on port 3000 (reload enabled)
	uvicorn app.main:app --host 0.0.0.0 --port 3000 --reload

tunnel:         ## Expose port 3000 via ngrok (run in a separate terminal)
	@echo "Copy the HTTPS URL into BASE_URL in .env, then restart the server."
	ngrok http 3000

gen-key:        ## Generate a random 32-char DB encryption key
	@python3 -c "import secrets; print('DB_ENCRYPTION_KEY=' + secrets.token_hex(16))"

check-env:      ## Warn about unfilled required .env values
	@for var in TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_PHONE_NUMBER \
	            OPENAI_API_KEY DEEPGRAM_API_KEY \
	            ELEVENLABS_API_KEY ELEVENLABS_VOICE_ID_EN ELEVENLABS_VOICE_ID_ES \
	            GHL_API_KEY GHL_LOCATION_ID GHL_CALENDAR_ID \
	            GOOGLE_CALENDAR_ID SUPABASE_URL SUPABASE_ANON_KEY; do \
	  val=$$(grep -E "^$$var=" .env 2>/dev/null | cut -d= -f2-); \
	  [ -z "$$val" ] && echo "  MISSING: $$var" && MISSING=1; \
	done; \
	[ -z "$$MISSING" ] && echo "All required .env values are set." || exit 1

# ─── Tests ───────────────────────────────────────────────────────────────────

test:           ## Run all tests with coverage summary
	python3 -m pytest --tb=short -q

test-unit:      ## Run only fast unit tests (no integration markers)
	python3 -m pytest --tb=short -q \
	  tests/test_audio_utils.py \
	  tests/test_call_router.py \
	  tests/test_cost_tracker.py \
	  tests/test_twiml_responses.py \
	  tests/test_conversation_state.py \
	  tests/test_intake_flow.py \
	  tests/test_llm_agent.py \
	  tests/test_context_manager.py \
	  tests/test_urgency_classifier.py \
	  tests/test_lead_scorer.py \
	  tests/test_ghl_webhooks.py \
	  tests/test_compliance_middleware.py \
	  tests/test_telephony.py \
	  tests/test_social.py \
	  tests/test_twilio_webhooks.py \
	  tests/test_stt_tts.py \
	  tests/test_websocket_handler.py \
	  tests/test_crm.py \
	  tests/test_scheduling.py \
	  tests/test_db_worker.py \
	  tests/test_call_logger.py

migrate-head:   ## Print current Alembic revision
	alembic current

migrate:        ## Apply all pending Alembic migrations
	alembic upgrade head

db-shell:       ## Open a psql shell using DATABASE_URL from .env
	@export $$(grep -v '^#' .env | xargs) && psql "$$DATABASE_URL"

# ─── Docker ───────────────────────────────────────────────────────────────────

docker-up:      ## Start all services (postgres, redis, app) detached
	docker compose up -d --build

docker-down:    ## Stop and remove all containers
	docker compose down

docker-logs:    ## Follow logs from all containers
	docker compose logs -f

# ─── Cleanup ─────────────────────────────────────────────────────────────────

clean:          ## Remove Python cache files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
	find . -type f -name '*.pyc' -delete; \
	find . -type d -name '.pytest_cache' -exec rm -rf {} + 2>/dev/null; \
	echo "Clean."
