"""
main.py - שרת FastAPI: נתיבי API, נקודת הסנכרון הלילי (Cron) והגשת ממשק ה-Web.

מה הקובץ עושה:
    - מאתחל פעם אחת (ב-lifespan) את השירותים המשותפים: DriveService, RagService והסוכן.
    - חושף נתיבי API לצ'אט, לניהול קבצים בדרייב, לכפתורי אנליזה ולהורדת מסמכי Word.
    - חושף את POST /api/sync, שמוגן ב-X-Sync-Secret ומופעל ע"י ה-Cron הלילי של Render.
    - מגיש את תיקיית public/ (index.html, styles.css, app.js) כאתר סטטי.

איך זה עובד:
    הנתיבים מוגדרים כ-def רגיל (לא async), ולכן FastAPI מריץ אותם ב-Thread Pool.
    זה חשוב כי הסוכן, הדרייב וה-OCR מבצעים קריאות רשת חוסמות - כך הן לא תוקעות
    את לולאת האירועים ושאר המשתמשים ממשיכים לקבל תשובות.
    עבודות ארוכות (סנכרון, אינדוקס קובץ שהועלה) רצות ברקע ב-BackgroundTasks.

תרומה לחיסכון בטוקנים / זיכרון ב-Render:
    - כל השירותים הם Singleton: Engine אחד ל-Postgres, לקוח אחד לדרייב, סוכן אחד.
    - העלאת קובץ נכתבת ל-/tmp ב-Chunks של 1MB (לא נטענת כולה לזיכרון), ומאונדקסת
      מיד - כך שהסנכרון הלילי ידלג עליה (Delta Sync).
    - כפתורי האנליזה שולחים פרומפטים קבועים וקצרים שמכוונים את הסוכן לכלי הזול
      ביותר (למשל SQL על טבלת המעקב במקום RAG).
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.agent import build_agent
from backend.config import get_settings
from backend.services.drive_service import SUPPORTED_MIME_TYPES, DriveService, DriveServiceError
from backend.services.rag_service import RagService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("contracts-agent")

settings = get_settings()

# מצב גלובלי של האפליקציה - מאותחל פעם אחת ב-lifespan
STATE: Dict[str, Any] = {"last_sync": None, "indexing": set()}

# פרומפטים קבועים לכפתורי האנליזה בממשק - קצרים ומכוונים לכלי הנכון
ANALYSIS_PROMPTS = {
    "expiring": "אילו חוזים מסתיימים ב-90 הימים הקרובים או כבר פגו? הצג טבלה: חוזה, צדדים, נכס, תאריך סיום, ימים שנותרו.",
    "financial": "סכם בטבלה את התנאים הכספיים העיקריים של כל החוזים (דמי שכירות/תמורה, הצמדה, ערבויות), על סמך המטא-דאטה.",
    "status": "מה סטטוס האינדוקס של מאגר החוזים? כמה חוזים אונדקסו, אילו נכשלו ומדוע.",
    "risks": "חפש בחוזים סעיפי סיכון: קנסות, ביטול מוקדם, אופציות הארכה ושיפוי. ציין חוזה וסעיף לכל ממצא.",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    מאתחל את השירותים פעם אחת בעליית השרת.

    יצירת החיבורים (Postgres, Drive, OpenAI) כאן ולא בכל בקשה חוסכת זמן תגובה
    וזיכרון. ה-RagService גם יוצר בעלייה את הטבלאות וה-Extension vector אם חסרים.
    """
    drive = DriveService(settings)
    rag = RagService(settings, drive)
    STATE.update(drive=drive, rag=rag, agent=build_agent(settings, rag, drive))
    logger.info("השירותים אותחלו בהצלחה")
    yield
    rag.engine.dispose()


app = FastAPI(title="סוכן חוזים - יחידת נכסים", lifespan=lifespan)


@app.exception_handler(DriveServiceError)
async def drive_error_handler(_, exc: DriveServiceError):
    """מחזיר שגיאות דרייב כהודעה ברורה בעברית (502) במקום Stack Trace."""
    return JSONResponse(status_code=502, content={"detail": str(exc)})


