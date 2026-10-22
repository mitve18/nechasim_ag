/*
  app.js - לוגיקת ה-Frontend של סוכן החוזים.

  אחריות:
    1. צ'אט ב-Streaming: /api/chat/stream (NDJSON) - טקסט מופיע בהדרגה, עם חיווי
       "מחפש בחוזים…" בזמן הפעלת כלים, וכפתור "עצור".
    2. שחזור השיחה אחרי רענון: /api/sessions/{id}/messages.
    3. דוחות מהירים: /api/reports/{kind} - SQL ישיר, 0 טוקנים.
    4. פעולות על תשובה: העתקה, העתקת טבלה לאקסל, ייצוא ל-Word, צ'יפים של מקורות.
    5. קבצים: רשימה, כרטיס חוזה (מטא-דאטה מקומי), מיקוד בחוזה, העלאה עם התקדמות, מחיקה.
    6. סנכרון ידני.

  חיסכון בטוקנים בצד הלקוח:
    - session_id קבוע לשיחה: השרת טוען רק את הסבבים האחרונים + סיכום, ולא כל ההיסטוריה.
    - כרטיס חוזה ודוחות לא מפעילים את המודל בכלל.
    - "ייצוא ל-Word" ממיר תשובה קיימת - המודל לא כותב אותה שוב.
*/

