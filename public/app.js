/*
  app.js - לוגיקת ה-Frontend של סוכן החוזים.

  אחריות:
    1. צ'אט: שליחת הודעות ל-/api/chat, הצגת תשובות Markdown (marked + DOMPurify).
    2. קבצים: טעינת הרשימה מ-/api/files, העלאה (גרירה/בחירה), מחיקה, סינון.
    3. כפתורי אנליזה: קריאה ל-/api/analysis/{kind}.
    4. סנכרון ידני: /api/files/refresh + מעקב עד לסיום.
    5. ערכת נושא (כהה/בהיר) ו-session_id נשמרים ב-localStorage.

  חיסכון בטוקנים בצד הלקוח:
    - session_id קבוע לשיחה: השרת טוען רק את 4 הסבבים האחרונים מ-Postgres, ולכן
      הלקוח לא שולח את כל ההיסטוריה בכל בקשה - רק את ההודעה החדשה.
    - "שיחה חדשה" יוצרת session_id חדש, כך שנושא חדש לא סוחב היסטוריה לא רלוונטית.
*/

(() => {
  "use strict";

  // ---------- עזרי אחסון (localStorage עלול להיות חסום - לכן try/catch) ----------
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
    set(key, val) { try { localStorage.setItem(key, val); } catch (_) {} },
  };

  const $ = (sel) => document.querySelector(sel);
  const els = {
    messages: $("#messages"),
    form: $("#chatForm"),
    input: $("#chatInput"),
    send: $("#sendBtn"),
    fileList: $("#fileList"),
    fileCount: $("#fileCount"),
    fileInput: $("#fileInput"),
    dropZone: $("#dropZone"),
    fileSearch: $("#fileSearch"),
    syncBtn: $("#syncBtn"),
    syncInfo: $("#syncInfo"),
    themeBtn: $("#themeBtn"),
    newChatBtn: $("#newChatBtn"),
    toast: $("#toast"),
  };

  let sessionId = store.get("session_id") || newSessionId();
  let files = [];
  let busy = false;
  let pollTimer = null;

  const STATUS_LABELS = {
    indexed: "מאונדקס",
    processing: "בעיבוד…",
    pending: "ממתין",
    outdated: "עודכן - ממתין",
    error: "שגיאה",
  };

  // ======================================================================
  // עזרים כלליים
  // ======================================================================
  /** יוצר מזהה שיחה חדש ושומר אותו. */
  function newSessionId() {
    const id = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/-/g, "");
    store.set("session_id", id);
    return id;
  }

  /** קריאת API אחידה: JSON, טיפול בשגיאות והחזרת הודעת השגיאה מהשרת בעברית. */
  async function api(path, options = {}) {
    const res = await fetch(path, options);
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
      const detail = data && data.detail;
      throw new Error(typeof detail === "string" ? detail : `שגיאת שרת (${res.status})`);
    }
    return data;
  }

  /** הודעה קצרה בתחתית המסך. */
  function toast(text, ms = 3500) {
    els.toast.textContent = text;
    els.toast.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { els.toast.hidden = true; }, ms);
  }

  /** ממיר Markdown ל-HTML בטוח (DOMPurify מונע הזרקת סקריפטים מתשובות המודל). */
  function renderMarkdown(md) {
    const html = window.marked ? marked.parse(md || "") : (md || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
    return window.DOMPurify ? DOMPurify.sanitize(html) : html;
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function formatSize(bytes) {
    if (!bytes) return "";
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)}KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  }

  // ======================================================================
  // צ'אט
  // ======================================================================
  /** מוסיף בועת הודעה לחלון הצ'אט ומחזיר את האלמנט. */
  function addMessage(role, content, { html = false, extraClass = "" } = {}) {
    const empty = els.messages.querySelector(".empty-state");
    if (empty) empty.remove();
    const div = document.createElement("div");
    div.className = `msg msg-${role} ${extraClass}`.trim();
    if (html) div.innerHTML = content; else div.textContent = content;
    els.messages.appendChild(div);
    els.messages.scrollTop = els.messages.scrollHeight;
    return div;
  }

  /** שולח שאלה (או מפעיל אנליזה) ומציג את התשובה. */
  async function ask({ message, analysis }) {
    if (busy) return;
    busy = true;
    els.send.disabled = true;
    if (message) addMessage("user", message);
    const pending = addMessage("bot", '<span class="typing"><i></i><i></i><i></i></span>', { html: true });
    try {
      const data = analysis
        ? await api(`/api/analysis/${analysis}?session_id=${encodeURIComponent(sessionId)}`, { method: "POST" })
        : await api("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message, session_id: sessionId }),
          });
      sessionId = data.session_id || sessionId;
      store.set("session_id", sessionId);
      pending.innerHTML = renderMarkdown(data.answer || "לא התקבלה תשובה.");
      if (data.tokens) {
        const t = document.createElement("span");
        t.className = "tokens";
        t.textContent = `${data.tokens.toLocaleString("he-IL")} טוקנים`;
        pending.appendChild(t);
      }
      // אם הסוכן מחק קובץ או הריץ סנכרון - נרענן את רשימת הקבצים
      loadFiles();
    } catch (err) {
      pending.classList.add("msg-error");
      pending.textContent = `שגיאה: ${err.message}`;
    } finally {
      busy = false;
      els.send.disabled = false;
      els.messages.scrollTop = els.messages.scrollHeight;
    }
  }

  els.form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = els.input.value.trim();
    if (!text) return;
    els.input.value = "";
    autoGrow();
    ask({ message: text });
  });

  // Enter שולח, Shift+Enter יורד שורה
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      els.form.requestSubmit();
    }
  });

  function autoGrow() {
    els.input.style.height = "auto";
    els.input.style.height = Math.min(els.input.scrollHeight, 160) + "px";
  }
  els.input.addEventListener("input", autoGrow);

  document.querySelectorAll("[data-analysis]").forEach((btn) => {
    btn.addEventListener("click", () => {
      addMessage("user", btn.textContent.trim());
      ask({ analysis: btn.dataset.analysis });
    });
  });

  els.messages.addEventListener("click", (e) => {
    const ex = e.target.closest(".example");
    if (ex) ask({ message: ex.textContent.trim() });
  });

  els.newChatBtn.addEventListener("click", () => {
    sessionId = newSessionId();
    els.messages.innerHTML = '<div class="empty-state"><h3>שיחה חדשה</h3><p>ההיסטוריה הקודמת לא תישלח למודל.</p></div>';
    toast("נפתחה שיחה חדשה");
  });

  // ======================================================================
  // קבצים
  // ======================================================================
  /** טוען את רשימת הקבצים מהשרת; ממשיך לרענן כל 5 שניות כל עוד יש קבצים בעיבוד. */
  async function loadFiles() {
    try {
      const data = await api("/api/files");
      files = data.files || [];
      renderFiles();
      renderSyncInfo(data.last_sync);
      const inProgress = files.some((f) => f.status === "processing");
      clearTimeout(pollTimer);
      if (inProgress) pollTimer = setTimeout(loadFiles, 5000);
    } catch (err) {
      els.fileList.innerHTML = `<li class="muted small">${escapeHtml(err.message)}</li>`;
    }
  }

  /** מציג את רשימת הקבצים (כולל סינון לפי טקסט חופשי). */
  function renderFiles() {
    const q = els.fileSearch.value.trim().toLowerCase();
    const visible = files.filter((f) => !q || f.name.toLowerCase().includes(q));
    els.fileCount.textContent = files.length;
    if (!visible.length) {
      els.fileList.innerHTML = `<li class="muted small">${files.length ? "אין תוצאות לסינון" : "התיקייה ריקה - העלה חוזה ראשון"}</li>`;
      return;
    }
    els.fileList.innerHTML = visible.map((f) => {
      const m = f.metadata || {};
      const metaParts = [m.block_parcel || m.property, m.end_date ? `עד ${m.end_date}` : "", formatSize(f.size)].filter(Boolean);
      const title = f.status === "error" && f.error ? ` title="${escapeHtml(f.error)}"` : "";
      return `
        <li class="file-item" data-id="${escapeHtml(f.id)}">
          <a class="file-name" href="${escapeHtml(f.link || "#")}" target="_blank" rel="noopener" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</a>
          <div class="file-actions">
            <span class="status status-${escapeHtml(f.status)}"${title}>${STATUS_LABELS[f.status] || escapeHtml(f.status)}</span>
            <button class="icon-btn" data-action="ask" title="שאל על החוזה">💬</button>
            <button class="icon-btn danger" data-action="delete" title="מחיקה">🗑</button>
          </div>
          <div class="file-meta">${escapeHtml(metaParts.join(" · ") || (f.extraction ? `פוענח: ${f.extraction}` : ""))}</div>
        </li>`;
    }).join("");
  }

  function renderSyncInfo(last) {
    if (!last) { els.syncInfo.textContent = ""; return; }
    if (last.status === "error") { els.syncInfo.textContent = `סנכרון אחרון נכשל: ${last.message || ""}`; return; }
    const when = last.finished_at ? new Date(last.finished_at).toLocaleString("he-IL") : "";
    els.syncInfo.textContent = `סנכרון אחרון ${when}: ${last.new} חדשים, ${last.updated} עודכנו, ${last.skipped} ללא שינוי, ${last.removed} הוסרו${last.failed ? `, ${last.failed} נכשלו` : ""}`;
  }

  els.fileSearch.addEventListener("input", renderFiles);

  els.fileList.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const item = btn.closest(".file-item");
    const file = files.find((f) => f.id === item.dataset.id);
    if (!file) return;

    if (btn.dataset.action === "ask") {
      els.input.value = `סכם בקצרה את החוזה "${file.name}": צדדים, נכס, תוקף ותנאים כספיים.`;
      els.input.focus();
      autoGrow();
    }

    if (btn.dataset.action === "delete") {
      if (!confirm(`להעביר את "${file.name}" לאשפה בדרייב ולהסיר מהאינדקס?`)) return;
      btn.disabled = true;
      try {
        await api(`/api/files/${encodeURIComponent(file.id)}`, { method: "DELETE" });
        toast("הקובץ הועבר לאשפה");
        loadFiles();
      } catch (err) {
        toast(err.message, 6000);
        btn.disabled = false;
      }
    }
  });

  /** מעלה קבצים אחד אחרי השני (לא במקביל - חוסך זיכרון בשרת). */
  async function uploadFiles(list) {
    for (const file of list) {
      const fd = new FormData();
      fd.append("file", file);
      toast(`מעלה את ${file.name}…`, 60000);
      try {
        await api("/api/files/upload", { method: "POST", body: fd });
        toast(`${file.name} הועלה - מתבצע אינדוקס ברקע`);
      } catch (err) {
        toast(`${file.name}: ${err.message}`, 6000);
      }
      await loadFiles();
    }
  }

  els.fileInput.addEventListener("change", () => {
    if (els.fileInput.files.length) uploadFiles([...els.fileInput.files]);
    els.fileInput.value = "";
  });

  ["dragenter", "dragover"].forEach((ev) => els.dropZone.addEventListener(ev, (e) => {
    e.preventDefault();
    els.dropZone.classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach((ev) => els.dropZone.addEventListener(ev, (e) => {
    e.preventDefault();
    els.dropZone.classList.remove("dragover");
  }));
  els.dropZone.addEventListener("drop", (e) => {
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

  // ======================================================================
  // ערכת נושא
  // ======================================================================
  els.themeBtn.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store.set("theme", next);
  });

  // ---------- אתחול ----------
  if (window.marked) marked.setOptions({ breaks: true, gfm: true });
  loadFiles();
})();
