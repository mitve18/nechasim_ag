"""
tools.py - מחלקת הכלים היחידה של הסוכן: ContractsToolkit.

מה הקובץ עושה:
    מגדיר את כל היכולות שהסוכן יכול להפעיל, מחולקות ל-4 קטגוריות:
        1. חיפוש ושליפה     - search_contracts, get_contract_metadata
        2. אנליזה            - list_expiring_contracts, summarize_contract, compare_contracts
        3. אינדוקס           - get_index_status
        4. הפקת מסמכים       - prepare_document, generate_word_document

    פעולות ניהול הדרייב (מחיקה, סנכרון, רשימת קבצים) קיימות בממשק בלבד ולא כאן:
    כך הגדרות הכלים שנשלחות בכל קריאה קטנות יותר, והסוכן לא יכול למחוק קובץ בטעות.

איך זה עובד:
    ContractsToolkit יורשת מ-agno.tools.Toolkit ומעבירה את רשימת המתודות ב-tools=[...].
    Agno הופך כל מתודה לפונקציה (Function Calling) ששמה, החתימה וה-Docstring שלה
    נשלחים למודל כהגדרת כלי.

תרומה לחיסכון בטוקנים:
    - ה-Docstrings של הכלים קצרים בכוונה: הם נשלחים למודל בכל קריאה.
    - search_contracts מחלק את תקציב התווים בין המקטעים *לפני* ההמרה ל-JSON, כך
      שכל 5 המקטעים מגיעים למודל (ולא 2 מקטעים ו-JSON שבור), ושורת ההקשר של כל
      קובץ נשלחת פעם אחת בלבד גם כשכמה מקטעים מגיעים מאותו חוזה.
    - במסמכים מובנים המודל מחזיר רק ערכי שדות - לא את כל גוף המסמך.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from agno.tools import Toolkit

from backend.config import Settings
from backend.doc_templates import DOC_TYPES, prefill_from_metadata, render_body
from backend.services.docx_service import DocxService
from backend.services.rag_service import RagService

logger = logging.getLogger(__name__)


class ContractsToolkit(Toolkit):
    """
    ארגז הכלים של סוכן החוזים.

    מקבל את השירותים המשותפים (RAG, Word) בבנאי - כך אין חיבורים כפולים
    ל-Postgres או לדרייב, והזיכרון של התהליך נשאר נמוך.
    """

    def __init__(self, settings: Settings, rag: RagService, docx: DocxService):
        """רושם את כל הכלים במפורש, לפי סדר הקטגוריות."""
        self.settings = settings
        self.rag = rag
        self.docx = docx
        super().__init__(
            name="contracts_toolkit",
            tools=[
                # 1. חיפוש ושליפה
                self.search_contracts,
                self.get_contract_metadata,
                # 2. אנליזה
                self.list_expiring_contracts,
                self.summarize_contract,
                self.compare_contracts,
                # 3. אינדוקס
                self.get_index_status,
                # 4. הפקת מסמכים
                self.prepare_document,
                self.generate_word_document,
            ],
        )

    # ------------------------------------------------------------------
    # עזר: הגבלת אורך פלט
    # ------------------------------------------------------------------
    def _clip(self, value: Any, limit: int | None = None) -> str:
        """
        ממיר פלט ל-JSON/טקסט ומקצץ אותו לתקרה.

        זה "שסתום הביטחון" של הקונטקסט: גם אם כלי מחזיר הרבה נתונים, למודל
        יגיעו לכל היותר TOOL_RESULT_MAX_CHARS תווים.
        """
        limit = limit or self.settings.TOOL_RESULT_MAX_CHARS
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= limit else text[:limit] + "...[קוצר]"

    def _resolve(self, name_or_id: str) -> dict | None:
        """מאתר חוזה בטבלת המעקב לפי מזהה או חלק משם הקובץ."""
        return self.rag.find_file(name_or_id.strip())

    # ==================================================================
    # קטגוריה 1: חיפוש ושליפה
    # ==================================================================
    def search_contracts(self, query: str, file_name: str = "") -> str:
        """חיפוש היברידי בחוזים. מחזיר עד 5 מקטעים מקובצים לפי חוזה, עם סעיף.

        Args:
            query: שאלה או מילות מפתח בעברית.
            file_name: אופציונלי - שם/חלק משם קובץ לצמצום החיפוש לחוזה אחד.
        """
        file_id = None
        if file_name:
            f = self._resolve(file_name)
            if not f:
                return f"לא נמצא חוזה בשם '{file_name}'."
            file_id = f["file_id"]
        docs = self.rag.search(query, file_id=file_id)
        if not docs:
            return "לא נמצאו מקטעים רלוונטיים."
        return json.dumps(group_chunks(docs, self.settings.TOOL_RESULT_MAX_CHARS), ensure_ascii=False)

    def get_contract_metadata(self, file_name: str) -> str:
        """מטא-דאטה מובנה של חוזה: צדדים, נכס, גוש/חלקה, תוקף, תנאים כספיים.

        Args:
            file_name: שם או חלק משם הקובץ.
        """
        f = self._resolve(file_name)
        if not f:
            return f"לא נמצא חוזה בשם '{file_name}'."
        return self._clip({"file": f["name"], "status": f["status"], **(f.get("metadata") or {})})

    # ==================================================================
    # קטגוריה 2: אנליזה
    # ==================================================================
    def list_expiring_contracts(self, days: int = 90) -> str:
        """חוזים שמסתיימים בתוך X ימים (כולל שפגו). מבוסס מטא-דאטה, ללא חיפוש.

        Args:
            days: טווח ימים מהיום (ברירת מחדל 90).
        """
        items = self.rag.expiring_contracts(int(days))
        return self._clip(items) if items else f"אין חוזים שמסתיימים ב-{days} הימים הקרובים."

    def summarize_contract(self, file_name: str) -> str:
        """מחזיר את טקסט החוזה (מקוצר) לצורך סיכום. השתמש רק כשנדרש סיכום מלא.

        Args:
            file_name: שם או חלק משם הקובץ.
        """
        # הטקסט מוגבל לתקרה; לשאלות נקודתיות עדיף search_contracts שזול בהרבה
        f = self._resolve(file_name)
        if not f:
            return f"לא נמצא חוזה בשם '{file_name}'."
        body = self.rag.get_file_chunks(f["file_id"], max_chars=self.settings.TOOL_RESULT_MAX_CHARS)
        return body or "לחוזה אין תוכן מאונדקס."

    def compare_contracts(self, file_a: str, file_b: str, topic: str = "") -> str:
        """השוואה בין שני חוזים: מטא-דאטה + מקטעים רלוונטיים לנושא מכל חוזה.

        Args:
            file_a: שם החוזה הראשון.
            file_b: שם החוזה השני.
            topic: נושא ההשוואה (למשל 'דמי שכירות', 'אופציה'). ריק = מטא-דאטה בלבד.
        """
        out = []
        # חצי מהתקרה לכל חוזה, כדי ששניהם ייכנסו לקונטקסט באופן שווה
        per_file = self.settings.TOOL_RESULT_MAX_CHARS // 2
        for name in (file_a, file_b):
            f = self._resolve(name)
            if not f:
                out.append(f"לא נמצא חוזה בשם '{name}'.")
                continue
            entry: dict = {"file": f["name"], "metadata": f.get("metadata")}
            if topic:
                # 3 מקטעים לכל חוזה; התקציב מתחלק ביניהם כמו ב-search_contracts
                docs = self.rag.search(topic, file_id=f["file_id"], limit=3)
                budget = max(200, (per_file - len(json.dumps(entry, ensure_ascii=False))) // max(len(docs), 1))
                entry["sections"] = [
                    {"section": d.meta_data.get("section"),
                     "text": _truncate(_strip_header(d.content), budget)}
                    for d in docs
                ]
            out.append(self._clip(entry, per_file))
        return "\n\n".join(out)

    # ==================================================================
    # קטגוריה 3: אינדוקס
    # ==================================================================
    def get_index_status(self) -> str:
        """סטטוס האינדקס: כמה חוזים אונדקסו, כמה נכשלו, וכמה עמודים עברו OCR."""
        return self._clip(self.rag.index_status())

    # ==================================================================
    # קטגוריה 4: הפקת מסמכים (אשף)
    # ==================================================================
    def prepare_document(self, doc_type: str, file_name: str = "") -> str:
        """שלב 1 בהפקת מסמך: מחזיר שדות נדרשים, מה כבר ידוע מהחוזה ומה חסר.

        Args:
            doc_type: termination_notice / option_exercise / payment_demand / free.
            file_name: אופציונלי - החוזה שהמסמך מתייחס אליו (למילוי אוטומטי).
        """
        if doc_type not in DOC_TYPES:
            return json.dumps({"error": "סוג לא מוכר",
                               "types": {k: v["title"] for k, v in DOC_TYPES.items()}}, ensure_ascii=False)
        spec = DOC_TYPES[doc_type]
        known: Dict[str, str] = {}
        if file_name:
            f = self._resolve(file_name)
            if f:
                known = prefill_from_metadata(doc_type, f.get("metadata") or {})
        missing = {k: label for k, label in spec["fields"].items() if k not in known}
        return json.dumps({
            "doc_type": doc_type, "title": spec["title"], "known": known,
            "missing": missing, "optional": spec["optional"],
            "next": "שאל את המשתמש על השדות החסרים בהודעה אחת, ואז הפעל generate_word_document."
            if missing else "אפשר להפעיל generate_word_document.",
        }, ensure_ascii=False)

    def generate_word_document(self, doc_type: str, title: str = "", fields_json: str = "{}",
                               content_markdown: str = "") -> str:
        """שלב 2: מפיק מסמך Word (RTL) מתבנית ומחזיר קישור להורדה.

        Args:
            doc_type: כמו ב-prepare_document.
            title: כותרת (ריק = כותרת ברירת המחדל של הסוג).
            fields_json: JSON של ערכי השדות, למשל {"recipient": "..."}. לסוגים מובנים.
            content_markdown: גוף המסמך ב-Markdown - רק לסוג free.
        """
        if doc_type not in DOC_TYPES:
            doc_type = "free"
        try:
            fields: Dict[str, str] = {k: str(v) for k, v in json.loads(fields_json or "{}").items()}
        except (json.JSONDecodeError, AttributeError):
            return "fields_json אינו JSON תקין."
        spec = DOC_TYPES[doc_type]
        missing = [label for k, label in spec["fields"].items() if not fields.get(k)]
        if missing:
            return f"חסרים שדות: {', '.join(missing)}. שאל את המשתמש לפני ההפקה."
        body = content_markdown if spec["body_template"] is None else render_body(doc_type, fields)
        result = self.docx.build(title or spec["title"], body, fields=fields, doc_type=doc_type)
        return json.dumps(result, ensure_ascii=False)


# ----------------------------------------------------------------------
# עזרי עיצוב תוצאות חיפוש (משמשים גם את main.py לחילוץ מקורות)
# ----------------------------------------------------------------------
def _strip_header(content: str) -> str:
    """מסיר את שורת ההקשר [קובץ: ...] מראש המקטע."""
    return content.split("\n", 1)[-1] if content.startswith("[") else content


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def group_chunks(docs: List[Any], budget: int) -> List[Dict[str, Any]]:
    """
    מקבץ מקטעים לפי חוזה: שורת הקשר אחת לכל חוזה + רשימת סעיפים.

    התקציב מחולק שווה בין המקטעים אחרי שמנכים את שורות ההקשר, וכל מקטע נחתך
    בנפרד - כך ה-JSON תמיד תקין וכל המקטעים מגיעים למודל.
    """
    groups: Dict[str, Dict[str, Any]] = {}
    for d in docs:
        name = d.meta_data.get("file_name") or ""
        if name not in groups:
            header = d.content.split("\n", 1)[0] if d.content.startswith("[") else f"[קובץ: {name}]"
            groups[name] = {"file": name, "context": header, "sections": []}
        groups[name]["sections"].append({"section": d.meta_data.get("section"), "text": _strip_header(d.content)})

    overhead = sum(len(g["context"]) + len(g["file"]) + 40 for g in groups.values()) + 30 * len(docs)
    per_chunk = max(200, (budget - overhead) // max(len(docs), 1))
    for g in groups.values():
        for s in g["sections"]:
            s["text"] = _truncate(s["text"], per_chunk)
    return list(groups.values())
