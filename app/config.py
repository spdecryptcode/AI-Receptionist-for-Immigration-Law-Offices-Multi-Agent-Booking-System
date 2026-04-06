from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from zoneinfo import ZoneInfo
from functools import cached_property


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server
    port: int = 3000
    host: str = "0.0.0.0"
    base_url: str = "https://your-domain.ngrok.io"

    @cached_property
    def base_host(self) -> str:
        """Hostname without scheme, e.g. 'abc.ngrok-free.app' — used in wss:// and https:// URLs."""
        url = self.base_url
        for prefix in ("https://", "http://"):
            if url.startswith(prefix):
                return url[len(prefix):]
        return url

    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str
    twilio_transfer_number: str = ""

    # OpenAI
    openai_api_key: str
    openai_model: str = "gpt-4o"

    # Deepgram
    deepgram_api_key: str
    deepgram_use_native_mulaw: bool = False

    # ElevenLabs
    elevenlabs_api_key: str
    elevenlabs_voice_id_en: str
    elevenlabs_voice_id_es: str
    # Fallback if individual IDs not set
    elevenlabs_voice_id: str = ""

    # GoHighLevel
    ghl_api_key: str
    ghl_location_id: str
    ghl_calendar_id: str
    ghl_webhook_secret: str = ""

    # Google Calendar
    google_calendar_id: str
    google_service_account_key: str = "service-account.json"

    # Redis
    redis_url: str = "redis://localhost:6379"

    # Supabase / PostgreSQL
    supabase_url: str
    supabase_anon_key: str
    database_url: str = ""  # kept for Alembic migrations only; runtime uses Supabase
    db_encryption_key: str = ""

    # Office hours & routing
    office_hours_start: str = "09:00"
    office_hours_end: str = "18:00"
    office_timezone: str = "America/New_York"
    oncall_attorney_phone: str = ""
    law_firm_name: str = "Immigration Law Office"

    # Optional — used by voicemail.py, outbound_callback.py, social channels
    attorney_alert_phone: str = ""        # SMS recipient for emergency voicemails
    office_direct_number: str = ""        # Front-desk number for cold transfers
    ghl_default_assignee_id: str = ""     # GHL user ID for task assignment
    booking_url: str = ""                 # Online booking link sent in SMS/social

    # Capacity & resilience
    max_concurrent_calls: int = 10
    silence_warning_timeout: int = 15
    silence_hard_timeout: int = 30
    call_duration_soft_minutes: int = 15
    call_duration_hard_minutes: int = 20

    # Dashboard
    dashboard_username: str = "admin"
    dashboard_password: str = "changeme"

    # Compliance
    recording_consent_enabled: bool = True
    transcript_retention_days: int = 30
    data_retention_days: int = 90

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.office_timezone)

    def get_voice_id(self, language: str = "en") -> str:
        """Return the correct ElevenLabs voice ID for the given language."""
        if language == "es":
            return self.elevenlabs_voice_id_es or self.elevenlabs_voice_id
        return self.elevenlabs_voice_id_en or self.elevenlabs_voice_id


settings = Settings()