(() => {
  "use strict";

  // ---------- עזרי אחסון (localStorage עלול להיות חסום - לכן try/catch) ----------
  const store = {
    get(key) {
      try {
        return localStorage.getItem(key);
      } catch (_) {
        return null;
      }
    },
    set(key, val) {
      try {
        localStorage.setItem(key, val);
      } catch (_) {}
    },
  };

  const $ = (sel) => document.querySelector(sel);
  const els = {
    messages: $("#messages"), // אזור הגלילה
    list: $("#messageList"), // רשימת הבועות בתוך אזור הגלילה
    emptyTpl: $("#emptyStateTpl"),
    form: $("#chatForm"),
    input: $("#chatInput"),
    send: $("#sendBtn"),
    jump: $("#jumpBtn"),
    focusBar: $("#focusBar"),
    fileList: $("#fileList"),
    fileCount: $("#fileCount"),
    fileInput: $("#fileInput"),
    uploadBtn: $("#uploadBtn"),
    sidebar: $("#sidebar"),
    fileSearch: $("#fileSearch"),
    uploadProgress: $("#uploadProgress"),
    syncBtn: $("#syncBtn"),
    syncInfo: $("#syncInfo"),
    newChatBtn: $("#newChatBtn"),
    toast: $("#toast"),
    dialog: $("#fileDialog"),
  };

  let sessionId = store.get("session_id") || newSessionId();
  let files = [];
  let maxRetries = 3;
  let busy = false;
  let controller = null; // AbortController של הבקשה הפעילה
  let focusFile = null; // שם החוזה שהשיחה ממוקדת בו
  let pollTimer = null;
  const rawMarkdown = new WeakMap(); // בועה -> Markdown המקורי (להעתקה/ייצוא)

  const STATUS_LABELS = {
    indexed: "מאונדקס",
    processing: "בעיבוד…",
    pending: "ממתין",
    outdated: "עודכן - ממתין",
    error: "שגיאה",
  };

  const META_LABELS = [
    ["parties", "צדדים"],
    ["property", "נכס"],
    ["block_parcel", "גוש/חלקה"],
    ["start_date", "תחילה"],
    ["end_date", "סיום"],
    ["financial_terms", "תנאים כספיים"],
  ];

  // ======================================================================
  // עזרים כלליים
  // ======================================================================
  function newSessionId() {
    const id = (
      crypto.randomUUID
        ? crypto.randomUUID()
        : String(Date.now()) + Math.random()
    ).replace(/-/g, "");
    store.set("session_id", id);
    return id;
  }

  /** קריאת API אחידה: JSON, טיפול בשגיאות והחזרת הודעת השגיאה מהשרת בעברית. */
  async function api(path, options = {}) {
    const res = await fetch(path, options);
    let data = null;
    try {
      data = await res.json();
    } catch (_) {}
    if (!res.ok) throw new Error(errorDetail(data, res.status));
    return data;
  }

  function errorDetail(data, status) {
    const d = data && data.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d) && d[0] && d[0].msg) return d[0].msg;
    return `שגיאת שרת (${status})`;
  }

  function toast(text, ms = 3500) {
    els.toast.textContent = text;
    els.toast.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
      els.toast.hidden = true;
    }, ms);
  }

  /** ממיר Markdown ל-HTML בטוח (DOMPurify מונע הזרקת סקריפטים מתשובות המודל). */
  function renderMarkdown(md) {
    const html = window.marked ? marked.parse(md || "") : escapeHtml(md || "");
    return window.DOMPurify ? DOMPurify.sanitize(html) : html;
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  }

  function formatSize(bytes) {
    if (!bytes) return "";
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)}KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  }

  // ======================================================================
  // גלילה חכמה: גוללים למטה רק אם המשתמש כבר היה בתחתית
  // ======================================================================
  const nearBottom = () =>
    els.messages.scrollHeight -
      els.messages.scrollTop -
      els.messages.clientHeight <
    80;

  /** מריץ עדכון DOM ושומר על "הדבקה לתחתית" רק אם המשתמש היה שם לפני העדכון. */
  function withScroll(update) {
    const stick = nearBottom();
    update();
    if (stick) els.messages.scrollTop = els.messages.scrollHeight;
    else els.jump.hidden = false;
  }

  els.messages.addEventListener("scroll", () => {
    if (nearBottom()) els.jump.hidden = true;
  });
  els.jump.addEventListener("click", () => {
    els.messages.scrollTo({
      top: els.messages.scrollHeight,
      behavior: "smooth",
    });
    els.jump.hidden = true;
  });

  // ======================================================================
  // בועות הודעה
  // ======================================================================
  function showEmptyState() {
    els.list.innerHTML = "";
    els.list.appendChild(els.emptyTpl.content.cloneNode(true));
  }

  function clearEmptyState() {
    const empty = els.list.querySelector(".empty-state");
    if (empty) empty.remove();
  }

  function addUserMessage(text, focus) {
    clearEmptyState();
    const div = document.createElement("div");
    div.className = "msg msg-user";
    if (focus) {
      const tag = document.createElement("span");
      tag.className = "msg-focus";
      tag.innerHTML = `<i class="icon-paperclip"></i>`;
      tag.appendChild(document.createTextNode(focus));
      div.appendChild(tag);
    }
    const body = document.createElement("div");
    body.className = "msg-user-text";
    body.textContent = text;
    div.appendChild(body);
    els.list.appendChild(div);
    els.messages.scrollTop = els.messages.scrollHeight; // הודעה של המשתמש עצמו - תמיד גוללים
    return div;
  }

  const DEFAULT_STATUS = "מחפש בחוזים…";

  /** יוצר בועת סוכן ריקה עם אזורי סטטוס, תוכן, מקורות ופעולות. */
  function addBotMessage() {
    clearEmptyState();
    const div = document.createElement("div");
    div.className = "msg msg-bot";
    div.innerHTML = `
      <div class="msg-status"><span class="typing"><i></i><i></i><i></i></span><span class="status-text">${DEFAULT_STATUS}</span></div>
      <div class="msg-body"></div>
      <div class="msg-sources"></div>
      <div class="msg-footer" hidden>
        <div class="msg-actions">
          <button class="icon-btn" data-msg-action="copy" title="העתקה"><i class="icon-copy"></i></button>
          <button class="icon-btn" data-msg-action="table" title="העתקת טבלה לאקסל" hidden><i class="icon-sheet"></i></button>
          <button class="icon-btn" data-msg-action="word" title="ייצוא ל-Word (ללא עלות טוקנים)"><i class="icon-file-down"></i></button>
        </div>
        <span class="tokens"></span>
      </div>`;
    withScroll(() => els.list.appendChild(div));
    return div;
  }

  function setBotStatus(bubble, text) {
    const st = bubble.querySelector(".msg-status");
    st.hidden = !text && !!rawMarkdown.get(bubble);
    st.querySelector(".status-text").textContent = text || "";
  }

  function setBotError(bubble, message) {
    bubble.classList.add("msg-error");
    const st = bubble.querySelector(".msg-status");
    st.hidden = false;
    st.innerHTML = `<i class="icon-circle-alert"></i><span class="status-text">שגיאה: ${escapeHtml(message)}</span>`;
  }

  function setBotBody(bubble, md) {
    rawMarkdown.set(bubble, md);
    withScroll(() => {
      bubble.querySelector(".msg-body").innerHTML = renderMarkdown(md);
    });
  }

  /** סוגר בועה: מסתיר סטטוס, מציג מקורות, טוקנים וכפתורי פעולה. */
  function finishBotMessage(
    bubble,
    { tokens, cached_tokens, sources, note } = {},
  ) {
    bubble.querySelector(".msg-status").hidden = true;
    const src = bubble.querySelector(".msg-sources");
    src.innerHTML = (sources || [])
      .map((s) => {
        const label = `<i class="icon-file-text"></i>${escapeHtml(s.file)}${s.sections && s.sections.length ? ` · ${escapeHtml(s.sections.join(", "))}` : ""}`;
        return s.link
          ? `<a class="source-chip" href="${escapeHtml(s.link)}" target="_blank" rel="noopener" title="פתח בדרייב">${label}</a>`
          : `<span class="source-chip">${label}</span>`;
      })
      .join("");

    const footer = bubble.querySelector(".msg-footer");
    const t = footer.querySelector(".tokens");
    if (note) t.textContent = note;
    else if (tokens) {
      t.textContent =
        `${tokens.toLocaleString("he-IL")} טוקנים` +
        (cached_tokens
          ? ` (${cached_tokens.toLocaleString("he-IL")} מהמטמון)`
          : "");
    }
    footer.querySelector('[data-msg-action="table"]').hidden =
      !bubble.querySelector(".msg-body table");
    footer.hidden = !rawMarkdown.get(bubble);
    withScroll(() => {});
  }

  // ======================================================================
  // מצב עומס
  // ======================================================================
  function setBusy(value) {
    busy = value;
    els.send.innerHTML = `<i class="${value ? "icon-square" : "icon-arrow-up"}"></i>`;
    els.send.title = value ? "עצור" : "שליחה";
    els.send.classList.toggle("btn-stop", value);
    document.querySelectorAll(".chip").forEach((b) => {
      b.disabled = value;
    });
  }

  // ======================================================================
  // צ'אט ב-Streaming
  // ======================================================================
  /** שולח בקשה ל-/api/chat/stream ומעדכן את הבועה לפי האירועים (NDJSON). */
  async function streamChat(payload) {
    const bubble = addBotMessage();
    setBusy(true);
    controller = new AbortController();
    let text = "";
    let renderTimer = null;
    let done = null;
    const flush = () => {
      renderTimer = null;
      setBotBody(bubble, text);
    };

    try {
      const res = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, session_id: sessionId }),
        signal: controller.signal,
      });
      if (!res.ok) {
        let data = null;
        try {
          data = await res.json();
        } catch (_) {}
        throw new Error(errorDetail(data, res.status));
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done: end } = await reader.read();
        if (end) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          const ev = JSON.parse(line);
          if (ev.type === "delta") {
            text += ev.text;
            setBotStatus(bubble, "");
            // רינדור Markdown לכל היותר כל 80ms - חוסך עבודה לדפדפן בתשובות ארוכות
            if (!renderTimer) renderTimer = setTimeout(flush, 80);
          } else if (ev.type === "status") {
            setBotStatus(bubble, ev.text);
          } else if (ev.type === "done") {
            done = ev;
          } else if (ev.type === "error") {
            throw new Error(ev.text);
          }
        }
      }
      clearTimeout(renderTimer);
      setBotBody(bubble, text || "לא התקבלה תשובה.");
      finishBotMessage(bubble, done || {});
    } catch (err) {
      clearTimeout(renderTimer);
      if (err.name === "AbortError") {
        setBotBody(bubble, (text || "") + "\n\n_— נעצר —_");
        finishBotMessage(bubble, { note: "התשובה נעצרה" });
      } else {
        if (text) setBotBody(bubble, text);
        setBotError(bubble, err.message);
      }
    } finally {
      controller = null;
      setBusy(false);
    }
  }

  function sendMessage(text) {
    if (busy || !text) return;
    addUserMessage(text, focusFile);
    streamChat({ message: text, focus_file: focusFile });
  }

  els.form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (busy) {
      if (controller) controller.abort();
      return;
    } // הכפתור משמש כ"עצור"
    const text = els.input.value.trim();
    if (!text) return;
    els.input.value = "";
    autoGrow();
    sendMessage(text);
  });

  // Enter שולח, Shift+Enter יורד שורה
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!busy) els.form.requestSubmit();
    }
  });

  function autoGrow() {
    els.input.style.height = "auto";
    els.input.style.height = Math.min(els.input.scrollHeight, 160) + "px";
  }
  els.input.addEventListener("input", autoGrow);

  // ======================================================================
  // דוחות מהירים (SQL, 0 טוקנים) ואנליזת LLM
  // ======================================================================
  const mdCell = (v) =>
    String(v ?? "")
      .replace(/\|/g, "\\|")
      .replace(/\n/g, " ");

  /** ממיר דוח {title, columns, rows} לטבלת Markdown - כך העתקה וייצוא ל-Word עובדים גם עליו. */
  function reportToMarkdown(r) {
    let md = `### ${r.title}\n\n`;
    if (r.rows && r.rows.length) {
      md += `| ${r.columns.map(mdCell).join(" | ")} |\n| ${r.columns.map(() => "---").join(" | ")} |\n`;
      md += r.rows
        .map((row) => `| ${row.map(mdCell).join(" | ")} |`)
        .join("\n");
    }
    if (r.note) md += `\n\n${r.note}`;
    return md;
  }

  document.querySelectorAll("[data-report]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (busy) return;
      addUserMessage(btn.textContent.trim());
      const bubble = addBotMessage();
      setBusy(true);
      try {
        const report = await api(`/api/reports/${btn.dataset.report}`);
        setBotBody(bubble, reportToMarkdown(report));
        finishBotMessage(bubble, { note: "0 טוקנים · ישירות ממסד הנתונים" });
      } catch (err) {
        setBotError(bubble, err.message);
      } finally {
        setBusy(false);
      }
    });
  });

  document.querySelectorAll("[data-analysis]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (busy) return;
      addUserMessage(btn.textContent.trim());
      streamChat({ analysis: btn.dataset.analysis });
    });
  });

  // ======================================================================
  // פעולות על תשובה: העתקה, טבלה לאקסל, ייצוא ל-Word
  // ======================================================================
  els.messages.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-msg-action]");
    if (!btn) return;
    const bubble = btn.closest(".msg-bot");
    const md = rawMarkdown.get(bubble) || "";
    const action = btn.dataset.msgAction;
    try {
      if (action === "copy") {
        await navigator.clipboard.writeText(md);
        const icon = btn.querySelector("i");
        icon.className = "icon-check";
        setTimeout(() => {
          icon.className = "icon-copy";
        }, 1400);
      } else if (action === "table") {
        const table = bubble.querySelector(".msg-body table");
        const tsv = [...table.rows]
          .map((r) =>
            [...r.cells]
              .map((c) => c.innerText.replace(/\s+/g, " ").trim())
              .join("\t"),
          )
          .join("\n");
        await navigator.clipboard.writeText(tsv);
        toast("הטבלה הועתקה - אפשר להדביק באקסל");
      } else if (action === "word") {
        btn.disabled = true;
        const title =
          (md.match(/^#+\s+(.+)$/m) || [])[1] || "תשובת סוכן החוזים";
        const doc = await api("/api/docs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ markdown: md, title: title.slice(0, 200) }),
        });
        const a = document.createElement("a");
        a.href = doc.download_url;
        a.download = doc.file_name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        toast(doc.drive_link ? "המסמך הורד וגובה לדרייב" : "המסמך הורד");
      }
    } catch (err) {
      toast(err.message || "הפעולה נכשלה", 6000);
    } finally {
      btn.disabled = false;
    }
  });

  // ======================================================================
  // מיקוד בחוזה
  // ======================================================================
  function setFocus(name) {
    focusFile = name || null;
    els.focusBar.hidden = !focusFile;
    els.focusBar.innerHTML = focusFile
      ? `<span class="focus-chip"><i class="icon-paperclip"></i><span>${escapeHtml(focusFile)}</span>
           <button type="button" class="focus-clear" aria-label="ביטול מיקוד"><i class="icon-x"></i></button></span>`
      : "";
    if (focusFile) els.input.focus();
    renderFiles(); // מסמן את החוזה הממוקד ברשימה
  }
  els.focusBar.addEventListener("click", (e) => {
    if (e.target.closest(".focus-clear")) setFocus(null);
  });

  // ======================================================================
  // שיחה חדשה ושחזור שיחה
  // ======================================================================
  els.newChatBtn.addEventListener("click", () => {
    if (controller) controller.abort();
    sessionId = newSessionId();
    setFocus(null);
    showEmptyState();
    toast("נפתחה שיחה חדשה");
  });

  /** משחזר את הודעות השיחה מהשרת אחרי רענון הדף (קריאה מ-Postgres, ללא LLM). */
  async function restoreSession() {
    try {
      const { messages } = await api(
        `/api/sessions/${encodeURIComponent(sessionId)}/messages`,
      );
      if (!messages || !messages.length) {
        showEmptyState();
        return;
      }
      els.list.innerHTML = "";
      for (const m of messages) {
        if (m.role === "user") addUserMessage(m.content, m.focus);
        else {
          const b = addBotMessage();
          setBotBody(b, m.content);
          finishBotMessage(b, {});
        }
      }
      els.messages.scrollTop = els.messages.scrollHeight;
    } catch (_) {
      showEmptyState();
    }
  }

  // ======================================================================
  // קבצים
  // ======================================================================
  /** טוען את רשימת הקבצים מהשרת; ממשיך לרענן כל 5 שניות כל עוד יש קבצים בעיבוד. */
  async function loadFiles() {
    try {
      const data = await api("/api/files");
      files = data.files || [];
      maxRetries = data.max_retries || maxRetries;
      renderFiles();
      renderSyncInfo(data.last_sync);
      const inProgress = files.some((f) => f.status === "processing");
      clearTimeout(pollTimer);
      if (inProgress) pollTimer = setTimeout(loadFiles, 5000);
    } catch (err) {
      els.fileList.innerHTML = `<li class="file-empty">${escapeHtml(err.message)}</li>`;
    }
  }

  function statusLabel(f) {
    if (f.status === "error" && f.retries >= maxRetries)
      return "נכשל - דורש בדיקה";
    return STATUS_LABELS[f.status] || f.status;
  }

  /** מציג את רשימת הקבצים (כולל סינון לפי טקסט חופשי). */
  function renderFiles() {
    const q = els.fileSearch.value.trim().toLowerCase();
    const visible = files.filter((f) => !q || f.name.toLowerCase().includes(q));
    els.fileCount.textContent = files.length;
    if (!visible.length) {
      els.fileList.innerHTML = `<li class="file-empty">${files.length ? "אין תוצאות לסינון" : "התיקייה ריקה - העלה חוזה ראשון"}</li>`;
      return;
    }
    els.fileList.innerHTML = visible
      .map((f) => {
        const m = f.metadata || {};
        const metaText = m.end_date
          ? `עד ${m.end_date}`
          : m.block_parcel ||
            m.property ||
            formatSize(f.size) ||
            (f.extraction ? `פוענח: ${f.extraction}` : "");
        // שגיאה מוצגת בטקסט גלוי (ולא רק ב-tooltip) - כדי שתהיה נגישה גם בנייד
        const isErr = f.status === "error" && f.error;
        return `
        <li class="file-item${focusFile === f.name ? " active" : ""}" data-id="${escapeHtml(f.id)}" data-action="focus" title="${escapeHtml(f.name)}">
          <i class="icon-file-text" aria-hidden="true"></i>
          <div class="file-body">
            <div class="file-name">${escapeHtml(f.name)}</div>
            <div class="file-meta">
              <span class="status status-${escapeHtml(f.status)}">${escapeHtml(statusLabel(f))}</span>
              <span class="meta-text${isErr ? " file-error" : ""}" title="${escapeHtml(isErr ? f.error : metaText)}">${escapeHtml(isErr ? f.error : metaText)}</span>
            </div>
          </div>
          <button class="file-info" type="button" data-action="details" title="כרטיס חוזה" aria-label="כרטיס חוזה"><i class="icon-ellipsis"></i></button>
        </li>`;
      })
      .join("");
  }

  function renderSyncInfo(last) {
    if (!last) {
      els.syncInfo.textContent = "";
      return;
    }
    if (last.status === "error") {
      els.syncInfo.textContent = `סנכרון אחרון נכשל: ${last.message || ""}`;
      return;
    }
    const when = last.finished_at
      ? new Date(last.finished_at).toLocaleString("he-IL", {
          dateStyle: "short",
          timeStyle: "short",
        })
      : "";
    const parts = [
      last.new && `${last.new} חדשים`,
      last.updated && `${last.updated} עודכנו`,
      last.removed && `${last.removed} הוסרו`,
      last.failed && `${last.failed} נכשלו`,
      last.gave_up && `${last.gave_up} דורשים בדיקה`,
    ].filter(Boolean);
    els.syncInfo.textContent = `סנכרון אחרון ${when} · ${parts.length ? parts.join(", ") : "ללא שינויים"}`;
  }

  els.fileSearch.addEventListener("input", renderFiles);

  async function deleteFile(file) {
    if (!confirm(`להעביר את "${file.name}" לאשפה בדרייב ולהסיר מהאינדקס?`))
      return false;
    try {
      await api(`/api/files/${encodeURIComponent(file.id)}`, {
        method: "DELETE",
      });
      toast("הקובץ הועבר לאשפה");
      if (focusFile === file.name) setFocus(null);
      loadFiles();
      return true;
    } catch (err) {
      toast(err.message, 6000);
      return false;
    }
  }

  els.fileList.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const item = btn.closest(".file-item");
    const file = files.find((f) => f.id === item.dataset.id);
    if (!file) return;
    if (btn.dataset.action === "details") openFileDialog(file);
    if (btn.dataset.action === "focus") setFocus(file.name);
  });

  // ---------- כרטיס חוזה (מטא-דאטה מקומי, 0 טוקנים) ----------
  let dialogFile = null;

  function openFileDialog(file) {
    dialogFile = file;
    const m = file.metadata || {};
    $("#fdTitle").textContent = file.name;
    const rows = META_LABELS.map(([k, label]) => [
      label,
      Array.isArray(m[k]) ? m[k].join("; ") : m[k],
    ]).filter(([, v]) => v);
    rows.push(["סטטוס", statusLabel(file)]);
    if (file.extraction)
      rows.push([
        "פענוח",
        `${file.extraction}${file.pages ? ` · ${file.pages} עמ'` : ""}`,
      ]);
    if (file.error) rows.push(["שגיאה", file.error]);
    $("#fdMeta").innerHTML =
      rows
        .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`)
        .join("") ||
      `<dd class="muted">אין מטא-דאטה - הקובץ עוד לא אונדקס.</dd>`;
    const link = $("#fdLink");
    link.hidden = !file.link;
    link.href = file.link || "#";
    $("#fdReindex").hidden = !["error", "pending", "outdated"].includes(
      file.status,
    );
    els.dialog.showModal();
  }

  $("#fdFocus").addEventListener("click", () => {
    setFocus(dialogFile.name);
    els.dialog.close();
  });
  $("#fdDelete").addEventListener("click", async () => {
    if (await deleteFile(dialogFile)) els.dialog.close();
  });
  $("#fdReindex").addEventListener("click", async () => {
    try {
      await api(`/api/files/${encodeURIComponent(dialogFile.id)}/reindex`, {
        method: "POST",
      });
      toast("האינדוקס התחיל ברקע");
      els.dialog.close();
      loadFiles();
    } catch (err) {
      toast(err.message, 6000);
    }
  });

  // ---------- העלאה עם סרגל התקדמות ----------
  /** מעלה קובץ בודד ב-XHR (ל-fetch אין אירועי התקדמות להעלאה). */
  function uploadOne(file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const fd = new FormData();
      fd.append("file", file);
      xhr.open("POST", "/api/files/upload");
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded / e.total);
      };
      xhr.onload = () => {
        let data = null;
        try {
          data = JSON.parse(xhr.responseText);
        } catch (_) {}
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(errorDetail(data, xhr.status)));
      };
      xhr.onerror = () => reject(new Error("שגיאת רשת בהעלאה"));
      xhr.send(fd);
    });
  }

  /** מעלה קבצים אחד אחרי השני (לא במקביל - חוסך זיכרון בשרת). */
  async function uploadFiles(list) {
    const bar = els.uploadProgress.querySelector("i");
    const label = els.uploadProgress.querySelector("span");
    els.uploadProgress.hidden = false;
    for (let i = 0; i < list.length; i++) {
      const file = list[i];
      const prefix = list.length > 1 ? `(${i + 1}/${list.length}) ` : "";
      try {
        await uploadOne(file, (p) => {
          bar.style.width = `${Math.round(p * 100)}%`;
          label.textContent =
            p < 1
              ? `${prefix}מעלה את ${file.name}… ${Math.round(p * 100)}%`
              : `${prefix}שומר בדרייב…`;
        });
        toast(`${file.name} הועלה - מתבצע אינדוקס ברקע`);
      } catch (err) {
        toast(`${file.name}: ${err.message}`, 6000);
      }
      bar.style.width = "0";
      await loadFiles();
    }
    els.uploadProgress.hidden = true;
  }

  els.uploadBtn.addEventListener("click", () => els.fileInput.click());
  els.fileInput.addEventListener("change", () => {
    if (els.fileInput.files.length) uploadFiles([...els.fileInput.files]);
    els.fileInput.value = "";
  });

  // גרירת קבצים לכל סרגל הצד
  ["dragenter", "dragover"].forEach((ev) =>
    els.sidebar.addEventListener(ev, (e) => {
      e.preventDefault();
      els.sidebar.classList.add("dragover");
    }),
  );
  els.sidebar.addEventListener("dragleave", (e) => {
    if (!els.sidebar.contains(e.relatedTarget))
      els.sidebar.classList.remove("dragover");
  });
  els.sidebar.addEventListener("drop", (e) => {
    e.preventDefault();
    els.sidebar.classList.remove("dragover");
    if (e.dataTransfer.files.length) uploadFiles([...e.dataTransfer.files]);
  });

  // ======================================================================
  // סנכרון ידני
  // ======================================================================
  /** מפעיל Delta Sync ובודק כל 4 שניות עד שמתקבל סיכום חדש. */
  els.syncBtn.addEventListener("click", async () => {
    if (els.syncBtn.classList.contains("spinning")) return;
    els.syncBtn.classList.add("spinning");
    try {
      const before = (await api("/api/sync/status")).last_sync?.finished_at;
      await api("/api/files/refresh", { method: "POST" });
      toast("הסנכרון התחיל - רק קבצים חדשים או שהשתנו יעובדו");
      const started = Date.now();
      while (Date.now() - started < 15 * 60 * 1000) {
        await new Promise((r) => setTimeout(r, 4000));
        const last = (await api("/api/sync/status")).last_sync;
        if (last && last.finished_at !== before) {
          renderSyncInfo(last);
          toast("הסנכרון הסתיים");
          break;
        }
      }
      await loadFiles();
    } catch (err) {
      toast(err.message, 6000);
    } finally {
      els.syncBtn.classList.remove("spinning");
    }
  });

  // ---------- אתחול ----------
  if (window.marked) marked.setOptions({ breaks: true, gfm: true });
  restoreSession();
  loadFiles();
})();
