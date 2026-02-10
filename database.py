import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")

if not url or not key:
    raise EnvironmentError("Supabase URL or Key not found in .env file")

supabase: Client = create_client(url, key)

# This is the single, fixed ID for the SaaS settings row
SAAS_SETTINGS_ID = "11111111-1111-1111-1111-111111111111"

# --- NEW FUNCTION ---
def get_saas_settings():
    """
    Fetches the global SaaS Admin settings (logo, contact, payment info).
    """
    try:
        # This table is public, so it can be read before login.
        res = supabase.table('saas_settings').select('*').eq('id', SAAS_SETTINGS_ID).maybe_single().execute()
        if res.data:
            return res.data
        return {}
    except Exception as e:
        print(f"[DB_ERROR] get_saas_settings: {e}")
        return {}