# ======================================================================
# מודלים של בקשות
# ======================================================================
class ChatRequest(BaseModel):
    """בקשת צ'אט: הודעת המשתמש + מזהה השיחה (נשמר ב-localStorage בדפדפן)."""

    message: str = Field(..., min_length=1, max_length=4000)  # תקרה למניעת הדבקות ענק יקרות
    session_id: Optional[str] = None


# ======================================================================
# צ'אט
# ======================================================================
@app.post("/api/chat")
def chat(req: ChatRequest) -> Dict[str, Any]:
    """
    שולח הודעה לסוכן ומחזיר את התשובה.

    session_id קובע איזו היסטוריה תיטען מ-Postgres (רק 4 הסבבים האחרונים).
    """
    session_id = req.session_id or uuid.uuid4().hex
    response = STATE["agent"].run(req.message, session_id=session_id)
    metrics = getattr(response, "metrics", None)
    return {
        "answer": response.content or "",
        "session_id": session_id,
        "tokens": getattr(metrics, "total_tokens", None) if metrics else None,
    }


@app.post("/api/analysis/{kind}")
def analysis(kind: str, session_id: Optional[str] = None) -> Dict[str, Any]:
    """מריץ אחד מכפתורי האנליזה המוגדרים מראש (expiring / financial / status / risks)."""
    prompt = ANALYSIS_PROMPTS.get(kind)
    if not prompt:
        raise HTTPException(404, "סוג אנליזה לא מוכר")
    return chat(ChatRequest(message=prompt, session_id=session_id))


# ======================================================================
# ניהול קבצים בדרייב
# ======================================================================
@app.get("/api/files")
def list_files() -> Dict[str, Any]:
    """
    מחזיר את רשימת הקבצים בדרייב, ממוזגת עם סטטוס האינדוקס והמטא-דאטה מטבלת המעקב.

    קובץ שה-fingerprint שלו בדרייב שונה מזה שבטבלה מסומן "outdated" - הסנכרון הבא יעבד אותו.
    """
    rag: RagService = STATE["rag"]
    tracked = {r["file_id"]: r for r in rag.list_tracked()}
    files = []
    for f in STATE["drive"].list_files():
        t = tracked.get(f["id"])
        if f["id"] in STATE["indexing"]:
            status = "processing"
        elif not t:
            status = "pending"
        elif t["fingerprint"] != f["fingerprint"]:
            status = "outdated"
        else:
            status = t["status"]
        files.append({
            "id": f["id"],
            "name": f["name"],
            "mime_type": f.get("mimeType"),
            "size": int(f["size"]) if f.get("size") else None,
            "modified": f.get("modifiedTime"),
            "link": f.get("webViewLink"),
            "status": status,
            "error": t.get("error") if t else None,
            "metadata": t.get("metadata") if t else None,
            "extraction": t.get("extraction") if t else None,
        })
    return {"files": files, "last_sync": STATE["last_sync"]}


@app.post("/api/files/upload")
def upload_file(background: BackgroundTasks, file: UploadFile = File(...)) -> Dict[str, Any]:
    """
    מעלה קובץ לתיקיית החוזים בדרייב ומאנדקס אותו ברקע.

    שלבים: כתיבה ל-/tmp ב-Chunks -> העלאה מתחדשת (Resumable) לדרייב -> מחיקת הקובץ
    הזמני -> אינדוקס ברקע (הורדה, פיענוח/OCR, Embeddings). התגובה חוזרת מיד,
    והממשק מציג את הקובץ בסטטוס "processing" עד לסיום.
    """
    mime = file.content_type or "application/octet-stream"
    if mime not in SUPPORTED_MIME_TYPES:
        raise HTTPException(400, "סוג קובץ לא נתמך. ניתן להעלות PDF או תמונה (PNG/JPG/TIFF/WEBP).")

    ext = Path(file.filename or "").suffix.lower()
    tmp_path = os.path.join(settings.TMP_DIR, f"upload_{uuid.uuid4().hex}{ext}")
    try:
        with open(tmp_path, "wb") as out:
            while chunk := file.file.read(1024 * 1024):  # 1MB בכל פעם - לא טוענים את כל הקובץ ל-RAM
                out.write(chunk)
        uploaded = STATE["drive"].upload_file(tmp_path, file.filename or "contract", mime_type=mime)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    STATE["indexing"].add(uploaded["id"])
    background.add_task(_index_in_background, uploaded)
    return {"id": uploaded["id"], "name": uploaded["name"], "status": "processing"}


