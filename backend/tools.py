"""
tools.py - מחלקת הכלים היחידה של הסוכן: ContractsToolkit.

מה הקובץ עושה:
    מגדיר את כל היכולות שהסוכן יכול להפעיל, מחולקות ל-5 קטגוריות:
        1. חיפוש ושליפה     - search_contracts, get_contract_metadata
        2. אנליזה            - list_expiring_contracts, summarize_contract, compare_contracts
        3. ניהול דרייב       - list_drive_files, delete_drive_file
        4. סנכרון ואינדוקס   - run_delta_sync, get_index_status
        5. הפקת מסמכים       - generate_word_document

איך זה עובד:
    ContractsToolkit יורשת מ-agno.tools.Toolkit ומעבירה את רשימת המתודות ב-tools=[...].
    Agno הופך כל מתודה לפונקציה (Function Calling) ששמה, החתימה וה-Docstring שלה
    נשלחים למודל כהגדרת כלי.

תרומה לחיסכון בטוקנים:
    - ה-Docstrings של הכלים קצרים בכוונה: הם נשלחים למודל בכל קריאה, ולכן כל מילה
      מיותרת בהם עולה טוקנים בכל הודעה. ההסבר המפורט נמצא בהערות (#) שלא נשלחות.
    - כל פלט של כלי עובר דרך _clip ומוגבל ל-TOOL_RESULT_MAX_CHARS.
    - כלים כמו list_expiring_contracts ו-get_index_status עונים מתוך טבלת המעקב
      ב-SQL, בלי RAG ובלי קריאה נוספת ל-LLM.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, List

from agno.tools import Toolkit
from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

from backend.config import Settings
from backend.services.drive_service import DriveService
from backend.services.rag_service import RagService

logger = logging.getLogger(__name__)


class ContractsToolkit(Toolkit):
    """
    ארגז הכלים של סוכן החוזים.

    מקבל את השירותים המשותפים (Drive, RAG) בבנאי - כך אין חיבורים כפולים
    ל-Postgres או לדרייב, והזיכרון של התהליך נשאר נמוך.
    """

    def __init__(self, settings: Settings, rag: RagService, drive: DriveService):
        """רושם את כל הכלים במפורש, לפי סדר הקטגוריות."""
        self.settings = settings
        self.rag = rag
        self.drive = drive
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
                # 3. ניהול דרייב
                self.list_drive_files,
                self.delete_drive_file,
                # 4. סנכרון ואינדוקס
                self.run_delta_sync,
                self.get_index_status,
                # 5. הפקת מסמכים
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
        """חיפוש היברידי בחוזים. מחזיר עד 5 מקטעים עם מקור (קובץ+סעיף).

        Args:
            query: שאלה או מילות מפתח בעברית.
            file_name: אופציונלי - שם/חלק משם קובץ לצמצום החיפוש לחוזה אחד.
        """
        # מגבלת limit=5 נאכפת בתוך rag.search - זו התקרה על גודל הקונטקסט מה-RAG
        file_id = None
        if file_name:
            f = self._resolve(file_name)
            if not f:
                return f"לא נמצא חוזה בשם '{file_name}'."
            file_id = f["file_id"]
        docs = self.rag.search(query, file_id=file_id)
        if not docs:
            return "לא נמצאו מקטעים רלוונטיים."
        results = [
            {
                "source": f"{d.meta_data.get('file_name')} | {d.meta_data.get('section')}",
                "content": d.content,
            }
            for d in docs
        ]
        return self._clip(results)

    def get_contract_metadata(self, file_name: str) -> str:
        """מטא-דאטה מובנה של חוזה: צדדים, נכס, גוש/חלקה, תוקף, תנאים כספיים.

        Args:
            file_name: שם או חלק משם הקובץ.
        """
        f = self._resolve(file_name)
        if not f:
            return f"לא נמצא חוזה בשם '{file_name}'."
        return self._clip({"file": f["name"], "file_id": f["file_id"], "status": f["status"],
                           "link": f.get("web_link"), **(f.get("metadata") or {})})

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
            # 3 מקטעים לכל חוזה = סה"כ 6, קרוב לתקרת ה-RAG הרגילה (5)
            f = self._resolve(name)
            if not f:
                out.append(f"לא נמצא חוזה בשם '{name}'.")
                continue
            entry: dict = {"file": f["name"], "metadata": f.get("metadata")}
            if topic:
                docs = self.rag.search(topic, file_id=f["file_id"], limit=3)
                entry["sections"] = [
                    {"section": d.meta_data.get("section"), "content": d.content.split("\n", 1)[-1]}
                    for d in docs
                ]
            out.append(self._clip(entry, per_file))
        return "\n\n".join(out)

    # ==================================================================
    # קטגוריה 3: ניהול דרייב
    # ==================================================================
    def list_drive_files(self) -> str:
        """רשימת הקבצים בתיקיית החוזים בדרייב, עם סטטוס האינדוקס של כל קובץ."""
        tracked = {r["file_id"]: r["status"] for r in self.rag.list_tracked()}
        files = [
            {"id": f["id"], "name": f["name"], "modified": f.get("modifiedTime", "")[:10],
             "indexed": tracked.get(f["id"], "pending")}
            for f in self.drive.list_files()
        ]
        return self._clip(files)

    def delete_drive_file(self, file_id: str) -> str:
        """מעביר קובץ לאשפה בדרייב ומוחק אותו מהאינדקס. רק לאחר אישור מפורש מהמשתמש.

        Args:
            file_id: מזהה הקובץ בדרייב (מ-list_drive_files).
        """
        self.drive.delete_file(file_id)
        self.rag.remove_file(file_id)
        return "הקובץ הועבר לאשפה בדרייב והוסר מהאינדקס."

    # ==================================================================
    # קטגוריה 4: סנכרון ואינדוקס
    # ==================================================================
    def run_delta_sync(self) -> str:
        """מסנכרן את האינדקס מול הדרייב - מעבד רק קבצים חדשים או שהשתנו."""
        return self._clip(self.rag.delta_sync())

    def get_index_status(self) -> str:
        """סטטוס האינדקס: כמה חוזים אונדקסו, כמה נכשלו, וכמה עמודים עברו OCR."""
        rows = self.rag.list_tracked()
        summary = {
            "total": len(rows),
            "indexed": sum(r["status"] == "indexed" for r in rows),
            "errors": [{"file": r["name"], "error": (r.get("error") or "")[:150]} for r in rows if r["status"] == "error"],
            "chunks": sum(r.get("chunks") or 0 for r in rows),
            "ocr_pages": sum(r.get("ocr_pages") or 0 for r in rows),
            "last_indexed": max((r["indexed_at"] for r in rows if r.get("indexed_at")), default=None),
        }
        return self._clip(summary)

    # ==================================================================
    # קטגוריה 5: הפקת מסמכים
    # ==================================================================
    def generate_word_document(self, title: str, content_markdown: str) -> str:
        """יוצר מסמך Word (RTL) מתוכן Markdown ומחזיר קישור להורדה.

        Args:
            title: כותרת המסמך.
            content_markdown: גוף המסמך ב-Markdown (#/## כותרות, - רשימות, **מודגש**).
        """
        # המסמך נשמר ב-generated_docs ומגובה לדרייב (הדיסק של Render זמני)
        safe_title = re.sub(r"[^\w֐-׿\- ]", "", title).strip().replace(" ", "_")[:60] or "document"
        filename = f"{safe_title}_{datetime.now():%Y%m%d_%H%M}_{uuid.uuid4().hex[:6]}.docx"
        path = Path(self.settings.GENERATED_DOCS_DIR) / filename

        doc = DocxDocument()
        _set_rtl_defaults(doc)
        _add_paragraph(doc, title, style="Title")
        for line in content_markdown.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("### "):
                _add_paragraph(doc, stripped[4:], style="Heading 3")
            elif stripped.startswith("## "):
                _add_paragraph(doc, stripped[3:], style="Heading 2")
            elif stripped.startswith("# "):
                _add_paragraph(doc, stripped[2:], style="Heading 1")
            elif stripped.startswith(("- ", "* ")):
                _add_paragraph(doc, stripped[2:], style="List Bullet")
            elif re.match(r"^\d+[.)]\s", stripped):
                _add_paragraph(doc, re.sub(r"^\d+[.)]\s", "", stripped), style="List Number")
            else:
                _add_paragraph(doc, stripped)
        doc.save(path)

        backup = self.drive.upload_generated_doc(str(path), filename)
        result = {"download_url": f"/api/docs/{filename}", "file_name": filename}
        if backup:
            result["drive_link"] = backup.get("webViewLink")
        return json.dumps(result, ensure_ascii=False)


# ----------------------------------------------------------------------
# פונקציות עזר ליצירת מסמך Word מימין לשמאל
# ----------------------------------------------------------------------
def _set_rtl_defaults(doc: Any) -> None:
    """מגדיר גופן Arial (תומך עברית) כברירת מחדל לכל הסגנונות במסמך."""
    for style in doc.styles:
        try:
            if style.font is not None:
                style.font.name = "Arial"
                rpr = style.element.get_or_add_rPr()
                fonts = rpr.find(qn("w:rFonts"))
                if fonts is None:
                    fonts = OxmlElement("w:rFonts")
                    rpr.append(fonts)
                fonts.set(qn("w:cs"), "Arial")
        except (AttributeError, ValueError):
            continue


def _add_paragraph(doc: Any, text: str, style: str | None = None) -> None:
    """
    מוסיף פסקה בכיוון RTL, עם תמיכה ב-**מודגש**.

    Word דורש סימון bidi ברמת הפסקה ו-rtl ברמת כל Run כדי להציג עברית נכון.
    """
    try:
        p = doc.add_paragraph(style=style) if style else doc.add_paragraph()
    except KeyError:
        p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    ppr = p._p.get_or_add_pPr()
    bidi = OxmlElement("w:bidi")
    bidi.set(qn("w:val"), "1")
    ppr.append(bidi)

    parts: List[str] = re.split(r"(\*\*[^*]+\*\*)", text)
    for part in parts:
        if not part:
            continue
        bold = part.startswith("**") and part.endswith("**")
        run = p.add_run(part[2:-2] if bold else part)
        run.bold = bold or None
        rpr = run._r.get_or_add_rPr()
        rtl = OxmlElement("w:rtl")
        rtl.set(qn("w:val"), "1")
        rpr.append(rtl)
