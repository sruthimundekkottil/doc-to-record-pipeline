"""Load configuration from the environment and the repository's .env."""

import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
CONFIDENCE_THRESHOLD = Decimal(os.getenv("CONFIDENCE_THRESHOLD") or "0.70")
