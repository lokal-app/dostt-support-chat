/* ---------------------------------------------------------------------
   Dostt Support Chatbot — thin UI client over the Python backend.
   All business logic (FAQ matching, refund automation, ticket rules,
   fallback/safety routing) now lives server-side (backend/app/) behind
   /api/chat's Gemini tool-calling agent — this file only renders the
   guided-flow cards (fetched from the API) and forwards messages/replies.
   See frontend/README.md for what needs GEMINI_VERTEX_CREDENTIALS_JSON to
   actually respond, vs. what works with no AI credentials at all.
--------------------------------------------------------------------- */

const state = {
  lang: "en",
  // Real deployment: the host app hands these off in the webview URL as
  // ?userId=<plain numeric id>&token=<raw JWT>&version=<app version> —
  // confirmed 2026-09-11 against a real captured hand-off payload
  // (userId is PLAIN, not base64-wrapped as earlier assumed from the
  // AstroHelp sibling project's convention; the URL param names are
  // camelCase `userId`/`token`, not `user_id`/`auth_token`). Internal
  // state/variable names below stay as user_id/authToken-style for
  // consistency with the rest of this file and the backend's own
  // ChatRequest field names — only the URL-reading key names had to
  // change to match the real contract. Defaults to the first seeded demo
  // account when opened with no query params at all.
  userId: new URLSearchParams(location.search).get("userId") || "90001",
  // Re-sent on every /api/chat call rather than stored server-side (see
  // backend/app/agent/context.py's SessionContext) — never logged or shown
  // in the UI. null for demo/test accounts opened with no ?token= at all
  // (ticket mirroring to the real Dostt API then simply no-ops — see
  // ticket_service.py).
  authToken: new URLSearchParams(location.search).get("token") || null,
  // The host app's own version string (e.g. "1.1.38") — more accurate than
  // the hardcoded DOSTT_APP_VERSION default when present, since it's the
  // real app version this real user is actually running (see
  // dostt_api_client.py's app-version header). null when opened with no
  // ?version= (falls back to the backend's own default).
  appVersion: new URLSearchParams(location.search).get("version") || null,
  sessionId: null,
  accountId: null,
  name: "",
  role: "user",
  ended: false,
  history: [],
};

/* Line-style SVG icons (currentColor) instead of platform emoji — the
   default red "❓" circle emoji clashed hard against the purple glass theme. */
const ICONS = {
  phone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg>',
  video: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>',
  card: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="4" width="22" height="16" rx="2" ry="2"/><line x1="1" y1="10" x2="23" y2="10"/></svg>',
  wallet: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/></svg>',
  chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
  help: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
};

// A handful of common, non-call/non-transaction questions — shown as quick
// chips on the first empty state, same pattern as the AstroHelp sibling
// project's FaqChips.tsx. Sent as free text, straight to /api/chat, exactly
// like anything typed into the composer. Text itself now lives in
// FAQ_SUGGESTIONS_I18N (data.js), keyed by language.

const $body = document.getElementById("chatBody");
const $ticketsBody = document.getElementById("ticketsBody");
const $composerBar = document.getElementById("composerBar");
const $input = document.getElementById("freeTextInput");
const $sendBtn = document.getElementById("sendBtn");
const $langSelect = document.getElementById("langSelect");
const $tabChat = document.getElementById("tabChat");
const $tabTickets = document.getElementById("tabTickets");
const $attachBtn = document.getElementById("attachBtn");
const $attachmentInput = document.getElementById("attachmentInput");
const $attachmentChip = document.getElementById("attachmentChip");
const $attachmentChipName = document.getElementById("attachmentChipName");
const $attachmentRemoveBtn = document.getElementById("attachmentRemoveBtn");

