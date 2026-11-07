#!/usr/bin/env python3
"""Reverse-proxy + chat UI for the Hermes API server.

Serves a browser chat window at GET / and reverse-proxies /v1/* and /health
to the real Hermes API server on an internal port.

On Cloud Run the proxy listens on $PORT (8080) so the chat UI is the first
thing a grader sees when they open the Cloud Run URL.  Hermes itself runs
on an internal port ($HERMES_PORT, default 8081).

Usage:  python3 web_ui.py [--port 8080] [--backend 8081]

Source: CSCI 599 Assignment 3, "Grader Access and Local Gemma Video", Part 1 step 1.
Kairo change: the page title and header name the agent (Kairo) instead of "Hermes Agent".
"""
import http.server
import json
import os
import sys
import urllib.request
import urllib.error
import threading

BACKEND_PORT = int(os.environ.get("HERMES_PORT",
    sys.argv[sys.argv.index("--backend") + 1] if "--backend" in sys.argv else "8081"))
LISTEN_PORT  = int(os.environ.get("PORT",
    sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else "8080"))

CHAT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kairo (Hermes Agent) — CSCI 599 A3</title>
<style>
  :root {
    --bg: #f7f7f8; --surface: #ffffff; --text: #1a1a2e;
    --muted: #6b7280; --accent: #2563eb; --accent-hover: #1d4ed8;
    --user-bg: #2563eb; --user-text: #fff;
    --bot-bg: #e5e7eb; --bot-text: #1a1a2e;
    --border: #e5e7eb; --input-bg: #fff;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    --mono: "SF Mono", "Fira Code", "Fira Mono", Menlo, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f172a; --surface: #1e293b; --text: #e2e8f0;
      --muted: #94a3b8; --accent: #3b82f6; --accent-hover: #60a5fa;
      --user-bg: #3b82f6; --user-text: #fff;
      --bot-bg: #334155; --bot-text: #e2e8f0;
      --border: #334155; --input-bg: #1e293b;
    }
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; font-family: var(--font); background: var(--bg); color: var(--text); }

  .chat-container { display: flex; flex-direction: column; height: 100%; max-width: 800px; margin: 0 auto; }

  /* header */
  .header {
    padding: 14px 20px; background: var(--surface); border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 12px; flex-shrink: 0;
  }
  .header h1 { font-size: 17px; font-weight: 600; }
  .header .subtitle { font-size: 12px; color: var(--muted); }
  .status { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; flex-shrink: 0; }
  .status.offline { background: #ef4444; }

  /* api key bar */
  .key-bar {
    padding: 8px 20px; background: var(--surface); border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 8px; flex-shrink: 0;
  }
  .key-bar label { font-size: 12px; color: var(--muted); white-space: nowrap; }
  .key-bar input {
    flex: 1; padding: 6px 10px; font-size: 13px; font-family: var(--mono);
    border: 1px solid var(--border); border-radius: 6px;
    background: var(--input-bg); color: var(--text);
  }
  .key-bar .save-btn {
    padding: 6px 14px; font-size: 12px; border: none; border-radius: 6px; cursor: pointer;
    background: var(--accent); color: #fff; font-weight: 500;
  }
  .key-bar .save-btn:hover { background: var(--accent-hover); }

  /* messages */
  .messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 12px; }
  .msg { max-width: 85%; padding: 10px 14px; border-radius: 16px; font-size: 14px; line-height: 1.5; word-wrap: break-word; }
  .msg.user { align-self: flex-end; background: var(--user-bg); color: var(--user-text); border-bottom-right-radius: 4px; }
  .msg.bot  { align-self: flex-start; background: var(--bot-bg); color: var(--bot-text); border-bottom-left-radius: 4px; }
  .msg.bot pre { background: rgba(0,0,0,.1); padding: 8px; border-radius: 6px; overflow-x: auto; font-size: 12px; margin: 6px 0; font-family: var(--mono); }
  .msg.bot code { font-family: var(--mono); font-size: 13px; }
  .msg.system { align-self: center; font-size: 12px; color: var(--muted); background: none; }
  .typing { align-self: flex-start; padding: 10px 18px; background: var(--bot-bg); border-radius: 16px; border-bottom-left-radius: 4px; }
  .typing span { display: inline-block; width: 7px; height: 7px; background: var(--muted); border-radius: 50%; animation: bounce .6s infinite alternate; margin: 0 2px; }
  .typing span:nth-child(2) { animation-delay: .2s; }
  .typing span:nth-child(3) { animation-delay: .4s; }
  @keyframes bounce { to { transform: translateY(-5px); opacity: .4; } }

  /* input */
  .input-bar {
    padding: 12px 20px; background: var(--surface); border-top: 1px solid var(--border);
    display: flex; align-items: flex-end; gap: 8px; flex-shrink: 0;
  }
  .input-bar textarea {
    flex: 1; padding: 10px 14px; font-size: 14px; font-family: var(--font);
    border: 1px solid var(--border); border-radius: 12px; resize: none;
    background: var(--input-bg); color: var(--text); min-height: 44px; max-height: 120px;
    line-height: 1.4;
  }
  .input-bar textarea:focus { outline: 2px solid var(--accent); border-color: transparent; }
  .send-btn {
    width: 44px; height: 44px; border: none; border-radius: 50%; cursor: pointer;
    background: var(--accent); color: #fff; display: flex; align-items: center; justify-content: center;
    flex-shrink: 0; transition: background .15s;
  }
  .send-btn:hover { background: var(--accent-hover); }
  .send-btn:disabled { opacity: .4; cursor: not-allowed; }
  .send-btn svg { width: 20px; height: 20px; }
