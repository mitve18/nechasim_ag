"""
rag_service.py - לב מערכת ה-RAG: טבלת מעקב, פיענוח/OCR, חלוקה ל-Chunks, אינדוקס וחיפוש.

מה הקובץ עושה:
    1. מנהל טבלת מעקב (contract_files) ב-Postgres שזוכרת לכל קובץ בדרייב את ה-id,
       טביעת האצבע (md5Checksum), סטטוס, מטא-דאטה מובנה ומספר המקטעים שנוצרו.
    2. מבצע Delta Sync: משווה את רשימת הקבצים בדרייב לטבלת המעקב ומעבד רק
       קבצים חדשים או כאלה שהשתנו. קבצים שנמחקו מהדרייב נמחקים גם מהאינדקס.
    3. מפענח כל קובץ: טקסט דיגיטלי עם PyMuPDF (חינם), ורק עמודים סרוקים/תמונות
       נשלחים ל-gpt-4o Vision עם Structured Outputs.
    4. מחלק את החוזה למקטעים לפי סעיפים ומזריק לכל מקטע שורת הקשר (Context Header).
    5. מאנדקס ב-PgVector (SearchType.hybrid) ומבצע חיפוש היברידי (וקטורי + מילות מפתח).

תרומה לחיסכון בטוקנים:
    - Delta Sync = קובץ מפוענח ומשולם פעם אחת בלבד; סנכרון לילי על תיקייה שלא
      השתנתה עולה 0 טוקנים.
    - OCR רק לעמודים שבאמת צריכים אותו (פחות מ-50 תווים), בשאר העמודים - חילוץ טקסט
      מקומי בחינם. עבור קובץ דיגיטלי נשלחים רק 6,000 התווים הראשונים למודל זול
      (gpt-4o-mini) לצורך מטא-דאטה.
    - Context Header בכל מקטע: הסוכן מקבל 5 מקטעים "שמסבירים את עצמם" ולא צריך
      לשלוף מקטעים נוספים רק כדי להבין לאיזה חוזה/נכס/תקופה הם שייכים.
    - מטא-דאטה מובנה בטבלת המעקב מאפשר לענות על שאלות כמו "אילו חוזים מסתיימים
      החודש" בשאילתת SQL - בלי LLM ובלי RAG בכלל.

חוזים ארוכים מאוד (ללא הגבלת עמודים):
    - כל קריאת LLM חסומה בגודל: OCR עמוד-עמוד עם תקרת פלט (עמוד צפוף מפוצל לרצועות),
      ומטא-דאטה רק מעמודי המפתח (התחלה וסוף) - גודל הבקשה לא תלוי באורך החוזה.
    - עמוד בעייתי מסומן כ"לא פוענח" והחוזה ממשיך; קובץ שנכשל לא עוצר את הסנכרון.
    - המקטעים מוגבלים בגודל ונכנסים ל-PgVector במנות קטנות.

ניהול זיכרון ב-Render:
    - עיבוד קובץ אחד בכל פעם, ובתוך הקובץ עמוד אחד בכל פעם (Pixmap משוחרר מיד).
    - הקבצים הזמניים נמחקים ב-finally ו-gc.collect() נקרא אחרי כל קובץ.
    - Engine יחיד של SQLAlchemy עם Pool קטן משותף ל-PgVector, להיסטוריה ולטבלת המעקב.
"""

from __future__ import annotations

