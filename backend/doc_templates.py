"""
doc_templates.py - הגדרות סוגי המסמכים של אשף ההפקה.

מה הקובץ עושה:
    לכל סוג מסמך מגדיר: כותרת, שדות חובה (עם תווית בעברית), אילו שדות אפשר למלא
    מראש מהמטא-דאטה של החוזה, וגוף מסמך בתבנית Markdown עם {field}.

איך זה עובד:
    הכלי prepare_document מחזיר לסוכן מה כבר ידוע ומה חסר; הסוכן שואל את המשתמש
    רק על החסר, ואז generate_word_document ממלא את body_template בערכים.

תרומה לחיסכון בטוקנים:
    - ההגדרות האלה לא נמצאות ב-instructions; הן מגיעות למודל רק כשמבקשים מסמך.
    - במסמך מובנה המודל מחזיר רק ערכי שדות קצרים (JSON) ולא כותב את כל המכתב -
      טוקני פלט הם היקרים ביותר.
"""

from __future__ import annotations

from typing import Any, Dict

# prefill: שם שדה -> מפתח/ות במטא-דאטה של החוזה (הראשון שיש לו ערך נבחר)
DOC_TYPES: Dict[str, Dict[str, Any]] = {
    "termination_notice": {
        "title": "הודעה על סיום / אי-חידוש הסכם",
        "fields": {
            "recipient": "שם הנמען (הצד השני לחוזה)",
            "property": "הנכס",
            "contract_date": "תאריך תחילת ההסכם",
            "end_date": "תאריך סיום ההסכם",
            "sender_name": "שם החותם",
            "sender_role": "תפקיד החותם",
        },
        "optional": {"notes": "הערות נוספות (פינוי, השבת ערבות וכו')"},
        "prefill": {"property": ["property", "block_parcel"], "contract_date": ["start_date"],
                    "end_date": ["end_date"]},
        "body_template": (
            "לכבוד: **{recipient}**\n\n"
            "## הנדון: הודעה על סיום ההסכם בנכס {property}\n\n"
            "בהתאם להסכם מיום {contract_date}, הרינו להודיעכם כי ההסכם יסתיים ביום "
            "**{end_date}** ולא יחודש.\n\n"
            "{notes}\n\n"
            "בכבוד רב,\n\n{sender_name}\n\n{sender_role}"
        ),
    },
    "option_exercise": {
        "title": "הודעה על מימוש אופציית הארכה",
        "fields": {
            "recipient": "שם הנמען",
            "property": "הנכס",
            "option_period": "תקופת האופציה (למשל: 24 חודשים)",
            "new_end_date": "תאריך הסיום החדש",
            "sender_name": "שם החותם",
            "sender_role": "תפקיד החותם",
        },
        "optional": {"notes": "תנאים מיוחדים לתקופת האופציה"},
        "prefill": {"property": ["property", "block_parcel"]},
        "body_template": (
            "לכבוד: **{recipient}**\n\n"
            "## הנדון: מימוש אופציה להארכת ההסכם בנכס {property}\n\n"
            "הרינו להודיעכם על מימוש האופציה להארכת תקופת ההסכם ב-{option_period}, "
            "עד ליום **{new_end_date}**, בהתאם לתנאי ההסכם.\n\n"
            "{notes}\n\n"
            "בכבוד רב,\n\n{sender_name}\n\n{sender_role}"
        ),
    },
    "payment_demand": {
        "title": "דרישת תשלום",
        "fields": {
            "recipient": "שם הנמען",
            "property": "הנכס",
            "amount": "סכום החוב (₪)",
            "period": "התקופה שבגינה החוב",
            "due_date": "מועד אחרון לתשלום",
            "sender_name": "שם החותם",
            "sender_role": "תפקיד החותם",
        },
        "optional": {"notes": "פירוט נוסף / סעיף רלוונטי בחוזה"},
        "prefill": {"property": ["property", "block_parcel"]},
        "body_template": (
            "לכבוד: **{recipient}**\n\n"
            "## הנדון: דרישת תשלום - נכס {property}\n\n"
            "על פי רישומינו, טרם שולם סך של **{amount} ₪** בגין התקופה {period}.\n\n"
            "הנכם נדרשים להסדיר את התשלום עד ליום **{due_date}**.\n\n"
            "{notes}\n\n"
            "בכבוד רב,\n\n{sender_name}\n\n{sender_role}"
        ),
    },
    "free": {
        "title": "מסמך חופשי (סיכום / דוח / השוואה)",
        "fields": {},
        "optional": {},
        "prefill": {},
        "body_template": None,  # התוכן מגיע מ-content_markdown
    },
}


def prefill_from_metadata(doc_type: str, metadata: Dict[str, Any]) -> Dict[str, str]:
    """ממלא שדות מתוך המטא-דאטה של החוזה, לפי מיפוי ה-prefill של סוג המסמך."""
    spec = DOC_TYPES[doc_type]
    out: Dict[str, str] = {}
    for field, keys in spec["prefill"].items():
        for k in keys:
            if metadata.get(k):
                out[field] = str(metadata[k])
                break
    parties = metadata.get("parties") or []
    if "recipient" in spec["fields"] and len(parties) == 2:
        out["recipient_options"] = " / ".join(parties)  # המודל יבקש מהמשתמש לבחור
    return out


def render_body(doc_type: str, fields: Dict[str, str]) -> str:
    """ממלא את body_template; שדה אופציונלי חסר הופך למחרוזת ריקה."""
    template = DOC_TYPES[doc_type]["body_template"] or ""
    spec = DOC_TYPES[doc_type]
    values = {k: "" for k in {**spec["fields"], **spec["optional"]}}
    values.update({k: v for k, v in fields.items() if v})
    return template.format(**values)
