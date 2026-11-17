// Team Fund pay page: pick whose fee to pay, get a VietQR, wait for SePay to confirm.
// No framework and no build step. Every name goes in through textContent, never innerHTML.
"use strict";

const T = {
  vi: {
    title: "Đóng quỹ đội", month: "Tháng {p}", owing: "Chưa đóng", people: "{n} người", total: "Tổng còn nợ",
    search: "Tìm tên…", pick: "Chọn người bạn đóng cho (thường là chính bạn).", selected: "Đã chọn {n}",
    pay: "Tạo mã QR", allPaid: "Cả đội đã đóng đủ 🎉", allPaidSub: "Không còn ai nợ quỹ.",
    noBank: "Đội chưa cài tài khoản ngân hàng. Nhờ quản lý đội gửi /setbank trong bot Telegram.",
    for: "Đóng cho", amount: "Số tiền", bank: "Ngân hàng", account: "Số tài khoản", holder: "Chủ tài khoản",
    content: "Nội dung chuyển khoản", copy: "Chép", copied: "Đã chép",
    scan: "Quét mã QR bằng app ngân hàng. Giữ nguyên số tiền và nội dung chuyển khoản.",
    waiting: "Đang chờ xác nhận… thường trong vòng 5 phút", validUntil: "Mã hiệu lực đến {t}",
    back: "Quay lại", other: "Đóng cho người khác", paidTitle: "Đã nhận {a}", paidSub: "Cảm ơn! Đã ghi nhận cho {m}.",
    expired: "Mã QR này đã hết hạn. Tạo mã mới để đóng.", notFound: "Link không đúng hoặc đã được đổi. Hỏi quản lý đội link mới.",
    error: "Có lỗi xảy ra. Vui lòng thử lại.", offline: "Không có mạng. Kiểm tra kết nối rồi thử lại.",
    slow: "Thao tác quá nhanh. Đợi một chút rồi thử lại.", nothing: "Những người này không còn khoản nào phải đóng.",
    install: "Cài vào màn hình chính", iosInstall: "iPhone: bấm Chia sẻ → Thêm vào MH chính để cài.",
    monthly: "Quỹ tháng {p}", retry: "Thử lại", telegram: "Dùng Telegram? Mở bot",
  },
  en: {
    title: "Team fund", month: "Month {p}", owing: "Still to pay", people: "{n} people", total: "Total owed",
    search: "Search names…", pick: "Choose who you're paying for (usually yourself).", selected: "{n} selected",
    pay: "Get QR code", allPaid: "Everyone has paid 🎉", allPaidSub: "Nobody owes the fund.",
    noBank: "The team's bank account isn't set yet. Ask a manager to send /setbank in the Telegram bot.",
    for: "Paying for", amount: "Amount", bank: "Bank", account: "Account number", holder: "Account holder",
    content: "Transfer content", copy: "Copy", copied: "Copied",
    scan: "Scan the QR code in your banking app. Keep the amount and transfer content exactly as shown.",
    waiting: "Waiting for confirmation… usually within 5 minutes", validUntil: "Code valid until {t}",
    back: "Back", other: "Pay for someone else", paidTitle: "Received {a}", paidSub: "Thank you! Recorded for {m}.",
    expired: "This QR code has expired. Make a new one to pay.", notFound: "This link is wrong or was changed. Ask a team manager for the new link.",
    error: "Something went wrong. Please try again.", offline: "You're offline. Check your connection and try again.",
    slow: "Too many tries. Wait a moment and try again.", nothing: "Those people have nothing left to pay.",
    install: "Add to home screen", iosInstall: "iPhone: tap Share → Add to Home Screen to install.",
    monthly: "Monthly fee {p}", retry: "Try again", telegram: "Use Telegram? Open the bot",
  },
};

