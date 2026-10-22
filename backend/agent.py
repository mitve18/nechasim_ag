"""
agent.py - הגדרת הסוכן היחיד של המערכת (Agno + OpenAI).

מה הקובץ עושה:
    בונה מופע אחד של Agent שמחובר ל:
        - מודל OpenAI (AGENT_MODEL).
        - מחלקת כלים אחת בלבד: ContractsToolkit (כולל כלי החיפוש ההיברידי ב-RAG).
        - PostgresDb (Supabase) לשמירת היסטוריית השיחות לפי session_id.

איך זה עובד:
    הסוכן נוצר פעם אחת בעליית השרת (Singleton) ומשותף לכל הבקשות. כל בקשה מעבירה
    session_id משלה ל-agent.run, ו-Agno טוען מ-Postgres את ההיסטוריה של אותה שיחה בלבד.

תרומה לחיסכון בטוקנים / זיכרון:
    - Prompt Cache: OpenAI נותן 50% הנחה על קידומת זהה של הבקשה (הגדרות כלים +
      הוראות מערכת). לכן אין כאן add_datetime_to_context (שעה מדויקת = קידומת
      שמשתנה בכל בקשה), אלא תאריך יומי בלבד בסוף ההוראות - הקידומת יציבה כל היום.
    - num_history_runs=HISTORY_RUNS (2): רק הסבבים האחרונים נכנסים כהודעות מלאות.
      מה שקדם להם מגיע כסיכום מתגלגל קצר (RollingSummaryManager, מודל זול), שנוצר
      רק כשהשיחה ארוכה מחלון ההיסטוריה.
    - max_tool_calls_from_history=2: תוצאות כלים ישנות לא נגררות לכל הודעה חדשה.
    - search_knowledge=False: מסלול שליפה יחיד, מוגבל ל-5 מקטעים.
    - tool_call_limit=6: מונע לולאות כלים יקרות.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, List, Optional

from agno.agent import Agent
from agno.db.postgres import PostgresDb
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.session.summary import SessionSummaryManager

from backend.config import Settings
from backend.services.docx_service import DocxService
from backend.services.rag_service import RagService
from backend.services.template_service import TemplateService
from backend.tools import ContractsToolkit

# הוראות המערכת - קצרות וממוקדות, כי הן חלק מכל בקשה למודל.
# חשוב: לא להכניס כאן ערכים משתנים (חוץ מהתאריך בסוף) - זה שובר את ה-Prompt Cache.
INSTRUCTIONS = [
    "אתה סוכן החוזים וההסכמים של יחידת הנכסים. ענה תמיד בעברית, בקצרה ובמדויק.",
    "לכל שאלה על תוכן חוזה - הפעל search_contracts וענה רק על סמך המקטעים שהתקבלו.",
    "כשהשאלה מציינת סוג הסכם (שכירות/חכירה/הרשאה...) או נכס (גוש/חלקה/מתחם) - העבר document_type/asset "
    "ל-search_contracts כדי לצמצם את החיפוש.",
    "ציין מקור לכל עובדה בפורמט (שם קובץ, סעיף X). אם המידע לא נמצא - אמור זאת במפורש ואל תנחש.",
    "הודעה שמתחילה ב-[מיקוד: X] מתייחסת לחוזה X: העבר את X כ-file_name לכלים.",
    "לשאלות על תוקף/סיום חוזים השתמש ב-list_expiring_contracts; לפרטי חוזה - get_contract_metadata.",
    "השתמש ב-summarize_contract רק כשהמשתמש מבקש סיכום של חוזה שלם.",
    "כאשר המשתמש מבקש ליצור מסמך מבוסס תבנית:",
    "- השתמש ב-list_document_templates כדי למצוא את התבנית המתאימה.",
    "- זהה אילו משתנים נדרשים לתבנית ובקש מהמשתמש את המידע החסר בפעם אחת.",
    "- לאחר קבלת המידע (מלא קודם משתנים מהחוזה בעזרת get_contract_metadata ומהשיחה), "
    "השתמש ב-generate_document_from_template והחזר את download_url כקישור Markdown.",
    "כאשר המשתמש מבקש לנסח מסמך חופשי (שאינו מבוסס תבנית) באמצעות draft_document_html:",
    "- לעולם אל תקרא ל-draft_document_html באופן מיידי ללא פרטים (אלא אם התבקשת במפורש).",
    "- נתח אילו פרטים מהותיים חסרים למסמך המבוקש (שמות צדדים, ח.פ/ת.ז, תאריכים, סכומים, "
    "פרטי נכס/גוש/חלקה/מתחם ותנאים ספציפיים).",
    "- אם ההקשר קשור לחוזה קיים, חפש את המידע קודם בעזרת get_contract_metadata או search_contracts "
    "כדי לא לשאול על מידע שניתן לאחזר.",
    "- שאל את המשתמש שאלות מקצועיות וממוקדות כרשימת תבליטים כדי לאסוף את המידע החסר, והמתן לתשובתו.",
    "- במידה ונענית חלקית, שאל שאלות המשך רק על הפרטים הקריטיים שעדיין חסרים.",
    "- קרא ל-draft_document_html רק כאשר נאסף מספיק מידע ליצירת מסמך מפורט ומקצועי. "
    "זכור: הטיוטה נפתחת ישירות לעריכה, לכן אין לחזור על התוכן שלה בטקסט התשובה.",
    "- כללי ניסוח לטיוטה: עברית משפטית רשמית ומקצועית. בחוזה/התחייבות חובה לכלול: כותרת רשמית, "
    "הגדרת הצדדים (שם, ת.ז/ח.פ וכתובת), סעיפי רקע הפותחים ב'הואיל ו...', סעיפים אופרטיביים ממוספרים "
    "(1., 1.1 - מספור ידני בתוך <p>), וסעיף סמכות שיפוט בסיום.",
    "- עיצוב ה-content של draft_document_html: HTML בלבד (לא Markdown), עטוף כולו ב-"
    "<div dir=\"rtl\" style=\"text-align: right; font-family: Arial, sans-serif;\">, ללא שורות ריקות וללא הזחה.",
    "- פרטי המפתח (צדדים, תאריך, נכס) יוצגו בראש המסמך בטבלה <table style=\"width: 100%; border: none;\">; "
    "אין לקנן טבלה בתוך טבלה.",
    "- בסוף המסמך תמיד בלוק חתימות בטבלת HTML: עמודה לכל צד, ושורות לשם, תפקיד, תאריך וחתימה/חותמת.",
    "- חריגים: אם בקשת המשתמש כבר מכילה את כל הפרטים המהותיים, או אם הוא מבקש במפורש לנסח את המסמך "
    "מיד ללא שאלות, נסח את המסמך מיד והשתמש במצייני מקום (כגון [___]) עבור נתונים חסרים.",
    "אין לך ייעוץ משפטי מחייב; במקרי ספק המלץ להתייעץ עם היועץ המשפטי.",
]


def _instructions() -> List[str]:
    """ההוראות הקבועות + תאריך יומי בסוף (נדרש לחישוב 'כמה זמן נשאר לחוזה')."""
    return [*INSTRUCTIONS, f"התאריך היום: {date.today().isoformat()}"]


SUMMARY_PROMPT = (
    "סכם בעברית, בקצרה (עד 5 משפטים), את השיחה בין המשתמש לסוכן החוזים: "
    "אילו חוזים נדונו, עובדות מרכזיות שנמצאו (סכומים, תאריכים, סעיפים) ושאלות פתוחות. "
    "שלב את הסיכום הקודם אם קיים.\n"
)


@dataclass
class RollingSummaryManager(SessionSummaryManager):
    """
    סיכום שיחה מתגלגל וזול.

    - נוצר רק כשמספר הסבבים עולה על חלון ההיסטוריה (min_runs) - שיחה קצרה לא
      משלמת על סיכום בכלל.
    - הסיכום הקודם מוזרק לפרומפט, כך שמספיק לשלוח רק את הסבבים האחרונים
      (last_n_runs) ולא את כל השיחה בכל פעם.
    """

    min_runs: int = 2

    def create_session_summary(self, session: Any, run_metrics: Optional[Any] = None):
        if len(getattr(session, "runs", None) or []) <= self.min_runs:
            return None
        return super().create_session_summary(session, run_metrics)

    def _prepare_summary_messages(self, session: Any = None) -> Optional[List[Message]]:
        messages = super()._prepare_summary_messages(session)
        previous = getattr(getattr(session, "summary", None), "summary", None)
        if messages and previous:
            messages[0].content = f"סיכום קודם: {previous}\n\n{messages[0].content}"
        return messages


def build_agent(settings: Settings, rag: RagService, docx: DocxService, templates: TemplateService) -> Agent:
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
    summary_manager = RollingSummaryManager(
        model=OpenAIChat(id=settings.METADATA_MODEL, api_key=settings.OPENAI_API_KEY, temperature=0),
        session_summary_prompt=SUMMARY_PROMPT,
        last_n_runs=settings.HISTORY_RUNS + 1,
        min_runs=settings.HISTORY_RUNS,
    )

    return Agent(
        name="סוכן חוזים - יחידת נכסים",
        model=OpenAIChat(id=settings.AGENT_MODEL, api_key=settings.OPENAI_API_KEY, temperature=0.1),
        tools=[ContractsToolkit(settings=settings, rag=rag, docx=docx, templates=templates)],
        instructions=_instructions,
        db=storage,
        # --- ניהול היסטוריה (חיסכון בטוקנים) ---
        add_history_to_context=True,
        num_history_runs=settings.HISTORY_RUNS,
        max_tool_calls_from_history=2,
        session_summary_manager=summary_manager,
        add_session_summary_to_context=True,
        # --- RAG: מסלול שליפה יחיד דרך search_contracts (limit=5) ---
        search_knowledge=False,
        markdown=True,
        tool_call_limit=6,  # מונע לולאות כלים יקרות
    )
