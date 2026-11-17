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
    tabPay: "Đóng quỹ", tabMonth: "Tháng này", tabFund: "Quỹ", tabMembers: "Thành viên",
    paidOf: "Đã đóng {a}/{b}", due: "Hạn đóng: ngày {d} hằng tháng", feeIs: "Mức quỹ: {f}/tháng",
    paidList: "Đã đóng", unpaidList: "Chưa đóng", nobodyPaid: "Chưa ai đóng tháng này.", nobodyOwes: "Không ai còn nợ.",
    income: "Tổng thu", expense: "Tổng chi", balance: "Số dư", byMonth: "Theo tháng", in: "Thu", out: "Chi",
    history: "Giao dịch gần đây", noHistory: "Chưa có giao dịch.", feeFrom: "Đóng quỹ: {n}", cashFrom: "Tiền mặt: {n}",
    unassigned: "Chuyển khoản chưa gán", owes: "Còn nợ {a}", allClear: "Đã đóng đủ", paused: "Tạm nghỉ",
    shirt: "Áo số {n}", noMembers: "Đội chưa có thành viên.", readOnly: "Chỉ xem. Quản lý đội trong bot Telegram.",
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
    tabPay: "Pay", tabMonth: "This month", tabFund: "Fund", tabMembers: "Members",
    paidOf: "{a} of {b} paid", due: "Due on day {d} of each month", feeIs: "Fee: {f}/month",
    paidList: "Paid", unpaidList: "Not paid yet", nobodyPaid: "Nobody has paid this month yet.", nobodyOwes: "Nobody owes anything.",
    income: "Money in", expense: "Money out", balance: "Balance", byMonth: "By month", in: "In", out: "Out",
    history: "Recent transactions", noHistory: "No transactions yet.", feeFrom: "Fee: {n}", cashFrom: "Cash: {n}",
    unassigned: "Transfer not assigned yet", owes: "Owes {a}", allClear: "All paid", paused: "On break",
    shirt: "Shirt {n}", noMembers: "The team has no members yet.", readOnly: "Read-only. The team is managed in the Telegram bot.",
  },
};

