"""
main.py - שרת FastAPI: נתיבי API, נקודת הסנכרון הלילי (Cron) והגשת ממשק ה-Web.

מה הקובץ עושה:
    - מאתחל פעם אחת (ב-lifespan) את השירותים המשותפים: DriveService, RagService,
      DocxService והסוכן.
    - חושף נתיבי API לצ'אט (רגיל ו-Streaming), לדוחות, להיסטוריית שיחה, לניהול קבצים
      בדרייב ולהפקת מסמכי Word.
    - חושף את POST /api/sync, שמוגן ב-X-Sync-Secret ומופעל ע"י ה-Cron הלילי של Render.
    - מגיש את תיקיית public/ (index.html, styles.css, app.js) כאתר סטטי.

איך זה עובד:
    הנתיבים מוגדרים כ-def רגיל (לא async), ולכן FastAPI מריץ אותם ב-Thread Pool.
    זה חשוב כי הסוכן, הדרייב וה-OCR מבצעים קריאות רשת חוסמות - כך הן לא תוקעות
    את לולאת האירועים ושאר המשתמשים ממשיכים לקבל תשובות.
    עבודות ארוכות (סנכרון, אינדוקס קובץ שהועלה) רצות ברקע ב-BackgroundTasks.

תרומה לחיסכון בטוקנים / זיכרון ב-Render:
    - כל השירותים הם Singleton: Engine אחד ל-Postgres, לקוח אחד לדרייב, סוכן אחד.
    - דוחות (חוזים שמסתיימים, תנאים כספיים, סטטוס אינדקס) עונים ישירות מ-SQL -
      0 טוקנים. רק אנליזת "סעיפי סיכון" באמת צריכה LLM.
    - אנליזה רצה ב-session חד-פעמי, כך שפלט הכלים הגדול שלה לא נגרר להמשך השיחה.
    - ייצוא תשובה ל-Word מתבצע מקומית, בלי לבקש מהמודל לכתוב את התוכן שוב.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from agno.run.agent import RunEvent
from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from backend.agent import build_agent
from backend.config import get_settings
from backend.services.docx_service import DocxService
from backend.services.drive_service import SUPPORTED_MIME_TYPES, DriveService, DriveServiceError
from backend.services.rag_service import RagService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("contracts-agent")

settings = get_settings()

# מצב גלובלי של האפליקציה - מאותחל פעם אחת ב-lifespan
STATE: Dict[str, Any] = {"last_sync": None, "indexing": set()}

# אנליזות שבאמת דורשות LLM (שאר הכפתורים עברו ל-/api/reports - SQL בלבד)
ANALYSIS_PROMPTS = {
    "risks": "חפש בחוזים סעיפי סיכון: קנסות, ביטול מוקדם, אופציות הארכה ושיפוי. ציין חוזה וסעיף לכל ממצא.",
}

# טקסט חיווי שמוצג בממשק בזמן שהסוכן מפעיל כלי
TOOL_STATUS = {
    "search_contracts": "🔍 מחפש בחוזים…",
    "get_contract_metadata": "📄 שולף פרטי חוזה…",
    "list_expiring_contracts": "⏳ בודק תאריכי סיום…",
    "summarize_contract": "📖 קורא את החוזה…",
    "compare_contracts": "⚖ משווה חוזים…",
    "get_index_status": "◉ בודק סטטוס אינדקס…",
    "prepare_document": "📝 מכין טיוטת מסמך…",
    "generate_word_document": "📝 מפיק מסמך Word…",
}

_FOCUS_RE = re.compile(r"^\[מיקוד: [^\]]+\]\n")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    מאתחל את השירותים פעם אחת בעליית השרת.

    יצירת החיבורים (Postgres, Drive, OpenAI) כאן ולא בכל בקשה חוסכת זמן תגובה
    וזיכרון. ה-RagService גם יוצר בעלייה את הטבלאות וה-Extension vector אם חסרים.
    """
    drive = DriveService(settings)
    rag = RagService(settings, drive)
    docx = DocxService(settings, drive, link_resolver=rag.find_link)
    STATE.update(drive=drive, rag=rag, docx=docx, agent=build_agent(settings, rag, docx))
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
    """
    בקשת צ'אט: הודעת המשתמש + מזהה השיחה (נשמר ב-localStorage בדפדפן).

    focus_file - שם חוזה שהמשתמש "נעץ" בממשק; החיפוש יצטמצם אליו.
    analysis   - מפתח אנליזה מוכנה (במקום message); רצה ב-session חד-פעמי.
    """

    message: str = Field("", max_length=4000)  # תקרה למניעת הדבקות ענק יקרות
    session_id: Optional[str] = None
    focus_file: Optional[str] = Field(None, max_length=300)
    analysis: Optional[str] = None

    @model_validator(mode="after")
    def _needs_input(self):
        if not self.message.strip() and not self.analysis:
            raise ValueError("נדרשת הודעה")
        return self


