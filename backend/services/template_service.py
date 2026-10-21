"""
template_service.py - מילוי תבניות Word מהדרייב עם docxtpl.

מה הקובץ עושה:
    - מאתר את תבניות ה-Word בתיקיית התבניות בדרייב (DRIVE_TEMPLATES_FOLDER_ID)
      ומזהה בכל אחת את המשתנים שהיא דורשת ({{ tenant_name }}, {%tr for p in payments %}).
    - ממלא תבנית בנתונים (context) שהסוכן אסף מהחוזים ומהשיחה, שומר את המסמך
      ב-generated_docs, מגבה לדרייב ומחזיר קישור להורדה.

איך זה עובד:
    docxtpl פותח את קובץ ה-docx כתבנית Jinja2: העיצוב, הלוגו, הכותרות וה-RTL של
    התבנית נשמרים בדיוק כפי שעוצבו ב-Word - רק המשתנים מוחלפים.

בטיחות וקבצים:
    - מותר למלא רק קבצים שנמצאים בתיקיית התבניות - הסוכן לא יכול להוריד קובץ
      שרירותי מהדרייב.
    - autoescape=True: ערך כמו "כהן & בניו" או "<" לא שובר את ה-XML של המסמך.
    - קובץ התבנית שהורד ל-/tmp נמחק תמיד (finally), וגם פלט חלקי שנכשל בשמירה.

תרומה לחיסכון בטוקנים:
    - רשימת התבניות והמשתנים נשמרת בזיכרון; תבנית שלא השתנתה (אותו fingerprint)
      לא מורדת שוב.
    - כשחסרים משתנים, הכלי מחזיר את רשימתם בלי לייצר מסמך - הסוכן שואל עליהם
      בהודעה אחת, במקום לייצר מסמך חלקי ולתקן אותו בסבבים נוספים.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from docxtpl import DocxTemplate
from jinja2 import TemplateError

from backend.config import Settings
from backend.services.docx_service import output_path, publish
from backend.services.drive_service import DriveService

logger = logging.getLogger(__name__)

# משתנים שהמערכת ממלאת בעצמה - לא נחשבים "חסרים"
AUTO_VARIABLES = {"today"}


class TemplateService:
    """שירות יחיד לתבניות Word מהדרייב (נוצר פעם אחת בעליית השרת)."""

    def __init__(self, settings: Settings, drive: DriveService):
        self.settings = settings
        self.drive = drive
        self._lock = threading.Lock()
        self._list_cache: Optional[Tuple[float, List[Dict[str, Any]]]] = None
        # file_id -> (fingerprint, variables, error) - כדי לא להוריד שוב תבנית שלא השתנתה
        self._vars_cache: Dict[str, Tuple[str, List[str], Optional[str]]] = {}

    # ------------------------------------------------------------------
    # רשימת תבניות
    # ------------------------------------------------------------------
    def list_templates(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """
        מחזיר [{id, name, variables, error?}] לכל תבנית בתיקייה.

        תבנית עם שגיאת תחביר מופיעה עם error (כדי שיהיה ברור למה אי אפשר
        להשתמש בה) ולא מפילה את הרשימה כולה.
        """
        with self._lock:
            now = time.monotonic()
            if (not refresh and self._list_cache
                    and now - self._list_cache[0] < self.settings.TEMPLATE_LIST_CACHE_SECONDS):
                return self._list_cache[1]

            result = []
            for f in self.drive.list_templates():
                fingerprint = f.get("fingerprint") or ""
                cached = self._vars_cache.get(f["id"])
                if cached and cached[0] == fingerprint:
                    variables, error = cached[1], cached[2]
                else:
                    variables, error = self._inspect(f["id"])
                    self._vars_cache[f["id"]] = (fingerprint, variables, error)
                item: Dict[str, Any] = {"id": f["id"], "name": _display_name(f["name"]), "variables": variables}
                if error:
                    item["error"] = error
                result.append(item)
            self._list_cache = (now, result)
            return result

    def _inspect(self, file_id: str) -> Tuple[List[str], Optional[str]]:
        """מוריד תבנית ומחזיר את רשימת המשתנים שלה (או שגיאה). הקובץ הזמני נמחק תמיד."""
        path: Optional[str] = None
        try:
            path = self.drive.download_template(file_id)
            variables = DocxTemplate(path).get_undeclared_template_variables()
            return sorted(v for v in variables if v not in AUTO_VARIABLES), None
        except TemplateError as e:
            return [], f"שגיאת תחביר בתבנית: {e}"
        except Exception as e:  # noqa: BLE001 - תבנית פגומה אחת לא מפילה את הרשימה
            logger.warning("בדיקת התבנית %s נכשלה: %s", file_id, e)
            return [], f"לא ניתן לקרוא את התבנית: {str(e)[:200]}"
        finally:
            _remove(path)

    # ------------------------------------------------------------------
    # מילוי תבנית
    # ------------------------------------------------------------------
    def render(self, template_file_id: str, context: Dict[str, Any], title: str = "") -> Dict[str, Any]:
        """
        ממלא תבנית ומחזיר {download_url, file_name, drive_link?}.

        מחזיר {"error": ...} כשהתבנית לא חוקית/לא נמצאה, ו-{"missing": [...]}
        כשחסרים משתנים - בשני המקרים בלי ליצור קובץ.
        """
        templates = {t["id"]: t for t in self.list_templates()}
        if template_file_id not in templates:
            # ייתכן שהתבנית נוספה לדרייב ממש עכשיו - רענון אחד לפני שמחליטים
            templates = {t["id"]: t for t in self.list_templates(refresh=True)}
        template = templates.get(template_file_id)
        if not template:
            return {"error": "התבנית לא נמצאה בתיקיית התבניות. השתמש ב-ID מתוך list_document_templates."}
        if template.get("error"):
            return {"error": template["error"]}

        tpl_path: Optional[str] = None
        out_path = None
        try:
            tpl_path = self.drive.download_template(template_file_id)
            doc = DocxTemplate(tpl_path)
            variables = doc.get_undeclared_template_variables()
            # חסר = מפתח שלא נשלח בכלל; מחרוזת ריקה היא בחירה מכוונת (שדה אופציונלי)
            missing = sorted(v for v in variables if v not in AUTO_VARIABLES and v not in context)
            if missing:
                return {"missing": missing, "template": template["name"]}

            doc.render({**context, "today": datetime.now().strftime("%d/%m/%Y")}, autoescape=True)
            out_path = output_path(self.settings, title or template["name"])
            doc.save(out_path)
            result = publish(self.drive, out_path)
            result["template"] = template["name"]
            return result
        except TemplateError as e:
            _remove(out_path)
            return {"error": f"שגיאה במילוי התבנית '{template['name']}': {e}"}
        except Exception:
            _remove(out_path)  # פלט חלקי לא נשאר בתיקייה
            raise
        finally:
            _remove(tpl_path)


def _remove(path: Any) -> None:
    """מוחק קובץ אם הוא קיים, בלי להפיל את הקורא אם המחיקה נכשלה."""
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            logger.warning("מחיקת הקובץ הזמני %s נכשלה: %s", path, e)


def _display_name(name: str) -> str:
    """שם תבנית בלי סיומת .docx (קצר יותר בפלט לסוכן)."""
    return name[:-5] if name.lower().endswith(".docx") else name
