"""
slime 的极简 Web 应用（单文件、自包含）——参考 agent_daping 的页面布局。

复用 slime.ask() 跑同一套 Agent 循环，把每轮的思考/工具调用/工具返回/最终回答
渲染成与 agent_daping 一致的轨迹卡片 + 推荐媒资卡片界面。

特点：
  - 只用 Python 标准库（http.server），不引入新依赖、不改动任何原有文件
  - 复用现有 slime 模块与 .env 配置
  - 默认监听 127.0.0.1（仅本机访问），无鉴权——仅作本地体验用

运行：
    python slime_app.py                  # http://127.0.0.1:8099
    SLIME_APP_PORT=9000 python slime_app.py
    SLIME_APP_HOST=0.0.0.0 python slime_app.py   # 需要外部访问时（注意无鉴权）
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import slime

# 关闭 slime 的逐轮调试日志/日志文件，保持服务端安静
slime.DEBUG = False
slime.VERBOSE = False
slime.LOG_FILE = None

HOST = os.getenv("SLIME_APP_HOST", "127.0.0.1")
PORT = int(os.getenv("SLIME_APP_PORT", "8099"))


# ============================================================
# 把 slime 的 trace 拍平成前端可渲染的步骤列表
# ============================================================
def build_steps(trace: dict, answer: str) -> list[dict]:
    steps: list[dict] = []
    for rd in trace.get("rounds", []):
        if rd.get("thinking"):
            steps.append({"type": "thinking", "content": rd["thinking"]})
        # 中间轮的 assistant 文本（带工具调用时）作为"思考"展示
        if rd.get("content") and rd.get("tool_calls"):
            steps.append({"type": "thought", "content": rd["content"]})
        for tc in rd.get("tool_calls") or []:
            steps.append({"type": "tool_call", "tool_name": tc.get("name", ""),
                          "tool_args": tc.get("args", {})})
            steps.append({"type": "tool_result", "tool_name": tc.get("name", ""),
                          "content": tc.get("result", {})})
    steps.append({"type": "answer", "content": answer})
    return steps


# ============================================================
# 前端页面（布局与 agent_daping/static/index.html 保持一致）
# ============================================================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>影视问答体验（slime）</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  height: 100vh; display: flex; background: #f5f5f5; color: #333; }
.container { display: flex; width: 100%; height: 100%; }

/* History Panel */
.history-panel { width: 280px; min-width: 280px; background: #fff; border-right: 1px solid #e0e0e0;
  display: flex; flex-direction: column; overflow: hidden; }
.history-header { padding: 16px; font-size: 16px; font-weight: 600; border-bottom: 1px solid #e0e0e0;
  display: flex; align-items: center; justify-content: space-between; }
.history-header .new-chat { font-size: 13px; color: #1a73e8; cursor: pointer; font-weight: 500; }
.history-list { flex: 1; overflow-y: auto; padding: 8px; }
.history-empty { display: flex; align-items: center; justify-content: center; height: 100%;
  color: #999; font-size: 14px; text-align: center; padding: 16px; }
.history-item { padding: 12px; padding-right: 32px; border-radius: 8px; cursor: pointer; margin-bottom: 4px;
  font-size: 14px; color: #555; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  transition: background 0.15s; position: relative; }
.history-item:hover { background: #f0f0f0; }
.history-item.active { background: #e8f0fe; color: #1a73e8; }
.history-item .history-delete { display: none; position: absolute; right: 8px; top: 50%;
  transform: translateY(-50%); width: 20px; height: 20px; line-height: 20px; text-align: center;
  border-radius: 50%; font-size: 14px; color: #999; background: transparent; border: none; cursor: pointer; }
.history-item:hover .history-delete { display: block; }
.history-item .history-delete:hover { color: #e53935; background: #ffebee; }

/* Chat Area */
.chat-area { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.chat-messages { flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; }
.chat-welcome { margin: auto; color: #999; font-size: 16px; }
.chat-input-bar { padding: 16px 24px; border-top: 1px solid #e0e0e0; background: #fff;
  display: flex; gap: 12px; align-items: center; }
.chat-input-bar input { flex: 1; padding: 12px 16px; border: 1px solid #ddd; border-radius: 8px;
  font-size: 14px; outline: none; transition: border-color 0.2s; }
.chat-input-bar input:focus { border-color: #1a73e8; }
.chat-input-bar input:disabled { background: #f5f5f5; cursor: not-allowed; }
.chat-input-bar button { padding: 12px 24px; background: #1a73e8; color: #fff; border: none;
  border-radius: 8px; font-size: 14px; cursor: pointer; white-space: nowrap; transition: background 0.15s; }
.chat-input-bar button:hover:not(:disabled) { background: #1557b0; }
.chat-input-bar button:disabled { background: #a0c4f1; cursor: not-allowed; }

.user-query { margin-bottom: 20px; padding: 12px 16px; background: #1a73e8; color: #fff;
  border-radius: 8px; font-size: 14px; align-self: flex-end; max-width: 80%; }

/* Trace Steps */
.trace-step { margin-bottom: 16px; padding: 12px 16px; border-radius: 8px; font-size: 14px;
  line-height: 1.6; position: relative; max-width: 90%; }
.trace-step.thought { background: #f8f9fa; border-left: 3px solid #9e9e9e; cursor: pointer; }
.trace-step.thought .thought-content { white-space: pre-wrap; max-height: 60px; overflow: hidden;
  transition: max-height 0.3s ease; }
.trace-step.thought .thought-content.expanded { max-height: none; }
.trace-step.thought .thought-toggle { font-size: 12px; color: #666; margin-top: 4px; }
.trace-step.thinking { background: #f3e5f5; border-left: 3px solid #9c27b0; cursor: pointer; }
.trace-step.thinking .thinking-summary { color: #9c27b0; font-style: italic; }
.trace-step.thinking .thought-content { white-space: pre-wrap; display: none; margin-top: 8px;
  padding: 8px; background: rgba(0,0,0,0.03); border-radius: 4px; font-size: 13px; color: #555; }
.trace-step.thinking .thought-content.expanded { display: block; }
.trace-step.tool_call { background: #e3f2fd; border-left: 3px solid #2196f3; cursor: pointer; }
.trace-step.tool_result { background: #e8f5e9; border-left: 3px solid #4caf50; cursor: pointer; }
.trace-step.answer { background: #fff3e0; border-left: 3px solid #ff9800; font-size: 15px; }
.trace-step.answer .answer-content { line-height: 1.8; }
.trace-step.answer .answer-content p { margin-bottom: 8px; }
.trace-step.answer .answer-content strong { font-weight: 600; }
.trace-step.recommendations { background: #e8f5e9; border-left: 3px solid #66bb6a; font-size: 15px; }
.trace-step.recommendations .rec-title { font-size: 13px; font-weight: 600; color: #388e3c; margin-bottom: 8px; }
.trace-step.recommendations .rec-list { list-style: none; padding: 0; }
.trace-step.recommendations .rec-list li { padding: 6px 10px; margin-bottom: 4px;
  background: rgba(255,255,255,0.7); border-radius: 4px; font-size: 14px; }
.trace-step .step-label { font-size: 12px; font-weight: 600; text-transform: uppercase; margin-bottom: 4px; opacity: 0.7; }
.trace-step .step-detail { display: none; margin-top: 8px; padding: 8px; background: rgba(0,0,0,0.03);
  border-radius: 4px; font-family: monospace; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
.trace-step .step-detail.expanded { display: block; }
.loading-step { color: #999; font-style: italic; padding: 12px 16px; }
</style>
</head>
<body>
<div class="container">
  <aside class="history-panel">
    <div class="history-header">
      <span>历史会话</span>
      <span class="new-chat" id="newChat">+ 新对话</span>
    </div>
    <div class="history-list" id="historyList">
      <div class="history-empty" id="historyEmpty">暂无历史会话</div>
    </div>
  </aside>
  <main class="chat-area">
    <div class="chat-messages" id="chatMessages">
      <div class="chat-welcome" id="chatWelcome">输入问题，开始影视问答体验</div>
    </div>
    <div class="chat-input-bar">
      <input type="text" id="queryInput" placeholder="请输入你的影视问题..." aria-label="问题输入框">
      <button id="sendBtn" aria-label="发送">发送</button>
    </div>
  </main>
</div>

<script>
const container = document.getElementById('chatMessages');
const welcome = document.getElementById('chatWelcome');
const input = document.getElementById('queryInput');
const sendBtn = document.getElementById('sendBtn');

// ===== 历史会话（localStorage）=====
const STORAGE_KEY = 'slime_ui_history';
let activeId = null;

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'); } catch { return []; }
}
function saveHistory(h) { localStorage.setItem(STORAGE_KEY, JSON.stringify(h)); }
function addSession(s) { const h = loadHistory(); h.unshift(s); saveHistory(h); renderHistory(); }

function renderHistory() {
  const h = loadHistory();
  const list = document.getElementById('historyList');
  list.innerHTML = '';
  if (!h.length) { list.innerHTML = '<div class="history-empty">暂无历史会话</div>'; return; }
  for (const s of h) {
    const item = document.createElement('div');
    item.className = 'history-item' + (s.id === activeId ? ' active' : '');
    item.textContent = s.title || s.query;
    item.addEventListener('click', () => openSession(s.id));
    const del = document.createElement('button');
    del.className = 'history-delete'; del.textContent = '×';
    del.addEventListener('click', (e) => { e.stopPropagation(); deleteSession(s.id); });
    item.appendChild(del);
    list.appendChild(item);
  }
}
function deleteSession(id) {
  saveHistory(loadHistory().filter(s => s.id !== id));
  if (activeId === id) { activeId = null; clearChat(); }
  renderHistory();
}
function openSession(id) {
  const s = loadHistory().find(x => x.id === id);
  if (!s) return;
  activeId = id;
  clearChat();
  renderQuery(s.query);
  for (const step of s.steps) renderStep(step);
  renderHistory();
}
function clearChat() { container.innerHTML = ''; }

// ===== 渲染 =====
function renderQuery(query) {
  if (welcome && welcome.parentNode) welcome.remove();
  const el = document.createElement('div');
  el.className = 'user-query';
  el.textContent = query;
  container.appendChild(el);
}

function renderMarkdown(text) {
  let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/^[\-\*]\s+(.+)$/gm, '<li>$1</li>');
  html = html.replace(/^\d+[\.\、]\s*(.+)$/gm, '<li>$1</li>');
  html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');
  html = html.replace(/\n{2,}/g, '</p><p>');
  html = html.replace(/\n/g, '<br>');
  html = '<p>' + html + '</p>';
  return html.replace(/<p>\s*<\/p>/g, '');
}

function renderRecommendations(recText) {
  const recEl = document.createElement('div');
  recEl.className = 'trace-step recommendations';
  const recTitle = document.createElement('div');
  recTitle.className = 'rec-title';
  recTitle.textContent = '推荐媒资卡片';
  recEl.appendChild(recTitle);
  const recList = document.createElement('ol');
  recList.className = 'rec-list';
  for (const line of recText.split('\n').slice(1)) {
    const m = line.match(/^\s*(?:\d+[\.\、]|\-)\s*(.+)/);
    if (m && m[1].trim()) { const li = document.createElement('li'); li.textContent = m[1].trim(); recList.appendChild(li); }
  }
  recEl.appendChild(recList);
  container.appendChild(recEl);
}

function renderStep(step) {
  const el = document.createElement('div');
  el.className = `trace-step ${step.type}`;
  const label = document.createElement('div');
  label.className = 'step-label';
  let summary = '', detail = null;

  switch (step.type) {
    case 'thinking':
      label.textContent = '深度思考'; el.appendChild(label);
      const ts = document.createElement('div'); ts.className = 'thinking-summary'; ts.textContent = 'thinking...'; el.appendChild(ts);
      const tc = document.createElement('div'); tc.className = 'thought-content'; tc.textContent = step.content || ''; el.appendChild(tc);
      el.addEventListener('click', () => tc.classList.toggle('expanded'));
      container.appendChild(el); container.scrollTop = container.scrollHeight; return;
    case 'thought':
      label.textContent = '思考'; el.appendChild(label);
      const th = document.createElement('div'); th.className = 'thought-content'; th.textContent = step.content || ''; el.appendChild(th);
      const tg = document.createElement('div'); tg.className = 'thought-toggle'; tg.textContent = '点击展开/折叠'; el.appendChild(tg);
      el.addEventListener('click', () => th.classList.toggle('expanded'));
      container.appendChild(el); container.scrollTop = container.scrollHeight; return;
    case 'tool_call':
      label.textContent = '工具调用';
      summary = `${step.tool_name}(${Object.keys(step.tool_args || {}).join(', ')})`;
      detail = JSON.stringify(step.tool_args, null, 2); break;
    case 'tool_result':
      label.textContent = '工具结果';
      summary = `${step.tool_name} 返回结果`;
      detail = JSON.stringify(step.content, null, 2); break;
    case 'answer':
      label.textContent = '最终回答'; el.appendChild(label);
      const raw = step.content || '';
      const recMatch = raw.match(/【推荐媒资卡片】[\s\S]*/);
      const mainText = recMatch ? raw.slice(0, recMatch.index).trim() : raw;
      const ac = document.createElement('div'); ac.className = 'answer-content';
      ac.innerHTML = renderMarkdown(mainText); el.appendChild(ac);
      container.appendChild(el);
      if (recMatch) renderRecommendations(recMatch[0]);
      container.scrollTop = container.scrollHeight; return;
  }

  el.appendChild(label);
  const contentEl = document.createElement('div'); contentEl.textContent = summary; el.appendChild(contentEl);
  if (detail !== null) {
    const d = document.createElement('div'); d.className = 'step-detail'; d.textContent = detail; el.appendChild(d);
    el.addEventListener('click', () => d.classList.toggle('expanded'));
  }
  container.appendChild(el); container.scrollTop = container.scrollHeight;
}

// ===== 发送 =====
async function send() {
  const query = input.value.trim();
  if (!query) return;
  input.value = '';
  input.disabled = true; sendBtn.disabled = true; sendBtn.textContent = '思考中...';

  activeId = null;
  clearChat();
  renderQuery(query);
  const loading = document.createElement('div');
  loading.className = 'loading-step'; loading.textContent = '正在检索与推理…';
  container.appendChild(loading);

  try {
    const resp = await fetch('/ask', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query })
    });
    const data = await resp.json();
    loading.remove();
    if (data.error) {
      const e = document.createElement('div'); e.className = 'trace-step';
      e.textContent = '出错：' + data.error; container.appendChild(e);
    } else {
      for (const step of data.steps) renderStep(step);
      addSession({ id: Date.now().toString(), title: query.slice(0, 30), query, steps: data.steps, createdAt: Date.now() });
    }
  } catch (err) {
    loading.remove();
    const e = document.createElement('div'); e.className = 'trace-step'; e.textContent = '请求失败：' + err; container.appendChild(e);
  } finally {
    input.disabled = false; sendBtn.disabled = false; sendBtn.textContent = '发送'; input.focus();
  }
}

sendBtn.addEventListener('click', send);
input.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
document.getElementById('newChat').addEventListener('click', () => { activeId = null; clearChat();
  container.innerHTML = '<div class="chat-welcome">输入问题，开始影视问答体验</div>'; renderHistory(); });
renderHistory();
</script>
</body>
</html>
"""


# ============================================================
# HTTP 服务
# ============================================================
class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/config":
            self._json(200, {"model": slime.LLM_MODEL})
        elif self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/ask":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            query = (payload.get("query") or "").strip()
            if not query:
                self._json(400, {"error": "query 不能为空"})
                return
            trace: dict = {}
            answer = slime.ask(query, trace=trace)
            self._json(200, {"answer": answer, "steps": build_steps(trace, answer)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            self._json(500, {"error": str(e)})

    def log_message(self, fmt, *args):  # 安静日志
        sys.stderr.write("[slime_app] " + (fmt % args) + "\n")


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"slime web app 已启动： http://{HOST}:{PORT}  （模型: {slime.LLM_MODEL}）", file=sys.stderr)
    if HOST not in ("127.0.0.1", "localhost"):
        print("⚠ 正在监听非本机地址且无鉴权，请勿暴露到公网。", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()