class DocRequest(BaseModel):
    """ייצוא תשובה קיימת ל-Word (ללא LLM)."""

    markdown: str = Field(..., min_length=1, max_length=60000)
    title: str = Field("תשובת סוכן החוזים", max_length=200)


# ======================================================================
# צ'אט
# ======================================================================
def _prepare_run(req: ChatRequest) -> tuple[str, str, str]:
    """מחזיר (הודעה למודל, session_id להרצה, session_id להחזרה ללקוח)."""
    client_session = req.session_id or uuid.uuid4().hex
    if req.analysis:
        prompt = ANALYSIS_PROMPTS.get(req.analysis)
        if not prompt:
            raise HTTPException(404, "סוג אנליזה לא מוכר")
        # session חד-פעמי: פלט הכלים של האנליזה לא מנפח את היסטוריית השיחה הראשית
        return prompt, f"analysis-{uuid.uuid4().hex}", client_session
    message = req.message.strip()
    if req.focus_file:
        message = f"[מיקוד: {req.focus_file}]\n{message}"
    return message, client_session, client_session


def _usage(metrics: Any) -> Dict[str, Optional[int]]:
    """טוקנים כוללים + טוקנים שהגיעו מה-Prompt Cache של OpenAI (מחיר מוזל)."""
    if not metrics:
        return {"tokens": None, "cached_tokens": None}
    return {"tokens": getattr(metrics, "total_tokens", None),
            "cached_tokens": getattr(metrics, "cache_read_tokens", None) or 0}