import base64
import gc
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF
import openai
from openai import ContentFilterFinishReasonError, LengthFinishReasonError, OpenAI
from pydantic import BaseModel, Field
from sqlalchemy import (
    Column, DateTime, Integer, MetaData, String, Table, Text, case, create_engine, delete, select, text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.engine import Engine

from agno.knowledge.document import Document
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.vectordb.pgvector import PgVector, SearchType

from backend.config import Settings
from backend.services.drive_service import DriveService

logger = logging.getLogger(__name__)


# ======================================================================
# סכמות Structured Outputs - המודל חייב להחזיר JSON בדיוק במבנה הזה
# ======================================================================
# הערה: ה-Docstring של מחלקות הסכמה נשלח למודל כחלק מה-JSON Schema בכל קריאה,
# ולכן ההסבר המפורט כתוב כאן כהערה (#) ולא כ-Docstring - חיסכון בטוקנים בכל עמוד OCR.
#
# ContractMetadata - מטא-דאטה מובנה של חוזה. כל השדות אופציונליים כי עמוד בודד
# מכיל בדרך כלל רק חלק מהמידע; השירות ממזג את המטא-דאטה של כל העמודים לרשומה אחת.
class ContractMetadata(BaseModel):
    parties: List[str] = Field(default_factory=list, description="שמות הצדדים לחוזה (משכיר/שוכר, מוכר/קונה וכו')")
    property: Optional[str] = Field(None, description="תיאור הנכס / כתובת")
    block_parcel: Optional[str] = Field(None, description="גוש וחלקה (ותת-חלקה אם יש), לדוגמה: גוש 6623 חלקה 12")
    start_date: Optional[str] = Field(None, description="תאריך תחילת ההסכם בפורמט YYYY-MM-DD")
    end_date: Optional[str] = Field(None, description="תאריך סיום ההסכם בפורמט YYYY-MM-DD")
    financial_terms: Optional[str] = Field(None, description="סיכום תנאים כספיים: דמי שכירות, הצמדה, ערבויות, מועדי תשלום")


# PageExtraction - תוצאת OCR לעמוד בודד: תוכן ב-Markdown + מטא-דאטה שנמצא בעמוד.
class PageExtraction(BaseModel):
    markdown: str = Field(description="תוכן העמוד ב-Markdown. כל סעיף חדש מתחיל בכותרת '## סעיף X'")
    metadata: ContractMetadata


# הנחיות קצרות ומדויקות ל-Vision - פרומפט קצר = פחות טוקני קלט בכל עמוד
OCR_SYSTEM_PROMPT = (
    "אתה מומחה OCR לחוזים משפטיים בעברית. תמלל את העמוד במדויק ל-Markdown. "
    "כל סעיף ממוספר מתחיל בשורה '## סעיף X' (X = מספר הסעיף כפי שמופיע). "
    "תת-סעיפים כרשימה. טבלאות כטבלת Markdown. אל תמציא טקסט ואל תסכם. "
    "במטא-דאטה מלא רק מה שמופיע בעמוד הזה, בקצרה (financial_terms עד 300 תווים); "
    "תאריכים בפורמט YYYY-MM-DD."
)

METADATA_SYSTEM_PROMPT = (
    "חלץ מטא-דאטה מחוזה בעברית. מלא רק מידע שמופיע במפורש בטקסט. תאריכים בפורמט YYYY-MM-DD. "
    "financial_terms - סיכום קצר עד 400 תווים."
)

# שגיאות OpenAI שאחריהן מכשילים את הקובץ כולו (והוא ינוסה שוב בסנכרון הבא) במקום
# לאנדקס אותו עם "חורים": עומס/ניתוק שנמשכו גם אחרי הניסיונות החוזרים של ה-SDK,
# או בעיית הרשאה/מכסה שתשפיע על כל העמודים.
_FATAL_OPENAI_ERRORS = (
    openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
    openai.InternalServerError, openai.AuthenticationError, openai.PermissionDeniedError,
)

# תקרות מטא-דאטה: בחוזה של מאות עמודים כל עמוד מוסיף צדדים/תנאים, ובלי תקרה
# שורת ההקשר (שנכנסת לכל מקטע ול-Embedding שלו) הייתה גדלה ללא הגבלה
_MAX_PARTIES = 8
_MAX_FINANCIAL_CHARS = 1500
_MAX_HEADER_CHARS = 400
_MAX_SECTION_TITLE = 80

# זיהוי כותרת סעיף בטקסט דיגיטלי: "סעיף 5", "5.", "5)" בתחילת שורה
_DIGITAL_SECTION_RE = re.compile(r"^\s*(?:סעיף\s+)?(\d{1,3})\s*[.)]\s+(?=\S)|^\s*סעיף\s+(\d{1,3})\b")
# פיצול Markdown לפי כותרות "## ..."
_MD_HEADING_RE = re.compile(r"(?m)^##\s+(.+)$")


class RagService:
    """
    שירות ה-RAG המרכזי. נוצר פעם אחת בעליית השרת ומשותף לכל הבקשות ולסוכן.

    מחזיק:
        - engine: חיבור Postgres יחיד (Pool קטן) לכל המערכת.
        - vector_db: מופע PgVector עם חיפוש היברידי.
        - tracking: טבלת המעקב של Delta Sync.
        - _sync_lock: מונע הרצה מקבילה של שני סנכרונים (Cron + לחיצה ידנית).
    """

    def __init__(self, settings: Settings, drive: DriveService):
        """
        מאתחל את החיבור ל-Postgres, את PgVector ואת טבלת המעקב.

        - prepare_threshold=None: מבטל Prepared Statements של psycopg, שאינם נתמכים
          ב-Pooler של Supabase (פורט 6543) וגורמים לשגיאות לסירוגין.
        - pool_size=3: מספיק לשרת קטן וחוסך זיכרון וחיבורים ב-Supabase.
        """
        self.settings = settings
        self.drive = drive
        # max_retries: ה-SDK של OpenAI מבצע Backoff אקספוננציאלי אוטומטי לשגיאות זמניות
        # בלבד (429, 5xx, Timeout, ניתוק רשת) - עמוד OCR לא נכשל בגלל עומס רגעי.
        self.openai = OpenAI(api_key=settings.OPENAI_API_KEY, max_retries=4, timeout=120)
        self._sync_lock = threading.Lock()

        self.engine: Engine = create_engine(
            settings.SUPABASE_DB_URL,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=2,
            pool_recycle=1800,
            connect_args={"prepare_threshold": None},
        )

        # PgVector עם חיפוש היברידי: וקטורי (משמעות) + Full-Text (מספרי גוש/חלקה, שמות).
        # content_language="simple" - ל-Postgres אין מילון עברית; "simple" מפרק למילים
        # בלי Stemming, וזה מה שמאפשר התאמה מדויקת למספרים ולשמות בעברית.
        self.vector_db = PgVector(
            table_name=settings.VECTOR_TABLE,
            schema=settings.DB_SCHEMA,
            db_engine=self.engine,
            embedder=OpenAIEmbedder(id=settings.EMBEDDING_MODEL, api_key=settings.OPENAI_API_KEY),
            search_type=SearchType.hybrid,
            content_language="simple",
        )
        self.vector_db.create()  # יוצר את ה-Extension vector, את הסכמה ואת הטבלה אם אינם קיימים

        self._metadata = MetaData(schema=settings.DB_SCHEMA)
        self.tracking = Table(
            settings.TRACKING_TABLE,
            self._metadata,
            Column("file_id", String, primary_key=True),  # מזהה הקובץ בדרייב
            Column("name", Text, nullable=False),
            Column("mime_type", String),
            Column("fingerprint", String),  # md5Checksum (או modifiedTime ל-Google Docs)
            Column("status", String, nullable=False, default="pending"),  # indexed / error / pending
            Column("extraction", String),  # digital / ocr / mixed - לצורכי ניטור עלויות
            Column("metadata", JSONB),  # צדדים, נכס, גוש-חלקה, תאריכים, תנאים כספיים
            Column("chunks", Integer, default=0),
            Column("pages", Integer, default=0),
            Column("ocr_pages", Integer, default=0),
            Column("web_link", Text),
            Column("error", Text),
            Column("retries", Integer, default=0),  # כשלונות רצופים על אותה גרסת קובץ
            Column("indexed_at", DateTime(timezone=True)),
        )
        with self.engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {settings.DB_SCHEMA}"))
        self._metadata.create_all(self.engine)
        # create_all לא משנה טבלה קיימת - מוסיפים עמודות חדשות ידנית (Idempotent)
        with self.engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {settings.DB_SCHEMA}.{settings.TRACKING_TABLE} "
                "ADD COLUMN IF NOT EXISTS retries INTEGER DEFAULT 0"
            ))

    # ==================================================================
    # Delta Sync
    # ==================================================================
    def delta_sync(self) -> Dict[str, Any]:
        """
        מסנכרן את האינדקס מול תיקיית הדרייב - מעבד רק את מה שהשתנה.

        אלגוריתם:
            1. שליפת רשימת הקבצים מהדרייב (id + fingerprint) - קריאת API זולה.
            2. שליפת כל הרשומות מטבלת המעקב.
            3. קובץ שה-fingerprint שלו זהה ו-status=indexed -> דילוג (0 טוקנים).
            4. קובץ חדש / שהשתנה / שנכשל בעבר -> הורדה, פיענוח ואינדוקס מחדש.
            5. קובץ שקיים בטבלה ולא בדרייב -> מחיקת המקטעים והרשומה.

        מחזיר סיכום מספרי שמוצג בממשק ובלוגים של ה-Cron.
        """
        if not self._sync_lock.acquire(blocking=False):
            return {"status": "busy", "message": "סנכרון כבר רץ ברקע"}
        try:
            drive_files = self.drive.list_files()
            with self.engine.connect() as conn:
                rows = {r.file_id: r for r in conn.execute(select(self.tracking)).fetchall()}

            summary = {"status": "ok", "total": len(drive_files), "new": 0, "updated": 0,
                       "skipped": 0, "removed": 0, "failed": 0, "gave_up": 0, "errors": []}
            drive_ids = set()
            for f in drive_files:
                drive_ids.add(f["id"])
                row = rows.get(f["id"])
                same_version = bool(row) and row.fingerprint == f["fingerprint"]
                if same_version and row.status == "indexed":
                    summary["skipped"] += 1  # ה-Delta Sync בפעולה: אין שינוי -> אין עלות
                    continue
                if same_version and row.status == "error" and (row.retries or 0) >= self.settings.MAX_INDEX_RETRIES:
                    # קובץ שנכשל שוב ושוב (פגום/מוצפן) לא נשלח ל-OCR יקר בכל לילה;
                    # ינוסה שוב רק כשתעלה גרסה חדשה שלו (fingerprint שונה)
                    summary["gave_up"] += 1
                    continue
                try:
                    result = self.index_file(f)
                    summary["updated" if row else "new"] += 1
                    if result.get("failed_pages"):
                        summary["errors"].append(f"{f.get('name')}: {len(result['failed_pages'])} עמודים לא פוענחו")
                except Exception as e:  # noqa: BLE001 - כשל בקובץ אחד (גם כבד במיוחד) לא עוצר את הסנכרון
                    summary["failed"] += 1
                    summary["errors"].append(f"{f.get('name')}: {type(e).__name__}: {str(e)[:200]}")
                finally:
                    gc.collect()  # קובץ ענק לא משאיר זיכרון תפוס לקובץ הבא

            for file_id in set(rows) - drive_ids:
                try:
                    self.remove_file(file_id)
                    summary["removed"] += 1
                except Exception as e:  # noqa: BLE001
                    logger.exception("הסרת קובץ %s מהאינדקס נכשלה", file_id)
                    summary["errors"].append(f"הסרה {file_id}: {str(e)[:200]}")
            logger.info("Delta Sync הסתיים: %s", summary)
            return summary
        finally:
            self._sync_lock.release()

    # ==================================================================
    # אינדוקס קובץ בודד
    # ==================================================================
    def index_file(self, file: Dict[str, Any]) -> Dict[str, Any]:
        """
        מוריד, מפענח, מחלק ומאנדקס קובץ אחד, ומעדכן את טבלת המעקב.

        קודם נמחקים המקטעים הישנים של הקובץ (אם קיימים) כדי שגרסה מעודכנת של
        חוזה לא תשאיר מקטעים מיושנים שיבלבלו את הסוכן.
        """
        local_path: Optional[str] = None
        try:
            local_path = self.drive.download_to_tmp(file)
            result = self.extract(local_path)
            meta = result["metadata"]
            chunks = self.chunk_by_sections(result["markdown"], meta, file)

            if not chunks:
                raise ValueError("לא חולץ טקסט מהקובץ (ייתכן שהוא ריק, מוצפן או פגום)")
            result["markdown"] = None  # הטקסט המלא כבר בתוך המקטעים - משחררים זיכרון

            # מחיקת הגרסה הקודמת רק אחרי שהפיענוח הצליח - כשל בפיענוח לא מוחק אינדקס קיים
            self.vector_db.delete_by_metadata({"file_id": file["id"]})
            # Embeddings + הכנסה במנות קטנות: חוזה של אלפי מקטעים לא נשלח כבקשה אחת
            batch = self.settings.EMBED_BATCH_SIZE
            for i in range(0, len(chunks), batch):
                self.vector_db.insert(content_hash=f"{file['id']}:{file['fingerprint']}",
                                      documents=chunks[i: i + batch], batch_size=batch)

            failed = result["failed_pages"]
            note = f"{len(failed)} עמודים לא פוענחו: {', '.join(map(str, failed[:30]))}" if failed else None
            self._upsert_tracking(file, status="indexed", metadata=meta, chunks=len(chunks),
                                  pages=result["pages"], ocr_pages=result["ocr_pages"],
                                  extraction=result["extraction"], error=note)
            return {"file": file["name"], "chunks": len(chunks), "extraction": result["extraction"],
                    "failed_pages": failed}
        except Exception as e:
            logger.exception("אינדוקס הקובץ %s נכשל", file.get("name"))
            try:
                self._upsert_tracking(file, status="error", error=f"{type(e).__name__}: {e}"[:1000])
            except Exception:  # noqa: BLE001 - כשל ברישום לא מסתיר את השגיאה המקורית
                logger.exception("עדכון טבלת המעקב נכשל עבור %s", file.get("name"))
            raise
        finally:
            # ניקוי זיכרון ודיסק: קריטי ב-Render, שם /tmp והזיכרון מוגבלים
            if local_path and os.path.exists(local_path):
                os.remove(local_path)
            gc.collect()

    def remove_file(self, file_id: str) -> None:
        """מוחק את כל המקטעים של קובץ מ-PgVector ואת הרשומה שלו מטבלת המעקב."""
        self.vector_db.delete_by_metadata({"file_id": file_id})
        with self.engine.begin() as conn:
            conn.execute(delete(self.tracking).where(self.tracking.c.file_id == file_id))

    def _upsert_tracking(self, file: Dict[str, Any], status: str, error: Optional[str],
                         metadata: Optional[dict] = None, chunks: int = 0, pages: int = 0,
                         ocr_pages: int = 0, extraction: Optional[str] = None) -> None:
        """
        מכניס או מעדכן רשומה בטבלת המעקב (INSERT ... ON CONFLICT DO UPDATE).

        במקרה של שגיאה נשמרת טביעת האצבע אבל status=error, כך שהסנכרון הבא ינסה שוב.
        """
        values = {
            "file_id": file["id"], "name": file.get("name", ""), "mime_type": file.get("mimeType"),
            "fingerprint": file.get("fingerprint"), "status": status, "error": error,
            "web_link": file.get("webViewLink"), "indexed_at": datetime.now(timezone.utc),
        }
        if status == "indexed":
            values.update(metadata=metadata, chunks=chunks, pages=pages,
                          ocr_pages=ocr_pages, extraction=extraction, retries=0)
        else:
            values["retries"] = 1
        stmt = pg_insert(self.tracking).values(**values)
        set_ = {k: stmt.excluded[k] for k in values if k != "file_id"}
        if status == "error":
            # כשל נוסף על אותה גרסה -> מונה +1; גרסה חדשה של הקובץ -> מתחילים מ-1
            set_["retries"] = case(
                (self.tracking.c.fingerprint == stmt.excluded.fingerprint,
                 self.tracking.c.retries + 1),
                else_=1,
            )
        stmt = stmt.on_conflict_do_update(index_elements=[self.tracking.c.file_id], set_=set_)
        with self.engine.begin() as conn:
            conn.execute(stmt)

    # ==================================================================
    # פיענוח: טקסט דיגיטלי או OCR
    # ==================================================================
    def extract(self, path: str) -> Dict[str, Any]:
        """
        מפענח PDF או תמונה ומחזיר Markdown מחולק לסעיפים + מטא-דאטה.

        ההחלטה מתבצעת לכל עמוד בנפרד:
            - עמוד עם 50 תווים או יותר של טקסט דיגיטלי -> שימוש בטקסט (חינם).
            - עמוד עם פחות מ-50 תווים (סרוק) או קובץ תמונה -> המרה לתמונה ב-150 DPI
              ושליחה ל-gpt-4o Vision עם Structured Outputs.
        כך חוזה דיגיטלי עם נספח סרוק אחד משלם OCR רק על הנספח.

        fitz.open פותח גם קבצי תמונה (PNG/JPG/TIFF) כמסמך בן עמוד אחד ללא טקסט,
        ולכן הם עוברים אוטומטית במסלול ה-OCR - אין צורך בקוד נפרד לתמונות.
        """
        parts: List[str] = []
        merged = ContractMetadata()
        ocr_pages = 0
        digital_pages = 0
        failed_pages: List[int] = []
        # טקסט עמודי המפתח (התחלה וסוף) - רק הם נשלחים לחילוץ מטא-דאטה, בכל אורך מסמך
        head_pages: List[str] = []
        tail_pages: List[str] = []
        key = self.settings.METADATA_KEY_PAGES

        with fitz.open(path) as doc:
            page_count = doc.page_count
            for i in range(page_count):
                page_no = i + 1
                try:
                    page = doc.load_page(i)
                    page_text = page.get_text("text") if doc.is_pdf else ""
                    if len(page_text.strip()) >= self.settings.MIN_CHARS_PER_PAGE:
                        md = self._digital_text_to_markdown(page_text)
                        digital_pages += 1
                    else:
                        page_result = self._ocr_page(page, page_no, page_count)
                        md = page_result.markdown
                        self._merge_metadata(merged, page_result.metadata)
                        ocr_pages += 1
                    del page  # שחרור משאבי העמוד מיד
                except _FATAL_OPENAI_ERRORS:
                    raise  # בעיה כללית (עומס/הרשאה) - הקובץ כולו ינוסה שוב בסנכרון הבא
                except Exception as e:  # noqa: BLE001 - עמוד בעייתי אחד לא מפיל חוזה של מאות עמודים
                    logger.warning("עמוד %s/%s לא פוענח: %s", page_no, page_count, e)
                    failed_pages.append(page_no)
                    md = f"[עמוד {page_no}: לא ניתן היה לפענח את העמוד]"
                parts.append(md)
                if i < key:
                    head_pages.append(md)
                elif i >= page_count - key:
                    tail_pages.append(md)

        if failed_pages and len(failed_pages) == page_count:
            raise RuntimeError(f"אף עמוד לא פוענח ({page_count} עמודים)")

        markdown = "\n\n".join(p for p in parts if p.strip())
        del parts

        # בעמודים דיגיטליים לא התקבל מטא-דאטה מה-OCR, לכן קריאה אחת זולה על עמודי המפתח בלבד
        if digital_pages:
            self._merge_metadata(merged, self._extract_metadata_from_text(head_pages, tail_pages))

        extraction = "ocr" if not digital_pages else ("digital" if not ocr_pages else "mixed")
        return {
            "markdown": markdown,
            "metadata": merged.model_dump(),
            "pages": page_count,
            "ocr_pages": ocr_pages,
            "extraction": extraction,
            "failed_pages": failed_pages,
        }

    def _ocr_page(self, page: "fitz.Page", page_no: int, total: int,
                  clip: Optional["fitz.Rect"] = None, depth: int = 0) -> PageExtraction:
        """
        ממיר עמוד (או רצועה ממנו) לתמונת PNG ושולח ל-Vision עם Structured Outputs.

        - 150 DPI: מספיק לקריאת עברית מודפסת, וקטן משמעותית מ-300 DPI.
        - max_tokens=OCR_MAX_OUTPUT_TOKENS: תקרה לכל קריאה. בלי תקרה, עמוד שבו המודל
          "נתקע בלולאה" (חוזר על שורות בטבלה) שורף ~16K טוקני פלט ונכשל בכל מקרה.
        - עמוד צפוף שחורג מהתקרה (LengthFinishReasonError - "length limit was reached")
          מפוצל לחצי עליון ותחתון (עם חפיפה קטנה כדי לא לחתוך שורה), כל חצי מפוענח
          בנפרד והתוצאות מתאחדות - עד OCR_MAX_SPLIT_DEPTH רמות (עד 4 רצועות).
        - ה-Pixmap וה-Bytes משוחררים מיד אחרי הקידוד ל-Base64.
        """
        pix = page.get_pixmap(dpi=self.settings.OCR_DPI, clip=clip)
        img_b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
        del pix

        part = "" if clip is None else " (רצועה מתוך העמוד - תמלל רק את מה שמופיע בתמונה)"
        try:
            completion = self.openai.chat.completions.parse(
                model=self.settings.VISION_MODEL,
                temperature=0,
                max_tokens=self.settings.OCR_MAX_OUTPUT_TOKENS,
                response_format=PageExtraction,
                messages=[
                    {"role": "system", "content": OCR_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"עמוד {page_no} מתוך {total}.{part}"},
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/png;base64,{img_b64}", "detail": "high"}},
                        ],
                    },
                ],
            )
        except LengthFinishReasonError:
            del img_b64
            if depth >= self.settings.OCR_MAX_SPLIT_DEPTH:
                raise ValueError(f"עמוד {page_no} ארוך מדי לפענוח גם אחרי פיצול")
            logger.info("עמוד %s חרג ממגבלת הפלט - מפצל לרצועות (רמה %s)", page_no, depth + 1)
            rect = clip or page.rect
            mid = rect.y0 + rect.height / 2
            overlap = rect.height * 0.03
            halves = [fitz.Rect(rect.x0, rect.y0, rect.x1, mid + overlap),
                      fitz.Rect(rect.x0, mid - overlap, rect.x1, rect.y1)]
            results = [self._ocr_page(page, page_no, total, clip=h, depth=depth + 1) for h in halves]
            merged = ContractMetadata()
            for r in results:
                self._merge_metadata(merged, r.metadata)
            return PageExtraction(markdown="\n\n".join(r.markdown for r in results), metadata=merged)
        except ContentFilterFinishReasonError:
            raise ValueError(f"עמוד {page_no} נחסם ע\"י מסנן התוכן של OpenAI")
        del img_b64
        parsed = completion.choices[0].message.parsed
        return parsed or PageExtraction(markdown="", metadata=ContractMetadata())

    def _extract_metadata_from_text(self, head_pages: List[str], tail_pages: List[str]) -> ContractMetadata:
        """
        מחלץ מטא-דאטה מעמודי המפתח בלבד, עם מודל זול (gpt-4o-mini).

        - נשלחים רק METADATA_KEY_PAGES העמודים הראשונים (צדדים, נכס, תקופה) והאחרונים
          (חתימות, תאריכים, נספח כספי) - לא כל החוזה. גודל הבקשה קבוע, גם לחוזה של
          אלף עמודים, ולעולם לא חורג ממגבלת ההקשר.
        - גם בתוך עמודי המפתח יש תקרת תווים (METADATA_SAMPLE_CHARS) למקרה של עמודים צפופים.
        - max_tokens=METADATA_MAX_OUTPUT_TOKENS; חריגה (LengthFinishReasonError) -> ניסיון
          נוסף על דגימה קטנה בחצי, ואם גם הוא נכשל - חוזה בלי מטא-דאטה (עדיף על חוזה שלא אונדקס).
        """
        limit = self.settings.METADATA_SAMPLE_CHARS
        head = "\n\n".join(head_pages)
        tail = "\n\n".join(tail_pages)
        for budget in (limit, limit // 2):
            sample = head[: int(budget * 0.7)]
            if tail:
                sample += "\n...\n" + tail[-int(budget * 0.3):]
            try:
                completion = self.openai.chat.completions.parse(
                    model=self.settings.METADATA_MODEL,
                    temperature=0,
                    max_tokens=self.settings.METADATA_MAX_OUTPUT_TOKENS,
                    response_format=ContractMetadata,
                    messages=[
                        {"role": "system", "content": METADATA_SYSTEM_PROMPT},
                        {"role": "user", "content": sample},
                    ],
                )
                return completion.choices[0].message.parsed or ContractMetadata()
            except LengthFinishReasonError:
                logger.warning("חילוץ מטא-דאטה חרג ממגבלת הפלט (דגימה של %s תווים) - מנסה דגימה קטנה יותר", budget)
            except Exception as e:  # noqa: BLE001 - חוזה בלי מטא-דאטה עדיף על חוזה שלא אונדקס
                logger.warning("חילוץ מטא-דאטה נכשל: %s", e)
                break
        return ContractMetadata()

    @staticmethod
    def _merge_metadata(target: ContractMetadata, source: ContractMetadata) -> None:
        """
        ממזג מטא-דאטה של עמוד/קטע לתוך הרשומה המרכזית.

        צדדים מאוחדים ללא כפילויות; לשדות טקסט - הערך הראשון שנמצא נשמר;
        תנאים כספיים משורשרים (הם יכולים להופיע בכמה עמודים).
        """
        for p in source.parties:
            if p and p not in target.parties and len(target.parties) < _MAX_PARTIES:
                target.parties.append(p[:120])
        for field in ("property", "block_parcel", "start_date", "end_date"):
            if not getattr(target, field) and getattr(source, field):
                setattr(target, field, getattr(source, field))
        if source.financial_terms:
            if not target.financial_terms:
                target.financial_terms = source.financial_terms
            elif (source.financial_terms not in target.financial_terms
                  and len(target.financial_terms) < _MAX_FINANCIAL_CHARS):
                target.financial_terms += "; " + source.financial_terms
            target.financial_terms = target.financial_terms[:_MAX_FINANCIAL_CHARS]

    @staticmethod
    def _digital_text_to_markdown(page_text: str) -> str:
        """
        ממיר טקסט דיגיטלי גולמי ל-Markdown עם כותרות '## סעיף X'.

        מזהה שורות שמתחילות במספר סעיף ("5.", "5)", "סעיף 5") והופך אותן לכותרת,
        כך שגם חוזים דיגיטליים מתחלקים לפי סעיפים בדיוק כמו חוזים סרוקים.
        """
        out: List[str] = []
        for line in page_text.splitlines():
            m = _DIGITAL_SECTION_RE.match(line)
            if m:
                num = m.group(1) or m.group(2)
                out.append(f"\n## סעיף {num}")
                rest = line[m.end():].strip()
                if rest:
                    out.append(rest)
            else:
                out.append(line.rstrip())
        return "\n".join(out)

    # ==================================================================
    # חלוקה ל-Chunks + Context Header
    # ==================================================================
    def chunk_by_sections(self, markdown: str, meta: Dict[str, Any], file: Dict[str, Any]) -> List[Document]:
        """
        מחלק את ה-Markdown למקטעים לפי כותרות '## ...' ומזריק Context Header לכל מקטע.

        Context Header לדוגמה:
            [קובץ: חוזה שכירות רחוב הרצל.pdf | צדדים: עיריית X; חברת Y | נכס: גוש 6623 חלקה 12 | תוקף: 2024-01-01 עד 2028-12-31]

        למה זה חוסך טוקנים: מקטע שנשלף בחיפוש "יודע" לאיזה חוזה הוא שייך, כך
        שהסוכן לא צריך שליפות נוספות כדי להבין את ההקשר. השורה גם נכנסת ל-Embedding
        ול-Full-Text, ולכן שאלה כמו "מה דמי השכירות בגוש 6623" מוצאת את המקטע הנכון.

        סעיף ארוך מ-MAX_CHUNK_CHARS מפוצל לתת-מקטעים עם חפיפה, וכל אחד מקבל את
        אותה שורת הקשר.
        """
        header = self._context_header(file.get("name", ""), meta)
        sections: List[tuple[str, str]] = []

        matches = list(_MD_HEADING_RE.finditer(markdown))
        preamble = markdown[: matches[0].start()] if matches else markdown
        if preamble.strip():
            sections.append(("מבוא", preamble.strip()))
        for idx, m in enumerate(matches):
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
            body = markdown[m.start():end].strip()
            if body:
                # כותרת ארוכה (OCR שהדביק פסקה לכותרת) לא תנפח את המטא-דאטה של כל מקטע
                sections.append((m.group(1).strip()[:_MAX_SECTION_TITLE], body))

        docs: List[Document] = []
        max_len = self.settings.MAX_CHUNK_CHARS
        # חפיפה תמיד קטנה מהמקטע - אחרת הלולאה לא מתקדמת (מקטעים אינסופיים)
        overlap = min(self.settings.CHUNK_OVERLAP, max_len // 4)
        base_meta = {
            "file_id": file["id"],
            "file_name": file.get("name", ""),
            "block_parcel": meta.get("block_parcel"),
            "start_date": meta.get("start_date"),
            "end_date": meta.get("end_date"),
        }
        for section_title, body in sections:
            pieces = [body] if len(body) <= max_len else [
                body[i: i + max_len] for i in range(0, len(body), max_len - overlap)
            ]
            for part_no, piece in enumerate(pieces, start=1):
                docs.append(Document(
                    content=f"{header}\n{piece}",
                    name=f"{file.get('name', '')} | {section_title}",
                    meta_data={**base_meta, "section": section_title, "part": part_no},
                ))
        return docs

    @staticmethod
    def _context_header(filename: str, meta: Dict[str, Any]) -> str:
        """בונה את שורת ההקשר הקצרה (שורה אחת = מעט טוקנים) שמוזרקת בראש כל מקטע."""
        parties = "; ".join(meta.get("parties") or []) or "לא צוין"
        prop = " ".join(x for x in (meta.get("property"), meta.get("block_parcel")) if x) or "לא צוין"
        validity = f"{meta.get('start_date') or '?'} עד {meta.get('end_date') or '?'}"
        header = f"[קובץ: {filename} | צדדים: {parties} | נכס: {prop} | תוקף: {validity}"
        # תקרה קשיחה: השורה נכנסת לכל מקטע ול-Embedding שלו
        return (header[: _MAX_HEADER_CHARS - 2] + "…" if len(header) > _MAX_HEADER_CHARS - 1 else header) + "]"

    # ==================================================================
    # חיפוש ושאילתות
    # ==================================================================
    def search(self, query: str, file_id: Optional[str] = None, limit: Optional[int] = None) -> List[Document]:
        """
        חיפוש היברידי (וקטורי + מילות מפתח) ב-PgVector.

        limit ברירת מחדל = RAG_LIMIT (5): זו התקרה הקשיחה לכמות הטקסט שנכנסת לקונטקסט
        של הסוכן מכל חיפוש. ניתן לצמצם לחוזה אחד באמצעות file_id.
        """
        limit = min(limit or self.settings.RAG_LIMIT, self.settings.RAG_LIMIT)
        filters = {"file_id": file_id} if file_id else None
        return self.vector_db.search(query=query, limit=limit, filters=filters)

    def get_file_chunks(self, file_id: str, max_chars: int) -> str:
        """
        מחזיר את תוכן החוזה (מקטעים לפי הסדר) עד לתקרת תווים.

        משמש לסיכום חוזה שלם; התקרה מונעת שליחת חוזה של 80 עמודים לקונטקסט.
        שורת ההקשר מוסרת מכל מקטע מלבד הראשון - אין טעם לחזור עליה.
        """
        table = self.vector_db.table
        stmt = (select(table.c.content, table.c.meta_data)
                .where(table.c.meta_data.contains({"file_id": file_id})))
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        rows.sort(key=lambda r: (_section_sort_key(r.meta_data.get("section", "")), r.meta_data.get("part", 0)))
        out: List[str] = []
        total = 0
        for i, r in enumerate(rows):
            content = r.content if i == 0 else r.content.split("\n", 1)[-1]
            if total + len(content) > max_chars:
                out.append("...[הטקסט קוצר]")
                break
            out.append(content)
            total += len(content)
        return "\n\n".join(out)

    def list_tracked(self) -> List[Dict[str, Any]]:
        """מחזיר את כל רשומות טבלת המעקב (לממשק ולכלים) - שאילתת SQL בלבד, ללא LLM."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(self.tracking).order_by(self.tracking.c.name)).mappings().all()
        return [dict(r) for r in rows]

    def index_status(self) -> Dict[str, Any]:
        """סיכום מצב האינדקס מטבלת המעקב - SQL בלבד, ללא LLM (משמש את הכלי ואת הדוח בממשק)."""
        rows = self.list_tracked()
        return {
            "total": len(rows),
            "indexed": sum(r["status"] == "indexed" for r in rows),
            "errors": [{"file": r["name"], "error": (r.get("error") or "")[:150],
                        "retries": r.get("retries") or 0}
                       for r in rows if r["status"] == "error"],
            "chunks": sum(r.get("chunks") or 0 for r in rows),
            "pages": sum(r.get("pages") or 0 for r in rows),
            "ocr_pages": sum(r.get("ocr_pages") or 0 for r in rows),
            "last_indexed": max((r["indexed_at"] for r in rows if r.get("indexed_at")), default=None),
        }

    def find_link(self, file_name: str) -> Optional[str]:
        """קישור הדרייב של חוזה לפי שם (לסעיף המקורות במסמכי Word)."""
        f = self.find_file(file_name)
        return f.get("web_link") if f else None

    def find_file(self, name_or_id: str) -> Optional[Dict[str, Any]]:
        """מאתר חוזה לפי מזהה מדויק או חלק משם הקובץ (ILIKE)."""
        with self.engine.connect() as conn:
            row = conn.execute(select(self.tracking).where(self.tracking.c.file_id == name_or_id)).mappings().first()
            if not row:
                row = conn.execute(
                    select(self.tracking).where(self.tracking.c.name.ilike(f"%{name_or_id}%")).limit(1)
                ).mappings().first()
        return dict(row) if row else None

    def expiring_contracts(self, days: int) -> List[Dict[str, Any]]:
        """
        מחזיר חוזים שתאריך הסיום שלהם בטווח של X ימים מהיום (כולל חוזים שפגו).

        מבוסס על המטא-דאטה שבטבלת המעקב - שאילתה מקומית, אפס טוקנים.
        """
        today = date.today()
        horizon = today + timedelta(days=days)
        result = []
        for r in self.list_tracked():
            end = _parse_date((r.get("metadata") or {}).get("end_date"))
            if end and end <= horizon:
                result.append({
                    "name": r["name"],
                    "end_date": end.isoformat(),
                    "days_left": (end - today).days,
                    "parties": (r.get("metadata") or {}).get("parties"),
                    "property": (r.get("metadata") or {}).get("block_parcel") or (r.get("metadata") or {}).get("property"),
                })
        return sorted(result, key=lambda x: x["days_left"])


def _parse_date(value: Optional[str]) -> Optional[date]:
    """ממיר מחרוזת תאריך (YYYY-MM-DD או DD/MM/YYYY) לאובייקט date; מחזיר None אם לא תקין."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _section_sort_key(section: str) -> tuple:
    """מפתח מיון לסעיפים: 'מבוא' ראשון, ואז לפי מספר הסעיף."""
    if section == "מבוא":
        return (0, 0)
    m = re.search(r"\d+", section)
    return (1, int(m.group()) if m else 9999)