const app = document.getElementById("app");
const store = {  // per-device conveniences only; the page works without them
  get(k) { try { return JSON.parse(localStorage.getItem("tf." + k)); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem("tf." + k) : localStorage.setItem("tf." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};
const S = { data: null, lang: store.get("lang"), selected: new Set(), query: "", intent: null, timer: null, install: null };

const t = (k, p = {}) => (T[S.lang] || T.vi)[k].replace(/\{(\w+)\}/g, (_, x) => p[x] ?? "");
const money = (n) => Number(n).toLocaleString(S.lang === "en" ? "en-US" : "vi-VN") + "đ";
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}
function show(...nodes) { app.replaceChildren(...nodes); }
function toTop() { try { window.scrollTo(0, 0); } catch { /* not in every browser engine */ } }

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch("api/" + path, { headers: { "Content-Type": "application/json" }, cache: "no-store", ...opts });
  } catch { throw new Error("offline"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(res.status === 404 ? "notFound" : res.status === 429 ? "slow" :
                               body.error === "nothing_to_pay" ? "nothing" : body.error === "bank_not_set" ? "noBank" : "error");
  return body;
}

function toast(text) {
  let el = document.querySelector(".toast");
  if (!el) { el = h("div", { class: "toast", role: "status" }); document.body.append(el); }
  el.textContent = text; el.classList.add("show");
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove("show"), 1400);
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = h("textarea", { readonly: true, class: "offscreen" }); ta.value = text;
    document.body.append(ta); ta.select(); try { document.execCommand("copy"); } catch { /* ignore */ } ta.remove();
  }
  toast(t("copied"));
}

function header() {
  const d = S.data;
  return h("header", { class: "top" },
    h("div", {}, h("h1", {}, d ? d.team : "Team Fund"),
      h("div", { class: "muted small" }, d ? `${t("title")} · ${t("month", { p: d.period_label })}` : t("title"))),
    h("button", { class: "lang", "aria-label": "Language", onclick: () => {
      S.lang = S.lang === "en" ? "vi" : "en"; store.set("lang", S.lang); document.documentElement.lang = S.lang; render();
    } }, S.lang === "en" ? "Tiếng Việt" : "English"));
}
function footer() {
  const d = S.data, ios = /iphone|ipad|ipod/i.test(navigator.userAgent) && !navigator.standalone;
  return h("footer", {},
    S.install ? h("p", {}, h("button", { class: "btn ghost", onclick: async () => {
      S.install.prompt(); await S.install.userChoice.catch(() => null); S.install = null; render();
    } }, t("install"))) : ios ? h("p", {}, t("iosInstall")) : null,
    d && d.bot_username ? h("p", {}, h("a", { href: `https://t.me/${encodeURIComponent(d.bot_username)}`, rel: "noreferrer" }, t("telegram"))) : null,
    h("p", {}, "Team Fund · SePay"));
}

function billText(b) { return b.kind === "monthly" ? t("monthly", { p: b.period_label }) : `${b.title} (${b.period_label})`; }

function listView() {
  const d = S.data;
  if (!d.has_bank) return show(header(), h("div", { class: "card note" }, t("noBank")), footer());
  if (!d.unpaid.length) return show(header(), h("div", { class: "empty" }, h("div", { class: "big" }, "⚽"),
    h("h2", {}, t("allPaid")), h("p", { class: "muted" }, t("allPaidSub"))), footer());

  const q = S.query.trim().toLocaleLowerCase();
  const fold = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/đ/g, "d").toLowerCase();
  const people = d.unpaid.filter((u) => !q || fold(u.name).includes(fold(q)));
  const chosen = d.unpaid.filter((u) => S.selected.has(u.member_id));
  const sum = chosen.reduce((a, u) => a + u.total, 0);

  const items = people.map((u) => {
    const on = S.selected.has(u.member_id);
    return h("li", {}, h("button", { class: "person", "aria-pressed": on ? "true" : "false", onclick: () => {
      on ? S.selected.delete(u.member_id) : S.selected.add(u.member_id); listView();
      const again = document.querySelector(`[data-id="${u.member_id}"]`); if (again) again.focus();
    }, "data-id": u.member_id },
      h("span", { class: "tick", "aria-hidden": "true" }, on ? "✓" : ""),
      h("span", { class: "who" }, h("div", { class: "name" }, u.name), h("div", { class: "bills" }, u.bills.map(billText).join(" · "))),
      h("span", { class: "amt" }, money(u.total))));
  });
  const search = d.unpaid.length > 8 ? h("input", { class: "search", type: "search", placeholder: t("search"),
    "aria-label": t("search"), value: S.query, oninput: (e) => { S.query = e.target.value; listView();
      const s = document.querySelector(".search"); s.focus(); s.setSelectionRange(s.value.length, s.value.length); } }) : null;

  show(header(),
    h("div", { class: "summary" },
      h("div", { class: "stat" }, h("span", { class: "muted small" }, t("owing")), h("b", {}, t("people", { n: d.unpaid.length }))),
      h("div", { class: "stat" }, h("span", { class: "muted small" }, t("total")), h("b", {}, money(d.total_unpaid)))),
    h("p", { class: "muted small" }, t("pick")), search, h("ul", { class: "people" }, items), footer(),
    h("div", { class: "bar" }, h("div", { class: "wrap" },
      h("div", { class: "sel" }, t("selected", { n: chosen.length }), h("b", {}, money(sum))),
      h("button", { class: "btn", disabled: !chosen.length, onclick: pay }, t("pay")))));
}