def _sources(tools: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """
    מחלץ את המקורות (קובץ + סעיפים) מתוצאות search_contracts / compare_contracts
    של הריצה, ומצרף קישור לדרייב מטבלת המעקב - בלי לבקש מהמודל להחזיר אותם.
    """
    found: Dict[str, List[str]] = {}
    for t in tools or []:
        if getattr(t, "tool_name", None) not in ("search_contracts", "compare_contracts") or not t.result:
            continue
        for chunk in str(t.result).split("\n\n"):
            try:
                data = json.loads(chunk)
            except (json.JSONDecodeError, TypeError):
                continue
            for g in data if isinstance(data, list) else [data]:
                if not isinstance(g, dict) or not g.get("file"):
                    continue
                secs = found.setdefault(g["file"], [])
                for s in g.get("sections") or []:
                    if s.get("section") and s["section"] not in secs:
                        secs.append(s["section"])
    if not found:
        return []
    links = {r["name"]: r.get("web_link") for r in STATE["rag"].list_tracked()}
    return [{"file": f, "sections": secs[:6], "link": links.get(f)} for f, secs in found.items()]


@app.post("/api/chat")
def chat(req: ChatRequest) -> Dict[str, Any]:
    """שולח הודעה לסוכן ומחזיר את התשובה המלאה (חלופה לא-Streaming)."""
    message, run_session, client_session = _prepare_run(req)
    response = STATE["agent"].run(message, session_id=run_session)
    usage = _usage(getattr(response, "metrics", None))
    logger.info("chat tokens=%s cached=%s", usage["tokens"], usage["cached_tokens"])
    return {
        "answer": response.content or "",
        "session_id": client_session,
        "sources": _sources(getattr(response, "tools", None)),
        **usage,
    }


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """
    צ'אט ב-Streaming (NDJSON - אירוע JSON בכל שורה):
        {"type": "status", "text": "🔍 מחפש בחוזים…"}  - כשהסוכן מפעיל כלי
        {"type": "delta",  "text": "..."}               - קטע מהתשובה
        {"type": "done",   "tokens", "cached_tokens", "sources", "session_id"}
        {"type": "error",  "text": "..."}
    המשתמש רואה התקדמות מיד, במקום להמתין לתשובה המלאה.
    """
    message, run_session, client_session = _prepare_run(req)

    def line(obj: Dict[str, Any]) -> str:
        return json.dumps(obj, ensure_ascii=False, default=str) + "\n"

    def events() -> Iterator[str]:
        tools: List[Any] = []
        try:
            stream = STATE["agent"].run(message, session_id=run_session, stream=True, stream_events=True)
            for ev in stream:
                kind = getattr(ev, "event", "")
                if kind == RunEvent.run_content.value and isinstance(getattr(ev, "content", None), str):
                    yield line({"type": "delta", "text": ev.content})
                elif kind == RunEvent.tool_call_started.value:
                    name = getattr(getattr(ev, "tool", None), "tool_name", "") or ""
                    yield line({"type": "status", "text": TOOL_STATUS.get(name, "⚙ מעבד…")})
                elif kind == RunEvent.tool_call_completed.value and getattr(ev, "tool", None):
                    tools.append(ev.tool)
                elif kind == RunEvent.run_completed.value:
                    usage = _usage(getattr(ev, "metrics", None))
                    logger.info("chat(stream) tokens=%s cached=%s", usage["tokens"], usage["cached_tokens"])
                    yield line({"type": "done", "session_id": client_session,
                                "sources": _sources(tools), **usage})
                elif kind == RunEvent.run_error.value:
                    yield line({"type": "error", "text": str(getattr(ev, "content", "") or "שגיאה בהרצת הסוכן")})
        except Exception as e:  # noqa: BLE001 - השגיאה נשלחת ללקוח כאירוע ולא שוברת את הזרם
            logger.exception("Streaming נכשל")
            yield line({"type": "error", "text": str(e)[:300]})

    # X-Accel-Buffering: מונע מ-Proxy (Render/Nginx) לאגור את הזרם עד הסוף
    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/analysis/{kind}")
def analysis(kind: str, session_id: Optional[str] = None) -> Dict[str, Any]:
    """מריץ אנליזת LLM מוגדרת מראש (כרגע: risks) ב-session חד-פעמי."""
    return chat(ChatRequest(analysis=kind, session_id=session_id))


@app.get("/api/sessions/{session_id}/messages")
def session_messages(session_id: str) -> Dict[str, Any]:
    """
    מחזיר את הודעות השיחה (משתמש/סוכן בלבד, בלי קריאות כלים) לשחזור המסך אחרי רענון.
    קריאה מ-Postgres בלבד - לא עולה טוקנים.
    """
    try:
        msgs = STATE["agent"].get_session_messages(session_id=session_id, skip_roles=["system", "tool"])
    except Exception:  # noqa: BLE001 - שיחה שעוד לא נשמרה = רשימה ריקה
        return {"messages": []}
    out = []
    for m in msgs:
        content = m.content if isinstance(m.content, str) else ""
        if m.role not in ("user", "assistant") or not content.strip():
            continue
        focus = None
        if m.role == "user" and (fm := _FOCUS_RE.match(content)):
            focus = fm.group(0)[len("[מיקוד: "):-2]
            content = content[fm.end():]
        out.append({"role": m.role, "content": content, "focus": focus})
    return {"messages": out[-60:]}


# ======================================================================
# דוחות - SQL ישיר, 0 טוקנים
# ======================================================================
@app.get("/api/reports/{kind}")
def report(kind: str, days: int = 90) -> Dict[str, Any]:
    """
    דוחות מובנים לכפתורי הפעולה המהירה. מחזיר טבלה גנרית {title, columns, rows, note}
    שהממשק מצייר בתוך הצ'אט - בלי LLM.
    """
    rag: RagService = STATE["rag"]
    if kind == "expiring":
        items = rag.expiring_contracts(days)
        return {
            "title": f"חוזים שמסתיימים ב-{days} הימים הקרובים או שפגו",
            "columns": ["חוזה", "צדדים", "נכס", "תאריך סיום", "ימים שנותרו"],
            "rows": [[i["name"], "; ".join(i.get("parties") or []), i.get("property") or "",
                      i["end_date"], i["days_left"]] for i in items],
            "note": None if items else "אין חוזים שמסתיימים בטווח הזה.",
        }
    if kind == "financial":
        rows = []
        for r in rag.list_tracked():
            m = r.get("metadata") or {}
            if r["status"] == "indexed":
                rows.append([r["name"], "; ".join(m.get("parties") or []),
                             m.get("block_parcel") or m.get("property") or "",
                             m.get("financial_terms") or "לא חולץ"])
        return {"title": "תנאים כספיים עיקריים (מתוך המטא-דאטה)",
                "columns": ["חוזה", "צדדים", "נכס", "תנאים כספיים"], "rows": rows,
                "note": None if rows else "אין חוזים מאונדקסים."}
    if kind == "status":
        s = rag.index_status()
        rows = [["סה\"כ חוזים", s["total"]], ["אונדקסו", s["indexed"]], ["נכשלו", len(s["errors"])],
                ["מקטעים", s["chunks"]], ["עמודים", s["pages"]], ["עמודים שעברו OCR", s["ocr_pages"]],
                ["אינדוקס אחרון", s["last_indexed"].strftime("%d/%m/%Y %H:%M") if s["last_indexed"] else "-"]]
        rows += [[f"⚠ {e['file']}", f"{e['error']} (ניסיונות: {e['retries']})"] for e in s["errors"]]
        return {"title": "סטטוס מאגר החוזים", "columns": ["מדד", "ערך"], "rows": rows, "note": None}
    raise HTTPException(404, "סוג דוח לא מוכר")


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
            "retries": (t.get("retries") or 0) if t else 0,
            "metadata": t.get("metadata") if t else None,
            "extraction": t.get("extraction") if t else None,
            "pages": t.get("pages") if t else None,
        })
    return {"files": files, "last_sync": STATE["last_sync"], "max_retries": settings.MAX_INDEX_RETRIES}


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


@app.post("/api/files/{file_id}/reindex", status_code=202)
def reindex_file(file_id: str, background: BackgroundTasks) -> Dict[str, str]:
    """ניסיון חוזר ידני לקובץ שנכשל (גם אחרי שהסנכרון הלילי הפסיק לנסות)."""
    file = STATE["drive"].get_file(file_id)
    STATE["indexing"].add(file_id)
    background.add_task(_index_in_background, file)
    return {"status": "processing"}


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
# מסמכי Word
# ======================================================================
@app.post("/api/docs")
def export_doc(req: DocRequest) -> Dict[str, Any]:
    """ממיר תשובה קיימת (Markdown) למסמך Word מהתבנית המוסדית - ללא LLM."""
    return STATE["docx"].build(req.title, req.markdown)


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


if __name__ == "__main__":
    # הרצה ישירה (python -m backend.main): הפורט נלקח מ-$PORT של Render, ברירת מחדל 8000 לפיתוח מקומי
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