</style>
</head>
<body>

<div class="chat-container">
  <div class="header">
    <div class="status" id="status"></div>
    <div>
      <h1>Kairo (Hermes Agent)</h1>
      <div class="subtitle">CSCI 599 Assignment 3 — Agentic AI</div>
    </div>
  </div>

  <div class="key-bar">
    <label>API Key:</label>
    <input type="password" id="apiKey" placeholder="Paste API_SERVER_KEY here…">
    <button class="save-btn" onclick="saveKey()">Save</button>
  </div>

  <div class="messages" id="messages">
    <div class="msg system">Enter your API key above, then start chatting.</div>
  </div>

  <div class="input-bar">
    <textarea id="input" placeholder="Type a message…" rows="1" onkeydown="handleKey(event)" oninput="autoGrow(this)"></textarea>
    <button class="send-btn" id="sendBtn" onclick="send()" disabled>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
    </button>
  </div>
</div>

<script>
const messagesEl = document.getElementById('messages');
const inputEl    = document.getElementById('input');
const sendBtn    = document.getElementById('sendBtn');
const statusEl   = document.getElementById('status');
const apiKeyEl   = document.getElementById('apiKey');

let conversationHistory = [];
let sending = false;

try { const k = sessionStorage.getItem('apiKey'); if (k) apiKeyEl.value = k; } catch(e) {}

function saveKey() {
  try { sessionStorage.setItem('apiKey', apiKeyEl.value); } catch(e) {}
  addMsg('system', 'API key saved for this session.');
  checkHealth();
}

function getKey() { return apiKeyEl.value.trim(); }

async function checkHealth() {
  try {
    const r = await fetch('/health', { signal: AbortSignal.timeout(5000) });
    statusEl.className = r.ok ? 'status' : 'status offline';
  } catch { statusEl.className = 'status offline'; }
}
checkHealth();
setInterval(checkHealth, 30000);

function addMsg(role, text) {
  const typ = document.querySelector('.typing');
  if (typ) typ.remove();
  const div = document.createElement('div');
  div.className = 'msg ' + (role === 'user' ? 'user' : role === 'system' ? 'system' : 'bot');
  if (role === 'bot') { div.innerHTML = formatMarkdown(text); } else { div.textContent = text; }
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function showTyping() {
  const div = document.createElement('div');
  div.className = 'typing';
  div.innerHTML = '<span></span><span></span><span></span>';
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function formatMarkdown(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;")
    .replace(/```(\w*)\n?([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/\n/g, '<br>');
}

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  sendBtn.disabled = !el.value.trim() || sending;
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
}

async function send() {
  const text = inputEl.value.trim();
  if (!text || sending) return;
  const key = getKey();
  if (!key) { addMsg('system', 'Please enter your API key first.'); return; }

  sending = true;
  sendBtn.disabled = true;
  inputEl.value = '';
  autoGrow(inputEl);

  addMsg('user', text);
  conversationHistory.push({ role: 'user', content: text });
  showTyping();

  try {
    const res = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key },
      body: JSON.stringify({ model: 'hermes-agent', messages: conversationHistory })
    });
    const data = await res.json();
    if (!res.ok) {
      addMsg('system', 'Error ' + res.status + ': ' + (data.detail || data.error?.message || JSON.stringify(data)));
    } else {
      const reply = data.choices?.[0]?.message?.content || '(empty response)';
      conversationHistory.push({ role: 'assistant', content: reply });
      addMsg('bot', reply);
    }
  } catch (err) {
    addMsg('system', 'Network error: ' + err.message);
  }
  sending = false;
  sendBtn.disabled = !inputEl.value.trim();
}

inputEl.addEventListener('input', () => { sendBtn.disabled = !inputEl.value.trim() || sending; });
inputEl.focus();
</script>
</body>
</html>"""


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Serve chat UI at / and reverse-proxy everything else to Hermes."""

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_chat()
        else:
            self._proxy("GET")

    def do_POST(self):   self._proxy("POST")
    def do_PUT(self):    self._proxy("PUT")
    def do_DELETE(self): self._proxy("DELETE")
    def do_OPTIONS(self): self._proxy("OPTIONS")

    def _serve_chat(self):
        body = CHAT_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method):
        url = f"http://127.0.0.1:{BACKEND_PORT}{self.path}"
        body = None
        cl = self.headers.get("Content-Length")
        if cl:
            body = self.rfile.read(int(cl))

        req = urllib.request.Request(url, data=body, method=method)
        for h in ("Authorization", "Content-Type", "Accept"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        msg = (fmt % args).replace('"', "'")
        sys.stderr.write(f'{{"severity":"INFO","component":"web_ui","message":"{msg}","ts":"{__import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}"}}\n')


class ThreadedServer(http.server.HTTPServer):
    """Handle each request in a thread so long API calls don't block the UI."""
    def process_request(self, request, client_address):
        t = threading.Thread(target=self.process_request_thread, args=(request, client_address))
        t.daemon = True
        t.start()

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


if __name__ == "__main__":
    server = ThreadedServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    print(f'{{"severity":"INFO","component":"web_ui","message":"listening on 0.0.0.0:{LISTEN_PORT} -> backend 127.0.0.1:{BACKEND_PORT}","ts":"{__import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}"}}', file=sys.stderr)
    server.serve_forever()
