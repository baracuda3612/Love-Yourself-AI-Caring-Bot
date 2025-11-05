
import os
from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN","")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY","")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit())
TZ = os.getenv("TZ","Europe/Kyiv")
MODEL = os.getenv("MODEL","gpt-4o-mini")
MAX_TOKENS = int(os.getenv("MAX_TOKENS","300"))
TEMPERATURE = float(os.getenv("TEMPERATURE","0.7"))
DB_URL = "postgresql://postgres:nnsTAPJmdbKvCjmMtndnXcyUOieFiHXN@postgres.railway.internal:5432/railway"
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT","10"))
DEFAULT_SEND_HOUR = int(os.getenv("DEFAULT_SEND_HOUR","9"))
