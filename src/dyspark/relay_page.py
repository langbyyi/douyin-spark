"""The single HTML page served at ``/login/<token>`` by the web login relay.

Everything dynamic is fetched from ``<page>/state`` and written with
``textContent``; the only server-side substitutions are the (escaped) account name
and the per-response CSP nonce.
"""

from __future__ import annotations

import html

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="spark-token" content="__PANEL_TOKEN__">
<title>抖音登录 · __ACCOUNT__</title>
<style nonce="__NONCE__">
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 0;
       background: #f4f5f7; color: #161823; }
main { max-width: 420px; margin: 0 auto; padding: 16px; }
h1 { font-size: 18px; margin: 8px 0 12px; }
section { background: #fff; border-radius: 12px; padding: 14px; margin-bottom: 12px; }
#status { font-weight: 600; font-size: 16px; }
#status[data-state="success"] { color: #0a8f3c; }
#status[data-state="failed"], #status[data-state="expired"] { color: #d0021b; }
.muted { color: #6b6f7b; font-size: 13px; line-height: 1.5; }
#qr, #screen { display: block; margin: 8px auto; max-width: 100%; }
#qr { width: 240px; height: 240px; image-rendering: pixelated; }
input { box-sizing: border-box; width: 100%; font-size: 16px; padding: 10px; margin: 6px 0;
        border: 1px solid #d0d3da; border-radius: 8px; }
button { font-size: 15px; padding: 10px 14px; border: 0; border-radius: 8px; margin: 4px 4px 4px 0;
         background: #fe2c55; color: #fff; }
button.plain { background: #e8e9ed; color: #161823; }
button:disabled { opacity: .5; cursor: not-allowed; }
label { display: block; font-size: 13px; margin-top: 8px; }
.row { display: flex; gap: 8px; align-items: center; }
.row input { flex: 1; }
[hidden] { display: none !important; }
</style></head>
<body><main>
<h1>抖音登录 · 账号 <span id="account">__ACCOUNT__</span></h1>
<section>
  <div id="status" data-state="starting">正在连接…</div>
  <div id="message" class="muted"></div>
  <div id="route" class="muted"></div>
  <div id="expires" class="muted"></div>
</section>
<section id="qr-box" hidden>
  <img id="qr" alt="抖音登录二维码">
  <button id="reload-qr" class="plain" type="button" hidden>重新加载二维码图片</button>
  <button id="refresh-qr" class="plain" type="button">刷新二维码</button>
  <div class="muted">用抖音 App 首页左上角「扫一扫」扫描上方二维码。<br>
  只有这一台手机时：截图保存二维码，在「扫一扫」右上角的相册中选择该截图。二维码过期会自动刷新。</div>
</section>
<section id="verify-box" hidden>
  <div class="muted">抖音验证页当前画面（输入内容已遮挡）：</div>
  <img id="screen" alt="验证画面">
  <div id="novnc-line" class="muted" hidden>可通过
    <a id="novnc" rel="noreferrer" target="_blank">noVNC 远程桌面</a>
    手动完成验证（密码见 SPARK_VNC_PASSWORD）。</div>
</section>
<button id="sms-alternative" class="plain" type="button" hidden>无法扫码？改用短信登录</button>
<section id="sms-box" hidden>
  <div><b id="sms-title">手机号验证码登录</b></div>
  <div id="sms-description" class="muted"></div>
  <div id="phone-row">
    <label for="phone">手机号</label>
    <input id="phone" type="tel" inputmode="numeric" autocomplete="tel" maxlength="16"
           placeholder="11 位手机号">
  </div>
  <label for="code">短信验证码</label>
  <div class="row">
    <input id="code" inputmode="numeric" autocomplete="one-time-code" maxlength="8"
           placeholder="短信验证码">
    <button id="send" class="plain" type="button">获取验证码</button>
  </div>
  <button id="verify" type="button">提交验证码</button>
  <button id="use-qr" class="plain" type="button" hidden>改用扫码</button>
  <div id="sms-msg" class="muted"></div>
  <div id="sms-hint" class="muted"></div>
</section>
<section>
  <div id="done" class="muted">此链接仅用于本次登录，登录成功或过期后自动失效。</div>
  <button id="cancel" class="plain" type="button" hidden>取消本次登录</button>
</section>
</main>
<script nonce="__NONCE__">
(() => {
  const base = location.pathname.replace(/\\/+$/, '');
  const panelToken = document.querySelector('meta[name="spark-token"]').content;
  const $ = (id) => document.getElementById(id);
  const LABELS = {
    starting: '正在打开抖音登录页…', pending: '等待扫码或手机验证码登录',
    scanned: '已扫码，请在手机上确认', sms_required: '需要短信验证码',
    verification_required: '抖音要求安全验证', success: '登录成功',
    finalizing: '已通过验证，正在保存登录',
    expired: '登录会话已过期', failed: '登录失败', cancelled: '已取消'
  };
  let qrVersion = -1, screenVersion = -1, finished = false, failures = 0, deadline = 0;
  let current = null, busy = false, verifyAfter = 0, smsAlternative = false, observedAt = 0;
  const STAGES = { browser: '启动浏览器', connection: '检查网络连接',
    page: '打开登录页', loading: '加载登录组件' };
  const show = (id, on) => { $(id).hidden = !on; };
  async function request(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(base + path, { ...options, signal: controller.signal });
      const body = await response.json();
      return { response, body };
    } finally { clearTimeout(timer); }
  }
  function reloadQr() {
    show('reload-qr', false);
    $('qr').src = base + '/qr.png?v=' + qrVersion + '&reload=' + Date.now();
  }
  $('qr').addEventListener('error', () => show('reload-qr', true));
  $('qr').addEventListener('load', () => show('reload-qr', false));
  $('reload-qr').addEventListener('click', reloadQr);
  function controls() {
    const sms = current && current.sms || {};
    const allowed = current && ['pending', 'sms_required'].includes(current.status) && !finished;
    const pendingAction = ['queued', 'running', 'waiting'].includes(current?.sms_action?.status);
    $('send').disabled = busy || pendingAction || !allowed
      || sms.resend_in > 0 || sms.sends_left === 0;
    $('send').textContent = sms.resend_in > 0 ? sms.resend_in + ' 秒后重发'
      : current?.sms_sent ? '重新获取验证码' : '获取验证码';
    $('verify').disabled = busy || pendingAction || !allowed || current.status !== 'sms_required'
      || Date.now() < verifyAfter || sms.verifies_left === 0;
    $('use-qr').disabled = busy || !allowed;
    $('refresh-qr').disabled = busy || finished || current?.status !== 'pending'
      || current?.qr_refreshing;
  }
  function tick() {
    if (!finished && deadline && Date.now() >= deadline && current?.status !== 'finalizing') {
      finished = true;
      $('status').textContent = '登录会话已过期';
      $('status').dataset.state = 'expired';
      ['qr-box', 'verify-box', 'sms-box', 'sms-alternative', 'cancel']
        .forEach(id => show(id, false));
      $('code').value = ''; $('phone').value = '';
      $('done').textContent = '本次登录已结束，如需重试请重新发起登录。';
    }
    controls();
    if (finished || !deadline) { $('expires').textContent = ''; return; }
    const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    const elapsed = Math.max(0, Math.floor((current?.elapsed_seconds || 0)
      + (Date.now() - observedAt) / 1000));
    $('expires').textContent = '已等待 ' + Math.floor(elapsed / 60) + ' 分 ' + elapsed % 60
      + ' 秒 · 剩余 ' + Math.floor(left / 60) + ' 分 ' + (left % 60) + ' 秒';
  }
  function render(s) {
    current = s;
    observedAt = Date.now();
    const loading = ['starting', 'pending'].includes(s.status) && !s.qr_version;
    $('status').textContent = loading ? STAGES[s.stage] || '加载登录组件'
      : s.status === 'success' && s.login_source === 'saved_session'
        ? '已恢复已有登录' : LABELS[s.status] || s.status;
    $('status').dataset.state = s.status;
    const message = String(s.message || '').replace(/(\\w+:\\/\\/)[^/\\s@]+@/g, '$1***@');
    $('message').textContent = loading && s.elapsed_seconds >= 20
      ? '页面加载较慢，请稍候。' : message;
    $('route').textContent = s.proxy ? '代理 ' + s.proxy : '直接连接';
    deadline = Date.now() + s.expires_in * 1000;
    finished = s.finished;
    const qrOn = !finished && ['pending', 'scanned'].includes(s.status) && s.qr_version > 0;
    show('qr-box', qrOn);
    show('refresh-qr', qrOn && s.status === 'pending');
    if (qrOn && s.qr_version !== qrVersion) {
      show('reload-qr', false);
      qrVersion = s.qr_version; $('qr').src = base + '/qr.png?v=' + qrVersion;
    }
    const verifyOn = !finished && (s.status === 'verification_required'
      || ['error', 'unconfirmed'].includes(s.sms_action?.status));
    show('verify-box', verifyOn);
    if (verifyOn && s.screen_version > 0 && s.screen_version !== screenVersion) {
      screenVersion = s.screen_version; $('screen').src = base + '/screen.png?v=' + screenVersion;
    }
    if (s.novnc_url) { $('novnc').href = s.novnc_url; }
    show('novnc-line', Boolean(s.novnc_url));
    const smsOn = s.status === 'sms_required' || (s.status === 'pending' && smsAlternative);
    show('sms-box', !finished && smsOn);
    show('sms-alternative', !finished && s.status === 'pending' && !smsAlternative);
    $('sms-title').textContent = s.secondary_verification ? '扫码后二次验证' : '手机号验证码登录';
    const smsSent = s.sms_sent || (s.message || '').includes('已发送')
      || (s.sms?.resend_in || 0) > 0;
    $('sms-description').textContent = s.secondary_verification
      ? (s.phone_input
        ? (s.phone_hint ? '抖音提示绑定手机：' + s.phone_hint + '。' : '未能读取到绑定手机号提示。')
          + '请填写本次扫码账号绑定的完整手机号，再获取短信验证码。'
        : (smsSent ? '验证码已发送到' : '验证码会发送到') + '抖音账号绑定的手机'
          + (s.phone_hint ? '（' + s.phone_hint + '）' : '（未能读取到号码提示）')
          + '，无需重新输入手机号。')
      : '可使用手机验证码登录，或扫描上方二维码。';
    show('phone-row', s.phone_input);
    show('use-qr', s.sms_tab && !s.secondary_verification);
    show('cancel', !finished && s.status !== 'finalizing');
    const wait = s.sms ? s.sms.resend_in : 0;
    $('sms-hint').textContent = s.sms && s.sms.sends_left === 0
      ? '本次获取验证码次数已用完，请重新发起登录。'
      : (wait > 0 ? wait + ' 秒后可重新获取验证码' : '验证码由你填写，仅用于本次登录。');
    if (finished) {
      $('code').value = ''; $('phone').value = '';
      $('done').textContent = s.status === 'success'
        ? '登录成功，可以关闭此页面。' : '本次登录已结束，如需重试请重新发起登录。';
    }
    tick();
  }
  async function poll() {
    try {
      const { response: r, body } = await request('/state', { cache: 'no-store' });
      if (!r.ok) throw new Error(String(r.status));
      render(body);
      failures = 0;
    } catch (e) {
      failures += 1;
      if (!finished) $('status').textContent = '暂时无法获取登录状态，正在重连…';
      tick();
    }
    if (!finished) setTimeout(poll, Math.min(5000, 1500 + failures * 500));
  }
  async function post(path, payload) {
    if (busy || finished) return;
    busy = true; controls();
    $('sms-msg').textContent = '';
    try {
      const { response: r, body: j } = await request(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json',
          ...(panelToken ? { 'X-Spark-Token': panelToken } : {}) },
        body: JSON.stringify(payload || {})
      });
      $('sms-msg').textContent = j.message || j.error || (r.ok ? '' : '请求失败');
      if (r.ok && path === '/sms/verify') {
        $('code').value = ''; verifyAfter = Date.now() + 3000;
      }
      if (r.ok && path === '/sms/send' && current) {
        current.sms ||= {};
        current.sms.resend_in = 60;
      }
    } catch (e) {
      $('sms-msg').textContent = '请求结果尚未确认，请等待页面反馈。';
    } finally {
      busy = false; controls();
    }
  }
  $('send').addEventListener('click', () =>
    post('/sms/send', $('phone-row').hidden ? {} : { phone: $('phone').value }));
  $('sms-alternative').addEventListener('click', () => {
    smsAlternative = true; if (current) render(current);
  });
  $('verify').addEventListener('click', () => post('/sms/verify', { code: $('code').value }));
  $('use-qr').addEventListener('click', () => post('/use-qr'));
  $('refresh-qr').addEventListener('click', () => post('/qr/refresh'));
  $('cancel').addEventListener('click', () => {
    if (confirm('确定取消本次登录？')) post('/cancel');
  });
  setInterval(tick, 1000);
  poll();
})();
</script>
</body></html>
"""


def page_policy(nonce: str) -> str:
    return (
        f"default-src 'none'; img-src 'self'; style-src 'nonce-{nonce}'; "
        f"script-src 'nonce-{nonce}'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    )


def render_page(account: str, nonce: str, *, panel_token: str = "") -> str:
    return (
        PAGE.replace("__NONCE__", nonce)
        .replace("__PANEL_TOKEN__", html.escape(panel_token, quote=True))
        .replace("__ACCOUNT__", html.escape(account))
    )
