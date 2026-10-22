"""
config.py - טעינת משתני סביבה ואימות הגדרות.

מה הקובץ עושה:
    טוען את כל ההגדרות של המערכת מקובץ .env (בפיתוח מקומי) או ממשתני הסביבה
    של Render (בייצור), מאמת שכל ערך חובה קיים, ומנרמל ערכים בעייתיים
    (למשל כתובת ה-Postgres של Supabase).

איך זה עובד:
    מחלקת Settings יורשת מ-BaseSettings של pydantic-settings. בעת יצירת המופע
    pydantic קורא את המשתנים, ממיר טיפוסים, ונכשל מיד עם הודעה ברורה אם חסר ערך
    חובה - עדיף להיכשל בעליית השרת מאשר באמצע שיחה עם משתמש.

תרומה לחיסכון בטוקנים / זיכרון:
    כל "כפתורי הכיול" של צריכת הטוקנים מרוכזים כאן כקבועים:
    RAG_LIMIT (מספר מקטעים שנשלפים), HISTORY_RUNS (אורך היסטוריית השיחה),
    MIN_CHARS_PER_PAGE (סף ההחלטה אם להפעיל OCR יקר) ו-OCR_DPI (גודל התמונה
    שנשלחת ל-Vision - 150 DPI הוא איזון בין קריאוּת לבין עלות טוקנים וזיכרון).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# שורש הפרויקט (התיקייה שמעל backend/) - משמש לאיתור public/ ו-generated_docs/
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """
    אובייקט ההגדרות המרכזי של המערכת.

    כל שדה ממופה אוטומטית למשתנה סביבה באותו שם (ללא תלות באותיות גדולות/קטנות).
    שדות ללא ערך ברירת מחדל הם חובה - אם הם חסרים, השרת לא יעלה.
    """

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- חיבורים חיצוניים (חובה) ---
    SUPABASE_DB_URL: str  # כתובת Postgres (Supabase) - משמשת ל-PgVector, להיסטוריה ולטבלת המעקב
    OPENAI_API_KEY: str  # מפתח OpenAI - לסוכן, ל-Vision OCR ול-Embeddings
    DRIVE_CONTRACTS_FOLDER_ID: str  # מזהה תיקיית MITVE TOOLS - NECHASIM - CONTRACTS ב-Shared Drive
    SYNC_JOB_SECRET: str  # סוד משותף שמגן על נתיב הסנכרון שמופעל ע"י ה-Cron הלילי

    # --- הרשאות Google (אחד מהשניים חובה) ---
    # בפיתוח מקומי: נתיב לקובץ service_account.json
    GOOGLE_APPLICATION_CREDENTIALS: Optional[str] = None
    # ב-Render: תוכן ה-JSON כולו כמשתנה סביבה (אין צורך להעלות קובץ סודי לשרת)
    GOOGLE_SERVICE_ACCOUNT_JSON: Optional[str] = None

    # --- תבניות Word בדרייב (אופציונלי) ---
    # תיקייה עם תבניות docx/Google Docs שמכילות משתנים בתחביר Jinja ({{ tenant_name }}).
    # הסוכן מגלה אותן בעצמו (list_document_templates) וממלא אותן עם docxtpl.
    DRIVE_TEMPLATES_FOLDER_ID: Optional[str] = None
    TEMPLATE_LIST_CACHE_SECONDS: int = 300  # רשימת התבניות והמשתנים שלהן נשמרת בזיכרון 5 דקות

    # --- מודלים ---
    AGENT_MODEL: str = "gpt-4o"  # מודל הסוכן
    VISION_MODEL: str = "gpt-4o"  # מודל ה-OCR (Vision + Structured Outputs)
    METADATA_MODEL: str = "gpt-4o-mini"  # מודל זול לחילוץ מטא-דאטה מטקסט דיגיטלי בלבד
    EMBEDDING_MODEL: str = "text-embedding-3-small"  # מודל Embeddings זול ומהיר

    # --- כיול צריכת טוקנים ---
    RAG_LIMIT: int = 5  # מקסימום מקטעים שחוזרים מכל חיפוש - שולט ישירות בגודל הקונטקסט
    HISTORY_RUNS: int = 2  # כמה סבבי שיחה קודמים נכנסים במלואם; מה שקדם להם מגיע כסיכום מתגלגל
    MIN_CHARS_PER_PAGE: int = 50  # מתחת לסף הזה בממוצע לעמוד -> הקובץ נחשב סרוק ונשלח ל-OCR
    OCR_DPI: int = 150  # רזולוציית המרת עמוד לתמונה עבור Vision
    METADATA_SAMPLE_CHARS: int = 6000  # תקרת תווים מעמודי המפתח שנשלחים לחילוץ מטא-דאטה (70% התחלה, 30% סוף)
    METADATA_KEY_PAGES: int = 3  # עמודי מפתח: 3 ראשונים + 3 אחרונים - רק הם נשלחים למטא-דאטה, בכל אורך חוזה
    METADATA_MAX_OUTPUT_TOKENS: int = 1000  # תקרת פלט לחילוץ מטא-דאטה
    OCR_MAX_OUTPUT_TOKENS: int = 8000  # תקרת פלט לעמוד OCR; חריגה -> העמוד מפוצל לרצועות
    OCR_MAX_SPLIT_DEPTH: int = 2  # כמה פעמים מותר לפצל עמוד צפוף (2 = עד 4 רצועות)
    EMBED_BATCH_SIZE: int = 50  # מקטעים לכל מנת Embedding+הכנסה ל-PgVector
    MAX_CHUNK_CHARS: int = 1500  # גודל מקסימלי למקטע; סעיף ארוך יותר יפוצל (5 מקטעים ≈ תקרת הכלי)
    CHUNK_OVERLAP: int = 200  # חפיפה בין תת-מקטעים של סעיף ארוך כדי לא לאבד הקשר
    TOOL_RESULT_MAX_CHARS: int = 6000  # תקרת אורך לפלט של כל כלי - מונע "הצפת" הקונטקסט
    MAX_INDEX_RETRIES: int = 3  # אחרי כמה כשלונות על אותה גרסת קובץ הסנכרון הלילי מפסיק לנסות (חוסך OCR)
    CLASSIFIER_SAMPLE_CHARS: int = 4000  # תקרת תווים מ-2 העמודים הראשונים שנשלחים לסיווג סוג המסמך

    # --- סנכרון בזמן אמת (Drive Push Notifications) ---
    # הכתובת הציבורית של השרת (HTTPS). ב-Render מוגדר אוטומטית RENDER_EXTERNAL_URL;
    # בלי כתובת ציבורית ה-Webhook לא נרשם והמערכת נשארת עם סנכרון לילי + ידני.
    PUBLIC_BASE_URL: Optional[str] = os.getenv("RENDER_EXTERNAL_URL")
    WEBHOOK_DEBOUNCE_SECONDS: int = 20  # איחוד "גל" של התראות מהדרייב לסנכרון אחד
    DRIVE_WATCH_TTL_HOURS: int = 72  # תוקף ערוץ ההתראות; מחודש בעלייה ובסנכרון הלילי

    # --- טבלאות ב-Postgres ---
    VECTOR_TABLE: str = "contract_chunks"
    TRACKING_TABLE: str = "contract_files"
    SESSIONS_TABLE: str = "contract_agent_sessions"
    DB_SCHEMA: str = "ai"

    # --- נתיבים ---
    TMP_DIR: str = "/tmp" if os.name != "nt" else tempfile.gettempdir()
    GENERATED_DOCS_DIR: str = str(BASE_DIR / "generated_docs")
    PUBLIC_DIR: str = str(BASE_DIR / "public")
    TEMPLATES_DIR: str = str(BASE_DIR / "templates")  # תבניות Word מוסדיות (base.docx / <doc_type>.docx)
    GENERATED_DRIVE_SUBFOLDER: str = "generated"  # תת-תיקייה בדרייב לגיבוי מסמכי Word שנוצרו

    @field_validator("SUPABASE_DB_URL")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        """
        מנרמל את כתובת ה-Postgres לדרייבר psycopg 3.

        Supabase מספקת כתובת בפורמט postgresql:// או postgres://, אבל SQLAlchemy
        צריך לדעת באיזה דרייבר להשתמש. אנחנו מתקינים psycopg (גרסה 3), ולכן
        מוסיפים את הקידומת postgresql+psycopg://.
        """
        v = v.strip()
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v

    @property
    def webhook_token(self) -> str:
        """טוקן ערוץ ההתראות של הדרייב - נגזר מ-SYNC_JOB_SECRET, כך שאין סוד נוסף לנהל."""
        return hmac.new(self.SYNC_JOB_SECRET.encode(), b"drive-webhook", hashlib.sha256).hexdigest()

    def google_credentials_info(self) -> dict:
        """
        מחזיר את פרטי ה-Service Account כמילון.

        סדר עדיפויות: קודם משתנה הסביבה GOOGLE_SERVICE_ACCOUNT_JSON (מתאים ל-Render),
        ואם הוא לא קיים - קריאת הקובץ שבנתיב GOOGLE_APPLICATION_CREDENTIALS.
        נתיב יחסי מחושב ביחס לשורש הפרויקט.
        """
        if self.GOOGLE_SERVICE_ACCOUNT_JSON:
            return json.loads(self.GOOGLE_SERVICE_ACCOUNT_JSON)
        if self.GOOGLE_APPLICATION_CREDENTIALS:
            path = Path(self.GOOGLE_APPLICATION_CREDENTIALS)
            if not path.is_absolute():
                path = BASE_DIR / path
            return json.loads(path.read_text(encoding="utf-8"))
        raise RuntimeError(
            "חסרות הרשאות Google: הגדר GOOGLE_SERVICE_ACCOUNT_JSON או GOOGLE_APPLICATION_CREDENTIALS"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    מחזיר מופע יחיד (Singleton) של ההגדרות.

    lru_cache מבטיח שקובץ ה-.env נקרא פעם אחת בלבד לאורך חיי התהליך.
    """
    settings = Settings()  # type: ignore[call-arg]
    os.environ.setdefault("OPENAI_API_KEY", settings.OPENAI_API_KEY)  # עבור ספריות שקוראות ישירות מהסביבה
    Path(settings.GENERATED_DOCS_DIR).mkdir(parents=True, exist_ok=True)
    Path(settings.TMP_DIR).mkdir(parents=True, exist_ok=True)
    return settings
