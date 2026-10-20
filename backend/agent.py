"""
agent.py - הגדרת הסוכן היחיד של המערכת (Agno + OpenAI gpt-4o).

מה הקובץ עושה:
    בונה מופע אחד של Agent שמחובר ל:
        - מודל OpenAI gpt-4o.
        - מחלקת כלים אחת בלבד: ContractsToolkit (כולל כלי החיפוש ההיברידי ב-RAG).
        - PostgresDb (Supabase) לשמירת היסטוריית השיחות לפי session_id.

איך זה עובד:
    הסוכן נוצר פעם אחת בעליית השרת (Singleton) ומשותף לכל הבקשות. כל בקשה מעבירה
    session_id משלה ל-agent.run, ו-Agno טוען מ-Postgres את ההיסטוריה של אותה שיחה בלבד.
    החיפוש בחוזים מתבצע דרך הכלי search_contracts (ולא דרך מנגנון ה-Knowledge המובנה),
    כך שיש מסלול שליפה יחיד שאוכף limit=5 ומחזיר מקטעים עם Context Header.

תרומה לחיסכון בטוקנים / זיכרון:
    - num_history_runs=4: רק 4 סבבי השיחה האחרונים נכנסים לקונטקסט.
      (בגרסאות Agno ישנות הפרמטר נקרא num_history_responses; בגרסה המותקנת (3.x)
      השם הוא num_history_runs יחד עם add_history_to_context=True.)
    - max_tool_calls_from_history=2: תוצאות כלים ישנות (שעלולות להיות ארוכות) לא
      נגררות שוב ושוב לכל הודעה חדשה - נשארות רק 2 האחרונות.
    - search_knowledge=False: אין כלי חיפוש כפול; מסלול השליפה היחיד מוגבל ל-5 מקטעים.
    - ההיסטוריה נשמרת ב-Postgres ולא בזיכרון התהליך, כך שצריכת ה-RAM ב-Render
      לא גדלה עם מספר השיחות, והשיחות שורדות Restart/Deploy.
    - הוראות מערכת קצרות - הן נשלחות בכל קריאה.
"""

from __future__ import annotations

from agno.agent import Agent
from agno.db.postgres import PostgresDb
from agno.models.openai import OpenAIChat

from backend.config import Settings
from backend.services.drive_service import DriveService
from backend.services.rag_service import RagService
from backend.tools import ContractsToolkit

# הוראות המערכת - קצרות וממוקדות, כי הן חלק מכל בקשה למודל
INSTRUCTIONS = [
    "אתה סוכן החוזים וההסכמים של יחידת הנכסים. ענה תמיד בעברית, בקצרה ובמדויק.",
    "לכל שאלה על תוכן חוזה - הפעל search_contracts וענה רק על סמך המקטעים שהתקבלו.",
    "ציין מקור לכל עובדה בפורמט (שם קובץ, סעיף). אם המידע לא נמצא - אמור זאת במפורש ואל תנחש.",
    "לשאלות על תוקף/סיום חוזים השתמש ב-list_expiring_contracts; לפרטי חוזה - get_contract_metadata.",
    "השתמש ב-summarize_contract רק כשהמשתמש מבקש סיכום של חוזה שלם.",
    "לפני delete_drive_file בקש מהמשתמש אישור מפורש בהודעה נפרדת.",
    "כשמבקשים מסמך Word - נסח את התוכן ב-Markdown והפעל generate_word_document; החזר את קישור ההורדה כ-Markdown link.",
    "אין לך ייעוץ משפטי מחייב; במקרי ספק המלץ להתייעץ עם היועץ המשפטי.",
]


def build_agent(settings: Settings, rag: RagService, drive: DriveService) -> Agent:
    """
    יוצר את הסוכן היחיד של המערכת.

    PostgresDb מקבל את אותו Engine של שירות ה-RAG (db_engine=rag.engine), כך שכל
    המערכת משתמשת ב-Pool חיבורים אחד קטן - חוסך זיכרון וחיבורים פתוחים ב-Supabase.
    """
    storage = PostgresDb(
        db_engine=rag.engine,
        db_schema=settings.DB_SCHEMA,
        session_table=settings.SESSIONS_TABLE,
    )

    return Agent(
        name="סוכן חוזים - יחידת נכסים",
        model=OpenAIChat(id=settings.AGENT_MODEL, api_key=settings.OPENAI_API_KEY, temperature=0.1),
        tools=[ContractsToolkit(settings=settings, rag=rag, drive=drive)],
        instructions=INSTRUCTIONS,
        db=storage,
        # --- ניהול היסטוריה (חיסכון בטוקנים) ---
        add_history_to_context=True,
        num_history_runs=settings.HISTORY_RUNS,  # = 4, המקבילה ל-num_history_responses=4
        max_tool_calls_from_history=2,
        # --- RAG: מסלול שליפה יחיד דרך search_contracts (limit=5) ---
        search_knowledge=False,
        add_datetime_to_context=True,  # נדרש לחישוב "כמה זמן נשאר לחוזה"
        markdown=True,
        tool_call_limit=6,  # מונע לולאות כלים יקרות
    )