async function pay(e) {
  const btn = e.currentTarget; btn.disabled = true;
  try {
    const intent = await api("pay", { method: "POST", body: JSON.stringify({ member_ids: [...S.selected] }) });
    S.intent = intent; store.set("intent", intent); S.selected.clear(); qrView(); toTop(); poll();
  } catch (err) { btn.disabled = false; toast(t(err.message)); if (err.message === "nothing") load(); }
}

function row(label, value, { code = false, copyable = true } = {}) {
  return h("div", { class: "row" }, h("div", {}, h("div", { class: "k" }, label), h("div", { class: "v" + (code ? " code" : "") }, value)),
    copyable ? h("button", { class: "copy", onclick: () => copy(String(value)) }, t("copy")) : null);
}
function qrView() {
  const i = S.intent;
  show(header(), h("div", { class: "card" },
      h("img", { class: "qr", src: i.qr_url, alt: `VietQR ${money(i.amount)}`, referrerpolicy: "no-referrer" }),
      h("p", { class: "small muted center" }, t("scan")),
      h("div", { class: "wait", role: "status" }, h("span", { class: "dot" }), t("waiting"))),
    h("div", { class: "card" },
      row(t("for"), i.members.join(", "), { copyable: false }),
      row(t("amount"), money(i.amount)),
      row(t("content"), i.content, { code: true }),
      row(t("bank"), i.bank_name, { copyable: false }),
      row(t("account"), i.bank_account, { code: true }),
      row(t("holder"), i.bank_holder || "", { copyable: false })),
    h("p", { class: "small muted center" }, t("validUntil", { t: i.expires_at })),
    h("button", { class: "btn ghost block", onclick: () => { stop(); S.intent = null; store.set("intent", null); load(); } }, t("back")),
    footer());
}
function doneView(st) {
  toTop();
  show(header(), h("div", { class: "done" }, h("div", { class: "big" }, "✅"), h("h1", {}, t("paidTitle", { a: money(st.amount) })),
      h("p", { class: "muted" }, t("paidSub", { m: st.members.join(", ") }))),
    h("button", { class: "btn block", onclick: () => load() }, t("other")), footer());
}
function messageView(key, retry = true) {
  toTop();
  show(header(), h("div", { class: "card note" }, t(key)),
    retry ? h("button", { class: "btn block", onclick: () => load() }, t("retry")) : null, footer());
}

function stop() { clearTimeout(S.timer); S.timer = null; }
function poll() {
  stop();
  if (!S.intent) return;
  S.timer = setTimeout(async () => {
    if (document.hidden) return poll();
    try {
      const st = await api("pay/" + encodeURIComponent(S.intent.code));
      if (st.status === "paid") { S.intent = null; store.set("intent", null); return doneView(st); }
      if (st.status === "expired") { S.intent = null; store.set("intent", null); return messageView("expired"); }
    } catch (err) { if (err.message === "notFound") { S.intent = null; store.set("intent", null); return messageView("notFound", false); } }
    poll();
  }, 4000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && S.intent && !S.timer) poll(); });

function render() { if (S.intent) qrView(); else if (S.data) listView(); }
async function load() {
  stop();
  try {
    S.data = await api("state");
    if (!S.lang) { S.lang = S.data.lang === "en" ? "en" : "vi"; }
    document.documentElement.lang = S.lang;
    document.title = `${S.data.team} · Team Fund`;
    const ids = new Set(S.data.unpaid.map((u) => u.member_id));
    for (const id of [...S.selected]) if (!ids.has(id)) S.selected.delete(id);
    const saved = store.get("intent");
    if (saved && saved.code) { S.intent = saved; qrView(); poll(); } else listView();
  } catch (err) { S.lang = S.lang || "vi"; messageView(err.message, err.message !== "notFound"); }
}

window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); S.install = e; render(); });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js", { scope: "./" }).catch(() => {});
load();
