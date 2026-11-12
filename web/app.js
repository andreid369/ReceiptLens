// ReceiptLens web client: Cognito hosted UI (authorization code + PKCE),
// direct-to-S3 uploads with a presigned POST, and the expenses list.
const cfg = window.RECEIPTLENS_CONFIG;
const $ = (id) => document.getElementById(id);
const store = window.sessionStorage;

// ---------- auth (PKCE) ----------
const b64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function login() {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const state = b64url(crypto.getRandomValues(new Uint8Array(16)));
  store.setItem("pkce_verifier", verifier);
  store.setItem("oauth_state", state);
  const params = new URLSearchParams({
    response_type: "code", client_id: cfg.clientId, redirect_uri: cfg.redirectUri,
    scope: "openid email", code_challenge_method: "S256", code_challenge: challenge, state,
  });
  location.assign(`${cfg.authDomain}/oauth2/authorize?${params}`);
}

async function handleRedirect() {
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  if (!code) return;
  history.replaceState({}, "", cfg.redirectUri);
  if (params.get("state") !== store.getItem("oauth_state")) throw new Error("state mismatch");
  const res = await fetch(`${cfg.authDomain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code", client_id: cfg.clientId, code,
      redirect_uri: cfg.redirectUri, code_verifier: store.getItem("pkce_verifier"),
    }),
  });
  if (!res.ok) throw new Error(`token exchange failed (${res.status})`);
  const tokens = await res.json();
  store.setItem("access_token", tokens.access_token);
  store.setItem("id_token", tokens.id_token);
  store.removeItem("pkce_verifier");
  store.removeItem("oauth_state");
}

function logout() {
  store.clear();
  const params = new URLSearchParams({ client_id: cfg.clientId, logout_uri: cfg.redirectUri });
  location.assign(`${cfg.authDomain}/logout?${params}`);
}

function claims(token) {
  try { return JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); }
  catch { return null; }
}

function token() {
  const t = store.getItem("access_token");
  const c = t && claims(t);
  return c && c.exp * 1000 > Date.now() + 30000 ? t : null;
}

// ---------- API ----------
async function api(path, options = {}) {
  const res = await fetch(`${cfg.apiUrl}${path}`, {
    ...options,
    headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (res.status === 401) { store.clear(); location.reload(); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
  return res;
}

async function upload(file) {
  const status = $("upload-status");
  status.textContent = "Requesting upload URL…";
  const { doc_id, upload: post, max_bytes } = await (await api("/uploads", {
    method: "POST", body: JSON.stringify({ filename: file.name, content_type: file.type }),
  })).json();
  if (file.size > max_bytes) throw new Error(`File is larger than ${(max_bytes / 1e6).toFixed(2)} MB`);

  const form = new FormData();
  Object.entries(post.fields).forEach(([k, v]) => form.append(k, v));
  form.append("file", file); // must be the last field
  status.textContent = "Uploading…";
  const s3 = await fetch(post.url, { method: "POST", body: form });
  if (!s3.ok) throw new Error(`Upload rejected by S3 (${s3.status})`);

  status.textContent = "Uploaded. Extracting…";
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    const item = await (await api(`/expenses/${doc_id}`)).json();
    if (["EXTRACTED", "NEEDS_REVIEW"].includes(item.status)) {
      status.textContent = item.status === "EXTRACTED"
        ? "Done."
        : `Needs review: ${(item.issues || []).join("; ")}`;
      break;
    }
  }
  await loadExpenses();
}

const money = (v, cur) => (v == null ? "" : `${Number(v).toFixed(2)} ${cur || ""}`.trim());

function cell(text, cls) {
  const td = document.createElement("td");
  td.textContent = text ?? "";
  if (cls) td.className = cls;
  return td;
}

async function loadExpenses() {
  const { items } = await (await api("/expenses")).json();
  const body = $("expenses");
  body.replaceChildren();
  for (const item of items) {
    const e = item.expense || {};
    const tr = document.createElement("tr");
    const badge = document.createElement("span");
    badge.className = `status ${item.status}`;
    badge.textContent = item.status.replace("_", " ").toLowerCase();
    const statusCell = cell("");
    statusCell.append(badge);
    tr.append(cell(e.invoice_date), cell(e.supplier_name || item.filename), cell(e.category),
      cell(money(e.total, e.currency), "num"), statusCell);
    body.append(tr);
  }
  if (!items.length) {
    const tr = document.createElement("tr");
    const td = cell("No documents yet. Upload your first receipt above.");
    td.colSpan = 5;
    tr.append(td);
    body.append(tr);
  }
}

async function exportCsv() {
  const blob = await (await api("/export")).blob();
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(blob), download: "receiptlens-expenses.csv",
  });
  a.click();
  URL.revokeObjectURL(a.href);
}

// ---------- wiring ----------
async function main() {
  $("login").onclick = login;
  $("logout").onclick = logout;
  try { await handleRedirect(); } catch (err) { console.error(err); }

  if (!token()) return;
  $("login").hidden = true;
  $("logout").hidden = false;
  $("signed-out").hidden = true;
  $("app").hidden = false;
  $("user").textContent = claims(store.getItem("id_token"))?.email || "";

  $("upload-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const file = $("file").files[0];
    if (!file) return;
    try { await upload(file); } catch (err) { $("upload-status").textContent = err.message; }
  };
  $("refresh").onclick = loadExpenses;
  $("export").onclick = exportCsv;
  await loadExpenses();
}

main();