function wait(ms) { return new Promise((r) => setTimeout(r, ms)); }
function scrollDown() { $body.scrollTop = $body.scrollHeight; }
function fmtDuration(sec) { return sec >= 60 ? `${Math.floor(sec / 60)}m ${sec % 60}s` : `${sec}s`; }
function escapeHtml(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}
function addRow(cls, node) {
  const row = el("div", `row ${cls}`);
  row.appendChild(node);
  $body.appendChild(row);
  scrollDown();
  return row;
}
function botBubble(html) { return addRow("bot", el("div", "bubble", html)); }
function userBubble(text) { const b = el("div", "bubble"); b.textContent = text; return addRow("user", b); }
function traceLines(trace) {
  if (!trace || !trace.length) return;
  const wrap = el("div", "trace-lines");
  wrap.innerHTML = trace
    .map((s) => `<div class="trace-line">${s.ok ? escapeHtml(s.summary) : `Couldn't complete: ${escapeHtml(s.summary)}`}</div>`)
    .join("");
  addRow("bot", wrap);
}
function showTyping() {
  return addRow("bot typing", el("div", "bubble", '<span class="dot"></span><span class="dot"></span><span class="dot"></span>'));
}
function renderTicketRaised(ticketId) {
  // Deliberately neutral — never states or implies whether the refund
  // automation approved anything behind the scenes (product decision,
  // 2026-09-08). Just confirms a ticket exists and points at My Tickets,
  // where its real status is visible without a chat claim attached to it.
  const card = el("div", "result-card ticket-raised");
  card.innerHTML = `
    <div class="headline">Ticket #${ticketId} raised</div>
    <div class="reason">Our team will review it — track its status anytime under My Tickets.</div>
  `;
  addRow("bot", card);
}
function cardGrid(items) {
  const wrap = el("div", "card-grid");
  items.forEach((item) => {
    const card = el("button", `option-card${item.muted ? " muted" : ""}`);
    card.type = "button";
    card.innerHTML = `<div class="icon">${item.icon}</div><div class="body"><div class="title">${escapeHtml(item.title)}</div><div class="subtitle">${escapeHtml(item.subtitle || "")}</div></div><div class="chev">›</div>`;
    card.addEventListener("click", item.onClick);
    wrap.appendChild(card);
  });
  return addRow("bot", wrap);
}
function pillRow(options) {
  const wrap = el("div", "pill-row");
  options.forEach((opt) => {
    const btn = el("button", `pill-btn${opt.primary ? " primary" : ""}`, escapeHtml(opt.label));
    btn.addEventListener("click", opt.onClick);
    wrap.appendChild(btn);
  });
  return addRow("bot", wrap);
}
function renderFaqChips() {
  const wrap = el("div", "faq-chip-block");
  const label = el("p", "faq-chip-label", t(state.lang, "commonQuestionsLabel"));
  const row = el("div", "faq-chip-row");
  const suggestions = FAQ_SUGGESTIONS_I18N[state.lang] || FAQ_SUGGESTIONS_I18N.en;
  suggestions.forEach((question) => {
    const chip = el("button", "faq-chip", escapeHtml(question));
    chip.type = "button";
    chip.addEventListener("click", () => { userBubble(question); sendMessage(question, { skipEcho: true }); });
    row.appendChild(chip);
  });
  wrap.appendChild(label);
  wrap.appendChild(row);
  addRow("bot", wrap);
}

/* ------------------------------- Session --------------------------------- */
async function resetChat() {
  $body.innerHTML = "";
  $input.disabled = true;
  $sendBtn.disabled = true;
  $input.placeholder = t(state.lang, "freeTextPlaceholder");
  state.ended = false;
  state.history = [];
  hideAttachmentChip(); // a new session has no pending attachment

  console.log("[resetChat] userId=" + state.userId + " lang=" + state.lang);
  botBubble(t(state.lang, "connecting"));
  try {
    const session = await api.initSession(state.userId, state.lang);
    state.sessionId = session.session_id;
    state.accountId = session.account_id;
    state.name = session.name;
    state.role = session.role;
    console.log("[resetChat] session ready:", JSON.stringify(session));
  } catch (err) {
    console.error("[resetChat] session init failed:", err.message || err);
    $body.innerHTML = "";
    botBubble(escapeHtml(t(state.lang, "couldntStartSession", err.message || "unknown error")));
    return;
  }

  $body.innerHTML = "";
  $input.disabled = false;
  $sendBtn.disabled = false;

  botBubble(escapeHtml(t(state.lang, "greeting", state.name)));
  showCategoryCards();
  renderFaqChips();
}

function showCategoryCards() {
  cardGrid([
    { icon: ICONS.phone, title: t(state.lang, "catCalls"), subtitle: t(state.lang, "catCallsSub"), onClick: () => selectCategory("calls") },
    { icon: ICONS.card, title: t(state.lang, "catTx"), subtitle: t(state.lang, "catTxSub"), onClick: () => selectCategory("tx") },
    { icon: ICONS.chat, title: t(state.lang, "catOther"), subtitle: t(state.lang, "catOtherSub"), onClick: () => selectCategory("other") },
  ]);
}

function selectCategory(cat) {
  console.log("[selectCategory]", cat);
  if (cat === "calls") { userBubble(t(state.lang, "catCalls")); showCallsFlow(); }
  else if (cat === "tx") { userBubble(t(state.lang, "catTx")); showTransactionsFlow(); }
  else { userBubble(t(state.lang, "catOther")); botBubble(t(state.lang, "catOtherPrompt")); }
}

/* --------------------------------- Calls --------------------------------- */
async function showCallsFlow() {
  // Recent Calls is backed by a live Redash/BigQuery query with no caching
  // benefit on a first-time lookup — observed taking 30+ seconds in real
  // testing (the query itself, not our polling, is what's slow). Without
  // this, the UI showed nothing at all for that whole wait, indistinguishable
  // from the app being frozen or broken.
  const typingEl = showTyping();
  let calls;
  try {
    console.log("[showCallsFlow] fetching calls for account_id=" + state.accountId);
    calls = await api.recentCalls(state.accountId);
    console.log("[showCallsFlow] got " + calls.length + " calls");
  } catch (err) {
    console.error("[showCallsFlow] failed:", err.message);
    typingEl.remove();
    botBubble(escapeHtml(t(state.lang, "connectionError")));
    return;
  }
  typingEl.remove();
  if (!calls.length) { botBubble(t(state.lang, "noRecentActivity")); return; }
  cardGrid(calls.slice(0, 5).map((c) => ({
    icon: c.call_type === "video" ? ICONS.video : ICONS.phone,
    title: t(state.lang, "callWith", c.call_type === "video", c.counterpart_name),
    subtitle: t(state.lang, "durationCoins", fmtDuration(c.duration_sec), c.coins_debited),
    onClick: () => selectCall(c),
  })));
}

async function selectCall(call) {
  userBubble(t(state.lang, "callWith", call.call_type === "video", call.counterpart_name));
  const faqs = await api.faqs("calls", state.role);
  showFaqCards(faqs, (faq) => `I have an issue: ${faq.category} — ${faq.sub_issue}. This is about my ${call.call_type} call with ${call.counterpart_name} (call_id=${call.id}).`);
}

/* ----------------------------- Transactions ------------------------------ */
async function showTransactionsFlow() {
  if (state.role === "listener") {
    botBubble(t(state.lang, "earningsOrPayouts"));
    pillRow([
      { label: t(state.lang, "earningsLabel"), onClick: async () => { userBubble(t(state.lang, "earningsLabel")); const faqs = await api.faqs("earnings", "listener"); showFaqCards(faqs, (faq) => `I have an issue: ${faq.category} — ${faq.sub_issue}.`); } },
      { label: t(state.lang, "payoutsLabel"), onClick: () => showTxList("withdrawal", "payouts") },
    ]);
    return;
  }
  showTxList("recharge", "transactions_user");
}

async function showTxList(kind, landingCategory) {
  // Same reasoning as showCallsFlow's typing indicator above — Recent
  // Transactions hits the same kind of slow, uncached Redash/BigQuery query.
  const typingEl = showTyping();
  let txs;
  try {
    console.log("[showTxList] fetching " + kind + " for account_id=" + state.accountId);
    txs = await api.recentTransactions(state.accountId, kind);
    console.log("[showTxList] got " + txs.length + " " + kind + " records");
  } catch (err) {
    console.error("[showTxList] failed:", err.message);
    typingEl.remove();
    botBubble(escapeHtml(t(state.lang, "connectionError")));
    return;
  }
  typingEl.remove();
  if (!txs.length) { botBubble(t(state.lang, "noRecentActivity")); return; }
  cardGrid(txs.slice(0, 5).map((tx) => ({
    icon: kind === "recharge" ? ICONS.card : ICONS.wallet,
    title: kind === "recharge" ? t(state.lang, "rechargeCard", tx.amount_inr, tx.coins) : t(state.lang, "payoutCard", tx.amount_inr),
    subtitle: t(state.lang, "statusLabel", tx.status),
    onClick: async () => {
      userBubble(kind === "recharge" ? t(state.lang, "rechargeShort", tx.amount_inr) : t(state.lang, "payoutShort", tx.amount_inr));
      const faqs = await api.faqs(landingCategory, state.role);
      showFaqCards(faqs, (faq) => `I have an issue: ${faq.category} — ${faq.sub_issue}. This is about my ${kind} of ₹${tx.amount_inr} (status: ${tx.status}, transaction_id=${tx.id}).`);
    },
  })));
}

/* --------------------------------- FAQs ---------------------------------- */
function showFaqCards(faqs, buildMessage) {
  const cards = faqs.map((faq) => ({
    icon: ICONS.help,
    title: faq.category,
    subtitle: faq.sub_issue,
    onClick: () => { userBubble(faq.category); sendMessage(buildMessage(faq), { skipEcho: true }); },
  }));
  cards.push({
    icon: ICONS.edit,
    title: t(state.lang, "noneOfThese"),
    subtitle: t(state.lang, "describeOwnWordsSubtitle"),
    muted: true,
    onClick: () => { userBubble(t(state.lang, "noneOfThese")); botBubble(t(state.lang, "noneOfThesePrompt")); },
  });
  cardGrid(cards);
}

/* ------------------------------- Messaging -------------------------------- */
async function sendMessage(text, { skipEcho } = {}) {
  if (!text.trim() || state.ended) return;
  console.log("[sendMessage] text=" + text.slice(0, 80) + " history_len=" + state.history.length);
  if (!skipEcho) userBubble(text);
  state.history.push({ role: "account", text });

  const typingEl = showTyping();
  try {
    const resp = await api.sendChat(state.sessionId, text, state.history.slice(0, -1), state.authToken, state.appVersion);
    typingEl.remove();
    console.log("[sendMessage] reply received, trace=" + JSON.stringify(resp.trace) + " metadata=" + JSON.stringify(resp.metadata));
    traceLines(resp.trace);
    botBubble(escapeHtml(resp.reply));
    state.history.push({ role: "assistant", text: resp.reply });

    if (resp.metadata && resp.metadata.created_ticket_id) {
      console.log("[sendMessage] ticket created: #" + resp.metadata.created_ticket_id);
      renderTicketRaised(resp.metadata.created_ticket_id);
      hideAttachmentChip(); // the backend already consumed it into that ticket
    }

    if (resp.metadata && resp.metadata.session_ended) {
      state.ended = true;
      $input.disabled = true;
      $sendBtn.disabled = true;
    } else if (resp.metadata && resp.metadata.show_feedback) {
      await wait(400);
      botBubble(t(state.lang, "anythingElse"));
      pillRow([
        { label: t(state.lang, "yes"), primary: true, onClick: () => { userBubble(t(state.lang, "yes")); showCategoryCards(); } },
        {
          label: t(state.lang, "no"),
          onClick: () => {
            userBubble(t(state.lang, "no"));
            botBubble(t(state.lang, "closeMsg"));
            state.ended = true;
            $input.disabled = true;
            $sendBtn.disabled = true;
          },
        },
      ]);
    }
  } catch (err) {
    typingEl.remove();
    console.error("[sendMessage] failed:", err.status, err.message);
    if (err.status === 500) {
      botBubble(escapeHtml(t(state.lang, "needsGeminiKey")));
    } else {
      botBubble(escapeHtml(t(state.lang, "connectionError")));
    }
  }
}

/* ------------------------------ My Tickets tab ----------------------------- */
function fmtDate(iso) {
  const d = new Date(iso + "Z");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
async function loadTicketsTab() {
  $ticketsBody.innerHTML = "";
  if (!state.accountId) return;
  let tickets;
  try {
    tickets = await api.myTickets(state.accountId);
  } catch {
    $ticketsBody.appendChild(el("p", "tickets-empty", t(state.lang, "couldntLoadTickets")));
    return;
  }
  if (!tickets.length) {
    $ticketsBody.appendChild(el("p", "tickets-empty", t(state.lang, "noTicketsYet")));
    return;
  }
  tickets.forEach((ticket) => {
    const item = el("div", "ticket-item");
    item.innerHTML = `
      <div class="top-row">
        <span class="category">${escapeHtml(ticket.category.replace(/_/g, " "))}</span>
        <span class="ticket-status-pill st-${ticket.status}">${ticket.status.replace("_", " ")}</span>
      </div>
      <div class="sub">${escapeHtml(ticket.sub_category)}</div>
      <div class="meta-row"><span>#${ticket.id}</span><span>${fmtDate(ticket.created_at)}</span></div>
    `;
    $ticketsBody.appendChild(item);
  });
}
function switchTab(tab) {
  console.log("[switchTab]", tab);
  const onChat = tab === "chat";
  $body.hidden = !onChat;
  $composerBar.hidden = !onChat;
  $ticketsBody.hidden = onChat;
  $tabChat.classList.toggle("active", onChat);
  $tabTickets.classList.toggle("active", !onChat);
  if (!onChat) loadTicketsTab();
}
$tabChat.addEventListener("click", () => switchTab("chat"));
$tabTickets.addEventListener("click", () => switchTab("tickets"));

/* --------------------------------- Wiring --------------------------------- */
function populateLangSelect() {
  $langSelect.innerHTML = "";
  LANGUAGES.forEach((l) => {
    const o = el("option", null, escapeHtml(l.label));
    o.value = l.code;
    $langSelect.appendChild(o);
  });
  $langSelect.value = state.lang;
}

/* ------------------------------- Attachment -------------------------------- */
function showAttachmentChip(name) {
  $attachmentChipName.textContent = name;
  $attachmentChip.hidden = false;
}
function hideAttachmentChip() {
  $attachmentChip.hidden = true;
  $attachmentChipName.textContent = "";
  $attachmentInput.value = "";
}

$attachBtn.addEventListener("click", () => { if (!state.ended && state.sessionId) $attachmentInput.click(); });

$attachmentInput.addEventListener("change", async () => {
  const file = $attachmentInput.files[0];
  if (!file || !state.sessionId) return;
  try {
    await api.uploadAttachment(state.sessionId, file);
    showAttachmentChip(file.name);
  } catch (err) {
    botBubble(escapeHtml(err.message || t(state.lang, "attachmentErrorDefault")));
    $attachmentInput.value = "";
  }
});

$attachmentRemoveBtn.addEventListener("click", async () => {
  hideAttachmentChip();
  if (state.sessionId) {
    try { await fetch(`/api/session/${state.sessionId}/attachment`, { method: "DELETE" }); } catch (err) { /* best-effort */ }
  }
});

$langSelect.addEventListener("change", () => {
  state.lang = $langSelect.value;
  if (!I18N[state.lang]) { state.lang = "en"; $langSelect.value = "en"; }
  resetChat();
});
document.getElementById("restartBtn").addEventListener("click", resetChat);
$sendBtn.addEventListener("click", () => { const v = $input.value; $input.value = ""; sendMessage(v); });
$input.addEventListener("keydown", (e) => { if (e.key === "Enter") { const v = $input.value; $input.value = ""; sendMessage(v); } });

/* ------------------------------ Keyboard-safe layout ----------------------------- */
/* .chat-shell's CSS height reads var(--app-height, 100dvh) — 100dvh alone reliably
   shrinks when the keyboard opens in a normal mobile browser tab, but many native
   apps' embedded WebViews never resize the layout viewport at all on keyboard open
   (depends on the host app's own Android windowSoftInputMode / iOS WKWebView setup,
   which we don't control), so the composer silently ends up hidden underneath the
   keyboard with no CSS-only way to detect it. window.visualViewport tracks the
   actually-visible area independent of that, in every WebView new enough to matter
   (Android WebView 61+, iOS WKWebView 13+) — this keeps --app-height in sync with it. */
function setupKeyboardSafeViewport() {
  if (!window.visualViewport) return; // no-op fallback: CSS keeps its 100dvh default
  const root = document.documentElement;
  const applyHeight = () => root.style.setProperty("--app-height", `${window.visualViewport.height}px`);
  window.visualViewport.addEventListener("resize", applyHeight);
  window.visualViewport.addEventListener("scroll", applyHeight);
  applyHeight();
}
setupKeyboardSafeViewport();

// Second, independent layer: a temporary bottom spacer added on focus. Needed
// because a lot of Android WebViews use windowSoftInputMode="adjustPan" (a
// very common default, set by the *host app*, not us) — in that mode the
// WebView's viewport dimensions never change at all when the keyboard opens,
// so visualViewport above never fires and --app-height never updates; the
// page has no signal the keyboard exists. Making the page temporarily taller
// than one screen gives the browser/WebView's own built-in "scroll the
// focused element into view" behavior — which exists even in pan mode, since
// it's a browser-level behavior, not something that depends on detecting a
// viewport resize — actual room to reveal the composer above the keyboard.
let $keyboardSpacer = null;
function keyboardSpacer() {
  if (!$keyboardSpacer) {
    $keyboardSpacer = document.createElement("div");
    $keyboardSpacer.id = "keyboardSpacer";
    $keyboardSpacer.style.height = "0px";
    // Appended as a SIBLING of .chat-shell, directly on body — not inside it.
    // .chat-shell is a fixed-height (100dvh) flex column whose chat-body
    // child has flex:1, which happily shrinks to absorb any new flex sibling
    // instead of letting the container actually overflow — so a spacer
    // placed inside it changes nothing about the page's real scrollable
    // height. Placed outside, it's genuine extra content below the fixed
    // shell, which is what actually makes body.scrollHeight grow.
    document.body.appendChild($keyboardSpacer);
  }
  return $keyboardSpacer;
}
$input.addEventListener("focus", () => {
  setTimeout(() => {
    // If visualViewport actually shrank, --app-height above already handles
    // it — no spacer needed. Only add one when there was NO measured shrink
    // at all, i.e. exactly the adjustPan case this exists for.
    const vv = window.visualViewport;
    const measuredShrink = vv ? Math.max(0, window.innerHeight - vv.height) : 0;
    const spacer = keyboardSpacer();
    spacer.style.height = measuredShrink > 40 ? "0px" : "300px";
    // Scroll the SPACER (not the composer) flush with the bottom of the
    // viewport. The spacer stands in for the keyboard's own footprint, so
    // this leaves the composer sitting directly above it — i.e. right where
    // the visible area actually ends once a real keyboard covers the
    // bottom ~300px, rather than flush with the bottom of the full
    // (keyboard-unaware) viewport, which would put it exactly where the
    // keyboard is about to appear.
    spacer.scrollIntoView({ block: "end", behavior: "smooth" });
  }, 300);
});
$input.addEventListener("blur", () => {
  if ($keyboardSpacer) $keyboardSpacer.style.height = "0px";
});

populateLangSelect();
resetChat();