def _index_in_background(file: Dict[str, Any]) -> None:
    """מאנדקס קובץ שהועלה; שגיאה נרשמת בטבלת המעקב ומוצגת בממשק."""
    try:
        STATE["rag"].index_file(file)
    except Exception:  # noqa: BLE001 - השגיאה כבר נשמרה בטבלת המעקב
        logger.exception("אינדוקס ברקע נכשל עבור %s", file.get("name"))
    finally:
        STATE["indexing"].discard(file["id"])


@app.delete("/api/files/{file_id}")
def delete_file(file_id: str) -> Dict[str, str]:
    """מעביר קובץ לאשפה בדרייב ומוחק את כל המקטעים והרשומה שלו מהאינדקס."""
    STATE["drive"].delete_file(file_id)
    STATE["rag"].remove_file(file_id)
    return {"status": "deleted"}


# ======================================================================
# סנכרון (Cron לילי + רענון ידני מהממשק)
# ======================================================================
def _run_sync() -> None:
    """מריץ Delta Sync ושומר את הסיכום האחרון כדי שהממשק יוכל להציג אותו."""
    try:
        summary = STATE["rag"].delta_sync()
    except Exception as e:  # noqa: BLE001
        logger.exception("הסנכרון נכשל")
        summary = {"status": "error", "message": str(e)[:300]}
    if summary.get("status") != "busy":
        STATE["last_sync"] = {**summary, "finished_at": datetime.now(timezone.utc).isoformat()}


@app.post("/api/sync", status_code=202)
def nightly_sync(background: BackgroundTasks, x_sync_secret: str = Header(default="")) -> Dict[str, str]:
    """
    נקודת הקצה של ה-Cron הלילי ב-Render.

    מוגנת בכותרת X-Sync-Secret (השוואה בזמן קבוע - secrets.compare_digest).
    מחזירה 202 מיד והסנכרון רץ ברקע, כך שה-Cron Job מסתיים בתוך שניות ולא
    מחזיק Instance נוסף פעיל (חיסכון בעלות ובזיכרון).
    """
    if not secrets.compare_digest(x_sync_secret, settings.SYNC_JOB_SECRET):
        raise HTTPException(401, "סוד סנכרון שגוי")
    background.add_task(_run_sync)
    return {"status": "started"}


@app.post("/api/files/refresh", status_code=202)
def manual_refresh(background: BackgroundTasks) -> Dict[str, str]:
    """
    רענון ידני מהממשק (כפתור "סנכרון"). אותו Delta Sync בדיוק - קבצים שלא השתנו
    לא עולים טוקנים, ונעילה פנימית מונעת הרצה כפולה במקביל ל-Cron.
    """
    background.add_task(_run_sync)
    return {"status": "started"}


@app.get("/api/sync/status")
def sync_status() -> Dict[str, Any]:
    """מחזיר את סיכום הסנכרון האחרון (או None אם עוד לא רץ מאז עליית השרת)."""
    return {"last_sync": STATE["last_sync"]}


# ======================================================================
# מסמכי Word שנוצרו
# ======================================================================
@app.get("/api/docs/{name}")
def download_doc(name: str):
    """
    מוריד מסמך Word מתיקיית generated_docs.

    שם הקובץ מאומת (ללא תווי נתיב) כדי למנוע Path Traversal מחוץ לתיקייה.
    """
    if not re.fullmatch(r"[\w֐-׿\-. ]+\.docx", name) or ".." in name:
        raise HTTPException(400, "שם קובץ לא תקין")
    path = Path(settings.GENERATED_DOCS_DIR) / name
    if not path.is_file():
        raise HTTPException(404, "המסמך לא נמצא (ייתכן שהשרת אותחל מחדש - בדוק בתיקיית generated בדרייב)")
    return FileResponse(
        path, filename=name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@app.get("/health")
def health() -> Dict[str, str]:
    """בדיקת חיים עבור Render (healthCheckPath)."""
    return {"status": "ok"}


# הגשת ממשק ה-Web - חייב להיות אחרון, כדי לא "לבלוע" את נתיבי ה-API
app.mount("/", StaticFiles(directory=settings.PUBLIC_DIR, html=True), name="public")
