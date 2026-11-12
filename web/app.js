// ReceiptLens web client: Cognito hosted UI (authorization code + PKCE),
// direct-to-S3 uploads with a presigned POST, and the expenses list.
//
// Session rules:
//  - Tokens live in memory only. A refresh, a new tab or closing the tab means
//    signing in again; no tokens are left in browser storage.
//  - "Sign in" always shows the password form: it first ends any Cognito
//    session (the hosted UI cookie), then starts a fresh login.
//  - 15 minutes without activity signs the user out.
const cfg = window.RECEIPTLENS_CONFIG;
const $ = (id) => document.getElementById(id);
const IDLE_LIMIT_MS = 15 * 60 * 1000;

// Only the short-lived PKCE values and one-shot flags survive the redirect to
// Cognito and back; they are removed as soon as they are used.
const flow = {
  get: (k) => { try { return sessionStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { sessionStorage.setItem(k, v); } catch { /* storage blocked */ } },
  take: (k) => { const v = flow.get(k); try { sessionStorage.removeItem(k); } catch { /* ignore */ } return v; },
};

let session = null; // { accessToken, email, exp }

// ---------- auth (PKCE) ----------
const b64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

function claims(token) {
  try { return JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); }
  catch { return null; }
}

// Step 1 of "Sign in": end any existing Cognito session so the password form is shown.
function signIn() {
  flow.set("login_after_logout", "1");
  const params = new URLSearchParams({ client_id: cfg.clientId, logout_uri: cfg.redirectUri });
  location.assign(`${cfg.authDomain}/logout?${params}`);
}

// Step 2: back from /logout, start the authorization-code + PKCE login.
async function startLogin() {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const state = b64url(crypto.getRandomValues(new Uint8Array(16)));
  flow.set("pkce_verifier", verifier);
  flow.set("oauth_state", state);
  const params = new URLSearchParams({
    response_type: "code", client_id: cfg.clientId, redirect_uri: cfg.redirectUri,
    scope: "openid email", code_challenge_method: "S256", code_challenge: challenge, state,
  });
  location.assign(`${cfg.authDomain}/oauth2/authorize?${params}`);
}

// Step 3: back from Cognito with ?code=..., exchange it for tokens (kept in memory).
async function finishLogin() {
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  if (!code) return false;
  history.replaceState({}, "", cfg.redirectUri);
  const verifier = flow.take("pkce_verifier");
  if (!verifier || params.get("state") !== flow.take("oauth_state")) throw new Error("Sign-in expired, please try again.");
  const res = await fetch(`${cfg.authDomain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code", client_id: cfg.clientId, code,
      redirect_uri: cfg.redirectUri, code_verifier: verifier,
    }),
  });
  if (!res.ok) throw new Error(`Sign-in failed (${res.status}), please try again.`);
  const tokens = await res.json();
  const access = claims(tokens.access_token);
  session = {
    accessToken: tokens.access_token,
    email: claims(tokens.id_token)?.email || "",
    exp: access ? access.exp * 1000 : 0,
  };
  return true;
}

// Sign out: forget the tokens and end the Cognito session.
function signOut(reason) {
  session = null;
  if (reason) flow.set("signed_out_reason", reason);
  const params = new URLSearchParams({ client_id: cfg.clientId, logout_uri: cfg.redirectUri });
  location.assign(`${cfg.authDomain}/logout?${params}`);
}

function token() {
  if (!session) return null;
  if (session.exp <= Date.now() + 30000) { signOut("Your session expired. Please sign in again."); return null; }
  return session.accessToken;
}

// ---------- inactivity timeout ----------
let idleTimer;
function resetIdleTimer() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => signOut("You were signed out after 15 minutes without activity."), IDLE_LIMIT_MS);
}
function watchActivity() {
  ["pointerdown", "keydown", "scroll", "touchstart"].forEach((ev) =>
    window.addEventListener(ev, resetIdleTimer, { passive: true }));
  resetIdleTimer();
}

// ---------- API ----------
async function api(path, options = {}) {
  const res = await fetch(`${cfg.apiUrl}${path}`, {
    ...options,
    headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (res.status === 401) { signOut("Your session expired. Please sign in again."); throw new Error("Signed out"); }
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
function showSignedOut(message) {
  $("login").hidden = false;
  $("logout").hidden = true;
  $("app").hidden = true;
  $("signed-out").hidden = false;
  if (message) $("signed-out").textContent = message;
}

async function main() {
  $("login").onclick = signIn;
  $("logout").onclick = () => signOut();

  // Back from /logout as part of "Sign in": continue to the login form.
  if (flow.take("login_after_logout")) { await startLogin(); return; }

  let signedIn = false;
  try { signedIn = await finishLogin(); } catch (err) { showSignedOut(err.message); return; }
  if (!signedIn || !token()) { showSignedOut(flow.take("signed_out_reason")); return; }

  $("login").hidden = true;
  $("logout").hidden = false;
  $("signed-out").hidden = true;
  $("app").hidden = false;
  $("user").textContent = session.email;
  watchActivity();

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