const app = document.getElementById("app");
const store = {  // per-device conveniences only; the page works without them
  get(k) { try { return JSON.parse(localStorage.getItem("tf." + k)); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem("tf." + k) : localStorage.setItem("tf." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};
const S = { data: null, lang: store.get("lang"), tab: store.get("tab") || "pay", selected: new Set(), query: "", intent: null, timer: null, install: null };

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
    h("p", {}, t("readOnly")), h("p", {}, "Team Fund · SePay"));
}

function billText(b) { return b.kind === "monthly" ? t("monthly", { p: b.period_label }) : `${b.title} (${b.period_label})`; }

const TABS = [["pay", "tabPay"], ["month", "tabMonth"], ["fund", "tabFund"], ["members", "tabMembers"]];
function tabs() {
  return h("nav", { class: "tabs", role: "tablist" }, TABS.map(([id, key]) =>
    h("button", { role: "tab", "aria-selected": S.tab === id ? "true" : "false", class: "tab", onclick: () => {
      S.tab = id; store.set("tab", id); render(); toTop();
    } }, t(key))));
}
function section(title, ...kids) { return h("section", { class: "card" }, h("h2", { class: "sec" }, title), ...kids); }
function item(main, sub, right, cls = "") {
  return h("li", { class: "item " + cls }, h("div", { class: "who" }, h("div", { class: "name" }, main),
    sub ? h("div", { class: "bills" }, sub) : null), right != null ? h("div", { class: "amt" }, right) : null);
}

function monthView() {
  const d = S.data, members = d.members.filter((m) => m.status === "active");
  show(header(), tabs(),
    h("div", { class: "summary" },
      h("div", { class: "stat" }, h("span", { class: "muted small" }, t("month", { p: d.period_label })),
        h("b", {}, t("paidOf", { a: d.paid.length, b: d.paid.length + d.unpaid.length }))),
      h("div", { class: "stat" }, h("span", { class: "muted small" }, t("total")), h("b", {}, money(d.total_unpaid)))),
    h("p", { class: "muted small" }, t("due", { d: d.due_day }), d.fee ? " · " + t("feeIs", { f: money(d.fee) }) : ""),
    section(t("unpaidList"), d.unpaid.length ? h("ul", { class: "list" }, d.unpaid.map((u) =>
      item(u.name, u.bills.map(billText).join(" · "), money(u.total), "owe"))) : h("p", { class: "muted" }, t("nobodyOwes"))),
    section(t("paidList"), d.paid.length ? h("ul", { class: "list" }, d.paid.map((n) => item("✅ " + n, null, null)))
      : h("p", { class: "muted" }, t("nobodyPaid"))),
    members.length ? null : h("p", { class: "muted center" }, t("noMembers")), footer());
}

function fundView() {
  const d = S.data, f = d.fund;
  const label = (x) => x.kind === "expense" ? x.title : x.kind === "unassigned" ? t("unassigned")
    : t(x.kind === "cash" ? "cashFrom" : "feeFrom", { n: x.title });
  show(header(), tabs(),
    h("div", { class: "summary three" },
      h("div", { class: "stat" }, h("span", { class: "muted small" }, t("income")), h("b", { class: "pos" }, money(f.income))),
      h("div", { class: "stat" }, h("span", { class: "muted small" }, t("expense")), h("b", { class: "neg" }, money(f.expense))),
      h("div", { class: "stat" }, h("span", { class: "muted small" }, t("balance")), h("b", {}, money(f.balance)))),
    section(t("byMonth"), h("table", { class: "months" },
      h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, t("in")), h("th", {}, t("out")))),
      h("tbody", {}, d.months.map((m) => h("tr", {}, h("td", {}, m.period_label),
        h("td", { class: "pos" }, m.collected ? money(m.collected) : "–"), h("td", { class: "neg" }, m.spent ? money(m.spent) : "–")))))),
    section(t("history"), d.history.length ? h("ul", { class: "list" }, d.history.map((x) =>
      item(label(x), x.at, (x.type === "in" ? "+" : "−") + money(x.amount), x.type === "in" ? "in" : "out")))
      : h("p", { class: "muted" }, t("noHistory"))),
    footer());
}

function membersView() {
  const d = S.data, ms = d.members.filter((m) => m.status !== "left");
  show(header(), tabs(),
    section(`${t("tabMembers")} (${ms.length})`, ms.length ? h("ul", { class: "list" }, ms.map((m) => item(
      m.name + (m.nickname ? ` (${m.nickname})` : ""),
      [m.shirt_number ? t("shirt", { n: m.shirt_number }) : null, m.status === "paused" ? t("paused") : null,
       t("feeIs", { f: money(m.fee) })].filter(Boolean).join(" · "),
      m.owes ? t("owes", { a: money(m.owes) }) : t("allClear"), m.owes ? "owe" : "ok"))) : h("p", { class: "muted" }, t("noMembers"))),
    footer());
}

function listView() {
  const d = S.data;
  if (!d.has_bank) return show(header(), tabs(), h("div", { class: "card note" }, t("noBank")), footer());
  if (!d.unpaid.length) return show(header(), tabs(), h("div", { class: "empty" }, h("div", { class: "big" }, "⚽"),
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

  show(header(), tabs(),
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

function render() {
  if (S.intent) return qrView();
  if (!S.data) return;
  ({ month: monthView, fund: fundView, members: membersView }[S.tab] || listView)();
}
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
    if (saved && saved.code) { S.intent = saved; qrView(); poll(); } else render();
  } catch (err) { S.lang = S.lang || "vi"; messageView(err.message, err.message !== "notFound"); }
}

window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); S.install = e; render(); });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js", { scope: "./" }).catch(() => {});
load();
