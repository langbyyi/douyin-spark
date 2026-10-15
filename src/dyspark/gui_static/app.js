(() => {
  const TOKEN = document.querySelector('meta[name="spark-token"]').content;
  const $ = (sel) => document.querySelector(sel);
  const accountLabel = name => window.accountDisplayName(state.accounts.find(account => account.name === name)) || name;
  const loginAccountLabel = name => window.accountOptionLabel(state.accounts.find(account => account.name === name), state.accounts) || name;
  const state = { accounts: [], loginSession: null, loginTimer: null, loginRestoring: false, jobId: null, jobTimer: null, jobBusy: false, fetchTimer: null, loginPolling: null, fetchPolling: false, fetchAccount: null, loginVersion: 0, loginCancelling: false, addClosePending: false, qrRefreshing: false, logsLoaded: false, lastRunAccount: null };
  const scheduleState = { info: null, times: [], dirty: false, saving: false, revision: 0 };
  const loginSms = { payload: null, busy: false, resendUntil: 0, verifyUntil: 0 };
  const targetMutations = new Map();
  let targetSelectionVersion = 0, targetRequestVersion = 0;
  const loginProgress = window.createLoginProgress({ setMsg });
  const loginFinished = payload => ["success", "failed", "expired", "cancelled"].includes(payload?.status);
  const loginSettling = payload => loginFinished(payload) && ["running", "blocked"].includes(payload?.job_state);
  const loginNeedsPolling = payload => !loginFinished(payload) || loginSettling(payload);

  let loginRedirecting = false;
  function redirectToLogin() {
    if (loginRedirecting) return;
    loginRedirecting = true;
    window.location.replace("/auth/login?expired=1");
  }

  let tokenReloadAsked = false;
  async function api(path, opts = {}) {
    const headers = Object.assign({ Accept: "application/json" }, opts.headers || {});
    if (opts.method && opts.method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-Spark-Token"] = TOKEN;
    }
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    if (res.status === 401) {
      redirectToLogin();
      throw new Error("面板登录已过期，请重新登录");
    }
    const data = await res.json().catch(() => ({ ok: false, error: "非 JSON 响应" }));
    if (!res.ok || data.ok === false) {
      if (res.status === 403 && (data.error || "").includes("面板令牌")) {
        // 只问一次：轮询接口持续 403 时反复弹确认会变成死循环。
        if (tokenReloadAsked) throw new Error("面板已重启，请刷新页面（F5）后重试");
        tokenReloadAsked = true;
        if (confirm("面板服务已重启，本页面已过期。重新加载页面后重试吗？")) {
          window.location.reload();
          throw new Error("页面已过期，正在刷新…");
        }
      }
      throw new Error(data.error || res.statusText || "请求失败");
    }
    return data.data !== undefined ? data.data : data;
  }

  async function loginRequest(path, opts = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try { return await api(path, { ...opts, signal: controller.signal }); }
    finally { clearTimeout(timer); }
  }

  function setMsg(el, text, kind) {
    el.textContent = text || "";
    el.className = "msg" + (kind ? " " + kind : "");
  }

  let toastTimer = null;
  function toast(text, kind) {
    const el = $("#toast");
    if (!el) return;
    el.textContent = text || "";
    el.className = "toast" + (kind ? " " + kind : "");
    el.classList.toggle("hidden", !text);
    if (toastTimer) clearTimeout(toastTimer);
    if (text) {
      toastTimer = setTimeout(() => {
        el.classList.add("hidden");
        el.textContent = "";
      }, 6000);
    }
  }

  const tabs = [...document.querySelectorAll(".tabs button")];
  function selectTab(btn) {
    tabs.forEach((tab) => {
      const active = tab === btn;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      $("#tab-" + tab.dataset.tab).classList.toggle("active", active);
    });
    if (window.matchMedia("(max-width: 700px)").matches) {
      btn.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    if (btn.dataset.tab === "logs" && !state.logsLoaded) {
      state.logsLoaded = true;
      $("#btn-logs").click();
    }
  }
  tabs.forEach((btn, index) => {
    btn.id = "nav-" + btn.dataset.tab;
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-controls", "tab-" + btn.dataset.tab);
    const panel = $("#tab-" + btn.dataset.tab);
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", btn.id);
    btn.addEventListener("click", () => selectTab(btn));
    btn.addEventListener("keydown", (event) => {
      let next;
      if (["ArrowRight", "ArrowDown"].includes(event.key)) next = (index + 1) % tabs.length;
      if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = (index + tabs.length - 1) % tabs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabs.length - 1;
      if (next === undefined) return;
      event.preventDefault();
      selectTab(tabs[next]);
      tabs[next].focus();
    });
  });
  selectTab(tabs[0]);
  const mobileNavigation = window.matchMedia("(max-width: 700px)");
  function setNavigationOrientation() {
    $(".tabs").setAttribute("aria-orientation", mobileNavigation.matches ? "horizontal" : "vertical");
  }
  mobileNavigation.addEventListener("change", setNavigationOrientation);
  setNavigationOrientation();

  function switchManagedAccount(name) {
    const previous = $("#account-edit-name").value;
    if (accountControls.isSaving()) return false;
    if (previous !== name) {
      if (state.jobBusy) {
        toast("任务正在执行，请等待结束或完成后再切换账号。", "err"); return false;
      }
      if ($("#login-account").disabled && $("#login-account").value !== name) {
        toast("账号正在登录，请先完成或取消登录再切换。", "err"); return false;
      }
      if ($("#friends-account").disabled) {
        toast("正在读取当前账号的会话，请等待完成再切换。", "err"); return false;
      }
      if ((accountControls.hasDraft() || $("#friend-name").value.trim())
        && !confirm("切换账号会放弃未保存的账号设置和待添加的对象名称，继续吗？")) return false;
    }
    return accountControls.openAccount(name, { discardConfirmed: true });
  }

  /* 任务结果属于执行它的那个账号：换账号时清掉，免得看着像新账号刚发过。 */
  function resetRunCard() {
    setMsg($("#run-msg"), "", "");
    const box = $("#job-summary");
    box.className = "empty-state";
    box.innerHTML = "<h3>暂无任务结果</h3>";
    $("#job-details").classList.add("hidden");
    $("#run-result").textContent = "";
  }

  function syncManagedAccount(name) {
    if (!addingAccount && !$("#login-account").disabled && loginSms.payload?.account !== name && !loginStarting) {
      state.loginVersion += 1;
      state.loginSession = null;
      loginSms.payload = null;
      loginProgress.reset();
      $("#login-sms").classList.add("hidden");
      $("#login-url").replaceChildren();
      $("#btn-login-cancel").disabled = true;
    }
    if ($("#friends-account").value !== name) {
      targetSelectionVersion += 1;
      $("#friend-name").value = "";
      $("#friends-search").value = "";
      $("#friends-live").classList.add("hidden");
      setMsg($("#friends-msg"), "", "");
      setMsg($("#account-delete-msg"), "", "");
      resetRunCard();
    }
    $("#friends-account").value = name;
    $("#btn-add-friend").disabled = targetMutations.has(name);
    $("#account-friends").disabled = accountControls.isSaving() || targetMutations.has(name);
    if (!$("#login-account").disabled) $("#login-account").value = name;
    renderFriends();
    workspace.render(state.accounts, name);
  }

  function navigate(page, account) {
    const accountSections = { friends: "targets", login: "login", messages: "messages", settings: "settings", "account-overview": "overview" };
    const connecting = page === "login";
    const withinAccount = page === "accounts" || !!accountSections[page];
    if (account && !state.accounts.some(a => a.name === account)) {
      toast("账号已不存在，请刷新后重试", "err"); return;
    }
    if (connecting) {
      const selector = $("#login-account");
      account = account || $("#account-edit-name").value || selector.value;
      if (!account) { toast("请先添加抖音账号", "err"); return; }
      if (selector.disabled && selector.value !== account) {
        toast(`正在登录 ${selector.value}，请先完成或取消，再登录 ${account}`, "err");
        account = selector.value;
      }
      if (!window.accountConnectionState(state.accounts.find(item => item.name === account)).requiresLogin
          && !(selector.disabled && selector.value === account)) page = "account-overview";
    }
    if (withinAccount) {
      const section = accountSections[page] || (account ? "settings" : "overview");
      account = account || $("#account-edit-name").value;
      if (account && !switchManagedAccount(account)) return;
      if (account) syncManagedAccount(account);
      workspace.selectSection(section);
      page = "accounts";
    }
    const tab = tabs.find(button => button.dataset.tab === page);
    if (tab) {
      selectTab(tab); tab.focus();
      if (page === "accounts" && account) {
        $("#account-detail-name").focus({ preventScroll: true });
      }
    }
  }
  $("#account-login-close").addEventListener("click", () => {
    workspace.selectSection("overview"); $("#account-login").focus();
  });
  $("#login-account").addEventListener("change", () => {
    const selector = $("#login-account");
    if (!switchManagedAccount(selector.value)) {
      selector.value = $("#account-edit-name").value; return;
    }
  });
  let addingAccount = false, loginStarting = false, addProxyRef = "";
  function mountAddLogin(prepare = true) {
    addingAccount = true;
    const dialog = $("#account-add-dialog");
    const pane = $("#tab-login");
    if (!pane) { addingAccount = false; return; }
    delete pane.dataset.accountPane;
    $("#account-add-login").append(pane);
    pane.querySelector(".page-heading").classList.add("hidden");
    $("#btn-login-start").classList.add("hidden");
    $("#account-add-retry").classList.add("hidden");
    $("#account-add-edit-proxy").classList.add("hidden");
    // prepare=true 先展示准备步骤（可选代理），点「开始扫码登录」再发起；
    // 刷新页面恢复进行中的登录（prepare=false）时直接回到登录画面。
    $("#account-add-prepare").classList.toggle("hidden", !prepare);
    $("#account-add-login").classList.toggle("hidden", prepare);
    pane.classList.toggle("hidden", prepare);
    $("#account-add-start").disabled = false;
    setMsg($("#account-add-prepare-msg"), "", "");
    if (!dialog.open) dialog.showModal();
  }
  function unmountAddLogin() {
    if (!addingAccount) return;
    addingAccount = false;
    state.addClosePending = false;
    if (state.loginTimer) { clearInterval(state.loginTimer); state.loginTimer = null; }
    $("#account-add-dialog").close();
    const pane = $("#tab-login");
    if (pane) {
      $("#account-login-slot").append(pane);
      pane.dataset.accountPane = "login";
      pane.classList.add("hidden");
      pane.querySelector(".page-heading").classList.remove("hidden");
    }
    $("#btn-login-start").classList.remove("hidden");
    $("#login-account").disabled = false;
    $("#btn-login-start").disabled = false;
    $("#account-add-prepare").classList.add("hidden");
    $("#account-add-prepare-msg").textContent = "";
    $("#account-add-proxy").value = "";
    addProxyRef = "";
    state.loginSession = null;
    syncManagedAccount($("#account-edit-name").value);
  }
  async function openAddAccount() {
    if (addingAccount) {
      if (!$("#account-add-dialog").open) $("#account-add-dialog").showModal();
      return;
    }
    if (state.loginCancelling) { toast("正在取消上一次登录，请稍候再添加账号。", "err"); return; }
    if (loginStarting || $("#login-account").disabled || state.jobBusy || $("#friends-account").disabled || accountControls.isSaving()) {
      toast("请先完成当前任务或登录，再添加账号。", "err"); return;
    }
    if ((accountControls.hasDraft() || $("#friend-name").value.trim())
        && !confirm("添加成功后会切换到新账号，未保存的修改将丢弃。继续吗？")) return;
    mountAddLogin();
  }
  function closeAddAccount() {
    if (state.loginCancelling) return;
    if (loginStarting) {
      // 启动请求还没回来、拿不到会话号：记住关闭意图，响应一到就取消并关闭。
      state.addClosePending = true;
      $("#account-add-dialog").close();
      return;
    }
    if (state.loginSession && (loginSettling(loginSms.payload)
        || ["finalizing", "success"].includes(loginSms.payload?.status))) {
      $("#account-add-dialog").close();
      return;
    }
    const active = state.loginSession && loginNeedsPolling(loginSms.payload);
    if (!active) { unmountAddLogin(); return; }
    // 会话还在跑：立即关闭弹窗，取消放后台执行，不让用户盯着「正在取消」等十几秒。
    const sessionId = state.loginSession;
    const payload = loginSms.payload, proxyRef = addProxyRef;
    state.loginVersion += 1;
    if (state.loginTimer) { clearInterval(state.loginTimer); state.loginTimer = null; }
    unmountAddLogin();
    cancelLoginInBackground(sessionId, payload, proxyRef);
  }
  async function cancelLoginInBackground(sessionId, payload, proxyRef = "") {
    state.loginCancelling = true;
    try {
      const data = await loginRequest(`/api/login/${encodeURIComponent(sessionId)}/cancel`, { method: "POST", body: "{}" });
      if (["finalizing", "success"].includes(data.status)) {
        mountAddLogin(false); addProxyRef = proxyRef;
        showLogin(data);
        $("#account-add-dialog").close();
        state.loginTimer = setInterval(pollLogin, 2500);
        if (!loginNeedsPolling(data)) await finishLogin(data, state.loginVersion);
        return;
      }
      if (loginNeedsPolling(data)) throw new Error("Login cancellation is still pending");
      // 弹窗关闭期间用户也可能刚好扫上了：真登录成功就照常说一声。
      if (data.status === "success") toast(`账号 ${accountLabel(data.account || "")} 登录成功`, "ok");
    } catch (error) {
      if (payload) {
        mountAddLogin(false); addProxyRef = proxyRef;
        showLogin(payload);
        state.loginTimer = setInterval(pollLogin, 2500);
      }
      toast("取消未完成，请再次取消登录。", "err");
    } finally {
      state.loginCancelling = false;
    }
  }
  $("#account-add-close").addEventListener("click", closeAddAccount);
  $("#account-add-dialog").addEventListener("cancel", event => { event.preventDefault(); closeAddAccount(); });
  $("#account-add-retry").addEventListener("click", () => startLogin(true, addProxyRef));
  $("#account-add-edit-proxy").addEventListener("click", () => {
    if (loginStarting || state.loginCancelling || !["failed", "expired", "cancelled"].includes(loginSms.payload?.status)) return;
    $("#account-add-prepare").classList.remove("hidden");
    $("#account-add-login").classList.add("hidden");
    $("#account-add-retry").classList.add("hidden");
    $("#account-add-edit-proxy").classList.add("hidden");
    $("#account-add-proxy").focus();
  });
  $("#account-add-start").addEventListener("click", () => {
    if (loginStarting) return;
    const ref = $("#account-add-proxy").value.trim();
    $("#account-add-prepare").classList.add("hidden");
    $("#account-add-prepare-msg").textContent = "";
    $("#account-add-login").classList.remove("hidden");
    $("#tab-login").classList.remove("hidden");
    startLogin(true, ref);
  });
  async function testProxyConnectivity(input, button, msg) {
    const value = $(input).value.trim();
    if (!value) { setMsg($(msg), "请先填写代理地址；留空表示直接连接，无需测试。", ""); return; }
    $(button).disabled = true;
    setMsg($(msg), "正在通过代理连接，请稍候…", "");
    try {
      const data = await api("/api/proxy/check", { method: "POST", body: JSON.stringify({ proxy: value }) });
      setMsg($(msg), `${data.message}（${data.latency_ms}ms）`, "ok");
    } catch (error) {
      setMsg($(msg), error.message, "err");
    } finally {
      $(button).disabled = false;
    }
  }
  $("#account-add-proxy-test").addEventListener("click", () =>
    testProxyConnectivity("#account-add-proxy", "#account-add-proxy-test", "#account-add-prepare-msg"));
  $("#account-proxy-test").addEventListener("click", () =>
    testProxyConnectivity("#account-proxy-ref", "#account-proxy-test", "#account-proxy-test-msg"));
  document.querySelector("main").addEventListener("click", event => {
    if (event.target.closest("[data-add-account]")) { openAddAccount(); return; }
    const button = event.target.closest("button[data-open]");
    if (!button) return;
    navigate(button.dataset.open, button.dataset.account);
  });

  async function toggleAccountEnabled(name, enable) {
    if (!name) return;
    if (accountControls.isSaving()) { toast("正在保存修改，请稍候再切换启用状态。", "err"); return; }
    if (enable && name === $("#account-edit-name").value && accountControls.hasDraft()) {
      toast("启用使用已保存的配置；请先保存或撤销修改。", "err"); return;
    }
    try {
      const result = await api("/api/accounts/" + (enable ? "enable" : "disable"), { method: "POST", body: JSON.stringify({ account: name }) });
      toast(result.message || (enable ? "已启用" : "已停用"), "ok");
    } catch (error) {
      toast(error.message, "err");
    }
    try { await refreshAccounts(); } catch (error) { toast(error.message, "err"); }
  }

  function accountActions(account) {
    const name = account.name, connection = window.accountConnectionState(account);
    const login = connection.requiresLogin ? `<button type="button" data-open="login" data-account="${esc(name)}">${esc(connection.actionLabel)}</button>` : "";
    const targets = account.friends?.length ? "管理对象" : "添加对象";
    return `<div class="row account-shortcuts">${login}<button type="button" class="ghost" data-open="friends" data-account="${esc(name)}">${targets}</button><button type="button" class="ghost" data-open="account-overview" data-account="${esc(name)}">查看账号</button></div>`;
  }
  function renderAccountDirectory() {
    workspace.render(state.accounts, $("#account-edit-name").value);
  }

  function fillAccountSelects(accounts) {
    const names = accounts.map((a) => a.name);
    for (const id of ["friends-account", "login-account"]) {
      const sel = $("#" + id);
      const current = sel.value;
      sel.innerHTML = "";
      accounts.forEach((account) => {
        const o = document.createElement("option");
        o.value = account.name;
        o.textContent = window.accountOptionLabel(account, accounts);
        sel.appendChild(o);
      });
      if (current && names.includes(current)) sel.value = current;
      else if (names.length) sel.value = names[0];
    }
  }

  const BUCKETS = { ok: "成功", failed: "失败", pending: "未尝试" };
  const bucketLabels = buckets => buckets.running ? { ...BUCKETS, running: "发送中" } : BUCKETS;

  /* Completed attempts are successful or failed; unattempted targets stay separate. */
  function accountBuckets(acc) {
    const buckets = { total: (acc.friends || []).length, ok: 0, held: 0, failed: 0, pending: 0, running: 0 };
    for (const item of acc.friends || []) {
      if (item.display_status === "running") buckets.running += 1;
      else if (item.ok === true) buckets.ok += 1;
      else if (item.held || item.ok === false) buckets.failed += 1;
      else buckets.pending += 1;
    }
    return buckets;
  }

  function sumBuckets(accounts) {
    const sum = { total: 0, ok: 0, held: 0, failed: 0, pending: 0, running: 0 };
    for (const acc of accounts) {
      const buckets = accountBuckets(acc);
      for (const key of Object.keys(sum)) sum[key] += buckets[key];
    }
    return sum;
  }

  function barMarkup(buckets, extra = "") {
    if (!buckets.total) return "";
    const cells = Object.keys(bucketLabels(buckets))
      .filter((key) => buckets[key])
      .map((key) => `<span class="${key}" data-count="${buckets[key]}"></span>`)
      .join("");
    const label = Object.entries(bucketLabels(buckets)).map(([key, name]) => `${name} ${buckets[key]}`).join("，");
    return `<div class="today-bar ${extra}" data-total="${buckets.total}" role="img" aria-label="${esc(label)}">${cells}</div>`;
  }

  /* 内联 style 属性被 CSP 的 style-src 'self' 拦掉，宽度只能用 CSSOM 设置。 */
  function paintBars(box) {
    for (const bar of box.querySelectorAll(".today-bar")) {
      const total = Number(bar.dataset.total) || 0;
      for (const cell of bar.children) {
        const count = Number(cell.dataset.count) || 0;
        cell.style.width = total ? (count / total * 100).toFixed(3) + "%" : "0%";
      }
    }
  }

  function legendMarkup(buckets) {
    return `<dl class="today-stats${buckets.running ? " has-running" : ""}">${Object.entries(bucketLabels(buckets))
      .map(([key, label]) => `<div class="today-stat ${key}" data-bucket="${key}"><dt>${label}</dt><dd><strong>${buckets[key]}</strong></dd></div>`)
      .join("")}</dl>`;
  }

  const nextRunLabel = (value, today) => (value ? window.scheduleTimeLabel(value, today) : "");

  function scheduleMarkup(scheduler, today, activeCount) {
    const info = scheduler || {};
    const enabled = info.enabled === true;
    const holding = Boolean(info.running && info.leader);
    const next = nextRunLabel(info.next_run, today);
    const detail = !enabled ? "未启用" : !holding ? "服务未就绪" : next || "待安排";
    return '<div class="card overview-plan"><div class="section-title"><h3>自动计划</h3>'
      + `<span class="pill ${enabled ? "ok" : ""}">${enabled ? "已启用" : "已停用"}</span></div>`
      + `<div class="overview-next"><span class="hint">下次执行</span><strong class="overview-next-run">${esc(detail)}</strong></div>`
      + (info.active_job ? '<span class="pill info">任务执行中</span>' : "")
      + (info.error ? `<p class="msg err">${esc(info.error)}</p>` : "")
      + `<div class="overview-plan-footer"><span class="hint">${activeCount} 个账号参与</span><button type="button" class="ghost" data-open="schedule">管理计划</button></div></div>`;
  }

  /* 需要人处理的账号排在前面，其余保持配置顺序。 */
  function accountRank(acc) {
    if (acc.enabled === false) return 0;
    if (window.accountConnectionState(acc).requiresLogin) return 3;
    return Number(acc.held_count || 0) ? 2 : 1;
  }

  const orderAccounts = (accounts) => accounts
    .map((acc, index) => [acc, index])
    .sort((a, b) => accountRank(b[0]) - accountRank(a[0]) || a[1] - b[1])
    .map((pair) => pair[0]);

  /* 汇总成一个清单：每条是「哪个账号的什么问题」加上下一步动作。 */
  function attentionItems(accounts) {
    const items = [];
    for (const acc of accounts) {
      const who = window.accountDisplayName(acc);
      const connection = window.accountConnectionState(acc);
      // 登录记录读不出来时卡片上已经写明，重新扫码也解决不了，不再重复列进清单。
      if (acc.enabled !== false && connection.requiresLogin && !acc.login_error) {
        // 结论词与后端原因说的是同一件事，这一行只留更具体的那句。
        items.push({
          account: acc.name, who, what: window.accountProblemReason(acc) || connection.label,
          detail: "", action: connection.actionLabel, open: "login",
        });
      }
      for (const item of acc.friends || []) {
        if (!item.held || item.display_status === "running") continue;
        const failure = window.deliveryFailure(item);
        items.push({
          account: acc.name, who,
          what: `${item.friend} · 失败：${failure.reason}`,
          detail: failure.guidance, failure,
        });
      }
    }
    return items;
  }

  function attentionMarkup(items) {
    if (!items.length) return "";
    const shown = items.slice(0, 8);
    const rows = shown.map((item) => {
      // 行内状态与后端原因经常是同一句话，重复时只留一句。
      const same = !item.detail || item.what.includes(item.detail) || item.detail.includes(item.what);
      const detail = same ? "" : ` · ${esc(item.detail)}`;
      return `<li><span class="attention-copy"><strong>${esc(item.who)}</strong>`
        + ` ${esc(item.what)}${detail}</span>`
        + (item.failure ? failureActionMarkup(item.failure, item.account)
          : `<button type="button" class="ghost" data-open="${item.open}" data-account="${esc(item.account)}">${esc(item.action)}</button>`)
        + `</li>`;
    }).join("");
    const rest = items.length > shown.length
      ? `<p class="hint">另有 ${items.length - shown.length} 项待处理</p>` : "";
    return `<div class="card attention-card"><div class="section-title"><h3>需要处理</h3>`
      + `<span class="pill bad">${items.length}</span></div><ul class="attention-list">${rows}</ul>${rest}</div>`;
  }

  function accountCard(acc) {
    const connection = window.accountConnectionState(acc);
    const enabled = acc.enabled !== false;
    const identity = window.accountIdentityNote(acc);
    const attention = `<span class="pill ${connection.kind === "connected" ? "ok" : "bad"}">${esc(connection.label)}</span>`;
    const paused = enabled ? "" : '<span class="pill">已停用</span>';
    const avatar = Array.from(window.accountDisplayName(acc) || "抖")[0];
    const parts = [`<article class="card account-block${enabled ? "" : " account-block-inactive"}" data-account="${esc(acc.name)}"><header class="account-block-head">`
      + `<span class="overview-account-avatar" aria-hidden="true">${esc(avatar)}</span>`
      + `<div class="account-block-title"><h3>${esc(window.accountDisplayName(acc))}</h3>`
      + (identity ? `<span class="hint">${esc(identity)}</span>` : "")
      + `</div><div class="account-block-badges">${attention}${paused}</div></header>`];
    const buckets = accountBuckets(acc);
    if (buckets.total) {
      parts.push(`<p class="account-block-counts"><strong>${buckets.ok}</strong> / ${buckets.total} 位已发送`
        + (buckets.failed ? ` · 失败 ${buckets.failed}` : "")
        + (buckets.pending ? ` · 未尝试 ${buckets.pending}` : "") + "</p>");
      if (buckets.running) parts.push(`<p class="hint">${buckets.running} 位发送中，正在等待平台回执</p>`);
      parts.push(barMarkup(buckets, "small"));
    }
    if (acc.login_error) parts.push(`<p class="msg err">${esc(acc.login_error)}</p>`);
    else if (!connection.requiresLogin && window.accountProblemReason(acc)) {
      // 下次运行会自己重新核对的异常：原因写在卡片上，不进「需要处理」清单。
      parts.push(`<p class="msg err">${esc(window.accountProblemReason(acc))}</p>`);
    }
    // 只列还没成功的对象：已发送的由上面的计数和进度条表达。
    const priority = item => item.held ? 2 : item.ok === false ? 1 : 0;
    const left = (acc.friends || []).filter(item => item.ok !== true).sort((a, b) => priority(b) - priority(a));
    if (left.length) {
      const shown = left.slice(0, 6);
      const chips = shown.map((item) => {
        const name = typeof item === "string" ? item : item.friend;
        const outcome = typeof item === "string" ? "pending" : item.outcome || "pending";
        const running = item.display_status === "running";
        const label = running ? "发送中" : item.held || item.ok === false ? "失败" : outcome === "pending" ? "未尝试" : outcomeLabel(outcome);
        const kind = running ? "info" : item.held || ["sending", "unconfirmed"].includes(outcome) ? "bad"
          : ["ok", "sent", "dry_run"].includes(outcome) ? "ok"
            : ["failed", "risk", "logged_out", "friend_error"].includes(outcome) ? "bad" : "";
        const reason = running ? "正在等待平台回执" : item.held || item.ok === false ? window.deliveryFailure(item).reason : "";
        return `<span class="friend-chip ${kind}" title="${esc(reason)}">${esc(name)}<i>${esc(label)}</i></span>`;
      });
      const more = left.length > shown.length
        ? `<button type="button" class="friend-more ghost" data-open="friends" data-account="${esc(acc.name)}">＋ ${left.length - shown.length} 位</button>` : "";
      parts.push(`<div class="friend-chips">${chips.join("")}${more}</div>`);
    } else if (!buckets.total) {
      parts.push('<p class="hint">未配置对象</p>');
    }
    parts.push(accountActions(acc), "</article>");
    return parts.join("");
  }

  function activityMarkup(view) {
    const parts = ['<div class="card activity-card"><div class="section-title"><h3>最近任务</h3>'
      + (view.busy ? '<span class="pill info">执行中</span>' : "")
      + '<button type="button" class="ghost" data-open="logs">查看日志</button></div>'];
    if (view.last_run_error) parts.push(`<p class="msg err">${esc(view.last_run_error)}</p>`);
    const jobs = Array.isArray(view.recent_jobs) ? view.recent_jobs.slice(0, 5) : [];
    if (!jobs.length && view.last_run) {
      const last = view.last_run;
      jobs.push({ kind: "run", dry_run: last.dry_run, started_at: last.started_at || last.finished_at || last.run,
        state: last.exit_code == null ? "running" : "finished", exit_code: last.exit_code });
    }
    if (jobs.length) {
      parts.push('<ul class="overview-tasks">');
      jobs.forEach(job => {
        const args = Array.isArray(job.args) ? job.args : [];
        const index = args.indexOf("--account");
        const name = index >= 0 ? args[index + 1] : "";
        const label = job.dry_run ? "发送测试" : kindLabel(job.kind);
        const stamp = job.started_at || "";
        const when = /^\d{8}-/.test(stamp) ? formatRunStamp(stamp) : stamp ? nextRunLabel(stamp, view.date) : "—";
        const kind = job.state === "running" ? "info" : job.state === "finished" && job.exit_code === 0 ? "ok"
          : job.state === "blocked" ? "warn" : job.state === "failed" || job.state === "finished" && job.exit_code !== 0 ? "bad" : "";
        parts.push(`<li class="overview-task"><div class="overview-task-name"><strong>${esc(label)}</strong>`
          + (name ? `<span class="hint">${esc(accountLabel(name))}</span>` : "")
          + `</div><span class="overview-task-time">${esc(when)}</span><span class="pill ${kind}">${esc(jobLabel(job))}</span></li>`);
      });
      parts.push("</ul>");
    } else parts.push('<p class="overview-tasks-empty hint">暂无任务记录</p>');
    parts.push("</div>");
    return parts.join("");
  }

  function renderStatus(view) {
    const box = $("#status-box");
    const accounts = view.accounts || [];
    const parts = [];
    if (view.ledger_blocked) parts.push(`<div class="overview-alert" role="alert"><span>${esc(view.ledger_error || "发送记录异常，请核对后再运行")}</span><button type="button" class="ghost" data-open="logs">查看日志</button></div>`);
    if (!accounts.length) {
      box.innerHTML = parts.join("") + '<div class="card overview-empty"><span class="overview-empty-icon" aria-hidden="true">＋</span><h3>暂无抖音账号</h3><button type="button" data-add-account>添加账号</button></div>' + activityMarkup(view);
      $("#updated-at").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
      return;
    }
    const ordered = orderAccounts(accounts);
    const active = ordered.filter((acc) => acc.enabled !== false);
    const totals = sumBuckets(active);
    const idle = accounts.length - active.length;
    parts.push('<div class="overview-summary"><div class="card today-card"><div class="section-title"><h3>今日进度</h3>'
      + `<span class="hint">${esc([view.date, view.timezone].filter(Boolean).join(" · "))}</span></div>`);
    if (totals.total) {
      parts.push(`<p class="today-count"><strong>${totals.ok}</strong><span> / ${totals.total} 位对象已发送</span></p>`);
      parts.push(barMarkup(totals));
    } else {
      parts.push(`<p class="today-unconfigured">${active.length ? "未配置对象" : "账号均已停用"}</p>`);
    }
    parts.push(legendMarkup(totals), "</div>", scheduleMarkup(view.scheduler, view.date, active.length), "</div>");
    parts.push(attentionMarkup(attentionItems(ordered)));
    parts.push('<div class="section-title overview-accounts-heading"><h3>账号结果</h3>'
      + `<span class="hint">${active.length} 已启用${idle ? ` · ${idle} 已停用` : ""}</span></div><div class="account-grid">`);
    for (const acc of ordered) parts.push(accountCard(acc));
    parts.push("</div>");
    parts.push(activityMarkup(view));
    box.innerHTML = parts.join("");
    for (const block of box.querySelectorAll(".account-block")) {
      const acc = accounts.find((item) => item.name === block.dataset.account);
      const avatar = block.querySelector(".overview-account-avatar");
      if (acc && avatar) window.renderAvatar(avatar, window.accountDisplayName(acc), acc.avatar_url);
    }
    paintBars(box);
    $("#updated-at").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
  }

  function outcomeLabel(value) {
    return ({ pending: "待运行", ok: "成功", failed: "失败", sent: "成功", dry_run: "测试通过", skipped: "已跳过", risk: "失败", logged_out: "失败", friend_error: "失败", unconfirmed: "失败", sending: "失败" })[value] || value;
  }
  function kindLabel(value) {
    return ({ run: "发送任务", check: "登录/IP 检查", friends: "拉取会话", login: "抖音登录" })[value] || value || "任务";
  }
  function verdictLabel(value) {
    // 与 runner.VERDICT_LABEL（推送正文）保持同一套用词：同一状态在面板和微信里同名。
    return ({ done: "成功", partial: "失败", error: "失败", ip_mismatch: "失败", needs_login: "失败", blocked: "失败" })[value] || value || "执行中";
  }
  function jobLabel(job) {
    if (emptyRun(job)) return job.result.dry_run ? "未执行测试" : "未执行发送";
    if (job.state === "finished") {
      const hasFailures = (job.result?.accounts || []).some(account => account.ok === false
        || (account.verdict && account.verdict !== "done") || Object.keys(account.failed || {}).length);
      return job.exit_code === 0 && !hasFailures ? "执行成功" : "执行失败";
    }
    return ({ running: "正在执行", failed: "执行失败", blocked: "执行失败", cancelled: "已取消" })[job.state] || job.state || "准备中";
  }

  function emptyRun(job) {
    return job.kind === "run" && job.state === "finished" && job.exit_code === 0
      && Array.isArray(job.result?.accounts) && job.result.accounts.length === 0;
  }

  function emptyRunReason() {
    const acc = state.accounts.find(a => a.name === state.lastRunAccount);
    if (!acc) return "本次没有需要处理的账号。";
    if (acc.enabled === false) return "此账号已停用，请先开启账号开关。";
    const friends = acc.friends?.length || 0;
    if (!friends) return "此账号暂无续火花对象，请在「续火花对象」中添加。";
    if (Number(acc.summary?.ok || 0) >= friends) return "名单中的对象今日均已发送成功，无需再次发送。";
    return "本次没有需要处理的账号。";
  }

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function failureActionMarkup(failure, account) {
    const labels = { check: "检测账号", login: "重新登录", friends: "查看会话", logs: "查看日志", settings: "检查代理" };
    const action = failure.action || "logs";
    return `<button type="button" class="ghost" data-failure-action="${esc(action)}" data-account="${esc(account)}">${esc(labels[action] || labels.logs)}</button>`;
  }

  document.addEventListener("click", event => {
    const button = event.target.closest("[data-failure-action]");
    if (!button) return;
    const account = button.dataset.account, action = button.dataset.failureAction;
    if (action === "logs") { navigate("logs"); return; }
    if (!state.accounts.some(item => item.name === account)) { toast("账号已不存在，请刷新", "err"); return; }
    if (!accountControls.openAccount(account)) return;
    if (action === "check") {
      navigate("account-overview", account);
      startJob("/api/check", { account, wait_seconds: 0 });
    } else navigate(action === "login" ? "login" : action === "settings" ? "settings" : "friends", account);
  });

  function renderFriends() {
    const account = $("#friends-account").value;
    state.friendsAccount = account;
    const info = state.accounts.find((a) => a.name === account) || {};
    const acc = info.friends || [];
    const sparkDays = info.spark_days || {};
    const conversationMetadata = info.conversation_metadata || {};
    const sparkAt = info.sparks_updated_at ? displayTime(info.sparks_updated_at) : "";
    const ul = $("#friends-list");
    ul.innerHTML = "";
    $("#friends-count").textContent = "发送名单";
    $("#friends-total").textContent = String(acc.length);
    $("#friends-search").disabled = !acc.length;
    const query = $("#friends-search").value.trim().toLocaleLowerCase();
    const shown = acc.filter(name => name.toLocaleLowerCase().includes(query));
    if (!shown.length) ul.innerHTML = `<li class="empty-state">${acc.length ? "未找到匹配对象" : "暂无对象"}</li>`;
    shown.forEach((name) => {
      const li = document.createElement("li");
      const days = sparkDays[name];
      const copy = document.createElement("div"); copy.className = "target-entry";
      copy.append(window.conversationIdentity(name, conversationMetadata[name] || {}, days, sparkAt));
      const sent = (info.send_results || []).find(item => item.friend === name);
      if (sent?.display_status === "running") {
        const description = document.createElement("p"); description.className = "hint";
        description.textContent = "发送中，正在等待平台回执"; copy.append(description);
      } else if (sent?.held || sent?.ok === false) {
        const failure = window.deliveryFailure(sent);
        const description = document.createElement("p"); description.className = "hint";
        description.textContent = `失败：${failure.reason}`; copy.append(description);
        const action = document.createElement("div");
        action.innerHTML = failureActionMarkup(failure, account); copy.append(action);
      }
      li.append(copy);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "danger ghost";
      btn.textContent = "移除";
      btn.setAttribute("aria-label", `移除好友 ${name}`);
      btn.addEventListener("click", () => mutateTarget("remove", account, name, btn));
      li.appendChild(btn);
      ul.appendChild(li);
    });
  }
  $("#friends-search").addEventListener("input", renderFriends);

  let accountRefreshVersion = 0;
  async function refreshAccounts(request = api) {
    const version = ++accountRefreshVersion;
    const data = await request("/api/accounts");
    if (version !== accountRefreshVersion) return;
    state.accounts = data.accounts || [];
    fillAccountSelects(state.accounts);
    accountControls.updateAccounts(data);
    renderAccountDirectory();
    $("#meta-line").textContent = `${data.preview ? "示例预览 · " : ""}${data.cloud ? "云端模式" : "本地模式"} · ${state.accounts.length} 个账号`;
    $("#meta-line").title = `配置文件：${data.config || "未提供"}`;
  }

  async function refreshAll() {
    await window.loadDeliveryFailureRules();
    await refreshAccounts();
    const status = await api("/api/status");
    renderStatus(status);
    const revision = scheduleState.revision;
    // 先取计划配置：调度器状态里的「下次自动运行」要按服务端时区说今天／明天。
    const sched = await api("/api/schedule");
    if (revision === scheduleState.revision && !scheduleState.saving) {
      if (!sched.scheduler) accountControls.updateScheduler(status.scheduler || {}, sched.now);
      renderSchedule(sched);
    }
    renderMcp(await api("/api/settings"), sched);
    await accountControls.refreshNotifications();
    const logsMeta = await api("/api/logs?lines=1");
    const sel = $("#logs-run");
    const cur = sel.value;
    renderRunOptions(sel, logsMeta.recent_runs || []);
    if (cur) sel.value = cur;
    await restoreLogin(status.recent_jobs || []);
  }

  function renderSchedule(info) {
    scheduleState.info = info;
    $("#schedule-zone").textContent = info.timezone || "未提供时区";
    if (!scheduleState.dirty && !scheduleState.saving) {
      scheduleState.times = [...(info.times || [])];
      renderTimeEditor();
    }
    const runs = Array.isArray(info.next_runs) ? info.next_runs.slice(1, 4) : [];
    $("#schedule-next-runs").innerHTML = runs.length
      ? `<h4>后续执行</h4><ul>${runs.map(t => `<li>${esc(window.scheduleTimeLabel(t, info.now))}</li>`).join("")}</ul>`
      : "";
    if (info.scheduler) accountControls.updateScheduler(info.scheduler, info.now);
  }

  let mcpBusy = false;
  function renderMcp(info, sched) {
    const enabled = !!info.mcp_enabled;
    const mcpSwitch = $("#mcp-switch");
    mcpSwitch.classList.toggle("on", enabled);
    mcpSwitch.setAttribute("aria-checked", String(enabled));
    $("#mcp-status").textContent = enabled
      ? "已开启 · AI 客户端可启动 spark mcp 控制登录、查询与发送。"
      : "已关闭 · 新的 spark mcp 启动会被拒绝；已连接的 AI 会话不受影响。";
    const schedInfo = sched || scheduleState.info || {};
    const spark = schedInfo.spark_command;
    $("#mcp-command").textContent = spark ? `${spark} mcp` : "spark mcp";
    if (schedInfo.mcp?.command) {
      $("#mcp-json").textContent = JSON.stringify(
        { mcpServers: { dyspark: { type: "stdio", command: schedInfo.mcp.command, args: schedInfo.mcp.args } } },
        null,
        2,
      );
    }
  }
  async function controlMcp(enabled) {
    if (mcpBusy) return;
    if (!enabled && !confirm("关闭后，新的 spark mcp 启动会被拒绝；已在运行的 AI 客户端会话不受影响。确认关闭？")) return;
    mcpBusy = true;
    $("#mcp-switch").disabled = true;
    try {
      const result = await api("/api/settings", { method: "POST", body: JSON.stringify({ mcp_enabled: enabled }) });
      setMsg($("#mcp-msg"), result.message, "ok");
    } catch (error) {
      setMsg($("#mcp-msg"), error.message, "err");
    } finally {
      mcpBusy = false;
      $("#mcp-switch").disabled = false;
      try { renderMcp(await api("/api/settings"), scheduleState.info); }
      catch (error) { toast(error.message, "err"); }
    }
  }
  $("#mcp-switch").addEventListener("click", () => controlMcp(!$("#mcp-switch").classList.contains("on")));

  function displayTime(value) {
    const match = String(value || "").match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
    return match ? `${match[1]} ${match[2]}` : value;
  }

  function updateScheduleControls() {
    const loaded = !!scheduleState.info;
    $("#btn-save-schedule").disabled = !loaded || !scheduleState.dirty || scheduleState.saving;
    $("#btn-reset-schedule").disabled = !loaded || !scheduleState.dirty || scheduleState.saving;
    $("#btn-add-time").disabled = !loaded || scheduleState.saving;
    $("#schedule-dirty").textContent = scheduleState.saving ? "保存中…" : scheduleState.dirty ? "未保存" : "";
    $("#btn-save-schedule").textContent = scheduleState.saving ? "保存中…" : "保存时间";
    const scheduleSwitch = $("#scheduler-switch");
    scheduleSwitch.disabled = !loaded || scheduleState.dirty || scheduleState.saving || scheduleSwitch.getAttribute("aria-busy") === "true";
    scheduleSwitch.title = scheduleState.dirty ? "先保存或撤销时间修改" : "";
    $("#schedule-times").querySelectorAll("input, button").forEach(el => {
      el.disabled = scheduleState.saving || (el.tagName === "BUTTON" && scheduleState.times.length === 1);
    });
  }

  function markScheduleDirty() {
    scheduleState.revision += 1;
    scheduleState.times = [...$("#schedule-times").querySelectorAll("input")].map(input => input.value);
    scheduleState.dirty = true;
    setMsg($("#schedule-msg"), "", "");
    updateScheduleControls();
  }

  function renderTimeEditor() {
    const container = $("#schedule-times");
    container.replaceChildren();
    scheduleState.times.forEach((time, index) => {
      const row = document.createElement("div");
      row.className = "schedule-time-row";
      const number = document.createElement("span");
      number.className = "schedule-time-index";
      number.textContent = String(index + 1).padStart(2, "0");
      number.setAttribute("aria-hidden", "true");
      const label = document.createElement("label");
      const caption = document.createElement("span");
      caption.className = "visually-hidden";
      caption.textContent = `时间 ${index + 1}`;
      label.appendChild(caption);
      const input = document.createElement("input");
      input.type = "time";
      input.required = true;
      input.step = "60";
      input.value = time;
      input.addEventListener("input", markScheduleDirty);
      label.append(input);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "ghost";
      remove.textContent = "移除";
      remove.setAttribute("aria-label", `移除时间 ${index + 1}`);
      remove.addEventListener("click", () => {
        markScheduleDirty();
        scheduleState.times.splice(index, 1);
        renderTimeEditor();
      });
      row.append(number, label, remove);
      container.append(row);
    });
    updateScheduleControls();
  }

  $("#btn-add-time").addEventListener("click", () => {
    markScheduleDirty();
    scheduleState.times.push("");
    renderTimeEditor();
    $("#schedule-times").lastElementChild.querySelector("input").focus();
  });
  $("#btn-reset-schedule").addEventListener("click", () => {
    scheduleState.revision += 1;
    scheduleState.dirty = false;
    renderSchedule(scheduleState.info);
    setMsg($("#schedule-msg"), "", "");
  });
  $("#schedule-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (scheduleState.saving || !scheduleState.dirty) return;
    scheduleState.revision += 1;
    scheduleState.saving = true;
    updateScheduleControls();
    try {
      const info = await api("/api/schedule", { method: "POST", body: JSON.stringify({ times: scheduleState.times }) });
      scheduleState.saving = false;
      scheduleState.dirty = false;
      scheduleState.revision += 1;
      renderSchedule(info);
      setMsg($("#schedule-msg"), "已保存", "ok");
    } catch (error) {
      setMsg($("#schedule-msg"), error.message, "err");
    } finally {
      scheduleState.saving = false;
      updateScheduleControls();
    }
  });
  updateScheduleControls();

  $("#friends-account").addEventListener("change", () => {
    const candidate = $("#friends-account").value;
    if (!switchManagedAccount(candidate)) $("#friends-account").value = state.friendsAccount;
    else syncManagedAccount(candidate);
  });
  async function mutateTarget(action, account, name, button, onSaved) {
    if (accountControls.isSaving() || targetMutations.has(account)) {
      setMsg($("#friends-msg"), "名单操作正在执行，请稍候再修改。", "err"); return;
    }
    if (account === $("#account-edit-name").value && accountControls.hasTargetDraft()) {
      setMsg($("#friends-msg"), "请先保存或撤销批量名单修改，再添加或移除对象。", "err"); return;
    }
    const selectionVersion = targetSelectionVersion, requestVersion = ++targetRequestVersion;
    const isCurrent = () => selectionVersion === targetSelectionVersion
      && account === $("#friends-account").value && targetMutations.get(account) === requestVersion;
    targetMutations.set(account, requestVersion);
    button.disabled = true;
    if (account === $("#friends-account").value) {
      $("#btn-add-friend").disabled = true; $("#account-friends").disabled = true;
    }
    let committed = false;
    try {
      const data = await api(`/api/friends/${action}`, {
        method: "POST", body: JSON.stringify({ account, name }),
      });
      committed = true;
      if ((!data.account || data.account === account) && Array.isArray(data.friends)) {
        accountControls.updateTargets(account, data.friends);
        const configured = state.accounts.find(item => item.name === account);
        if (configured) configured.friends = [...data.friends];
        if (account === $("#friends-account").value) renderFriends();
      }
      onSaved?.(isCurrent());
      await refreshAccounts();
      if (isCurrent()) {
        const message = data.message || (action === "add" ? "已添加" : "已移除");
        setMsg($("#friends-msg"), message, "ok"); toast(message, "ok");
      }
    } catch (error) {
      if (isCurrent()) {
        const message = (committed ? "名单已更新，但刷新失败：" : "") + error.message;
        setMsg($("#friends-msg"), message, "err"); toast(message, "err");
      }
    } finally {
      if (targetMutations.get(account) === requestVersion) targetMutations.delete(account);
      button.disabled = false;
      const selected = $("#friends-account").value;
      $("#btn-add-friend").disabled = targetMutations.has(selected);
      $("#account-friends").disabled = accountControls.isSaving() || targetMutations.has(selected);
    }
  }
  $("#friend-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const account = $("#friends-account").value;
    const name = $("#friend-name").value.trim();
    if (!name) {
      setMsg($("#friends-msg"), "请输入好友备注名、昵称或完整群名", "err");
      $("#friend-name").focus();
      return;
    }
    await mutateTarget("add", account, name, $("#btn-add-friend"), isCurrent => {
      if (isCurrent && $("#friend-name").value.trim() === name) $("#friend-name").value = "";
    });
  });

  function renderLiveConversations(payload) {
    const box = $("#friends-live");
    const status = $("#friends-live-status");
    const ul = $("#friends-live-list");
    box.classList.remove("hidden");
    ul.innerHTML = "";
    const result = payload.result || payload;
    const accounts = result.accounts || [];
    const account = state.fetchAccount || $("#friends-account").value;
    $("#friends-live-title").textContent = "抖音会话 · " + (accountLabel(account) || "当前账号");
    let total = 0;
    accounts.forEach((acc) => {
      const resultAccount = acc.account || account;
      const known = new Set((state.accounts.find(a => a.name === resultAccount)?.friends || []).map(String));
      (acc.conversations || []).forEach((c) => {
        total += 1;
        const name = typeof c === "string" ? c : c.name;
        const streak = typeof c === "string" ? null : c.streak_days;
        const li = document.createElement("li");
        li.append(window.conversationIdentity(name, typeof c === "string" ? {} : c, streak));
        if (!known.has(name)) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "ghost";
          btn.textContent = "加入名单";
          btn.addEventListener("click", () =>
            mutateTarget("add", resultAccount, name, btn, () => {
              known.add(name);
              btn.remove();
            }));
          li.appendChild(btn);
        } else {
          const tag = document.createElement("span");
          tag.className = "hint";
          tag.textContent = "已在名单";
          li.appendChild(tag);
        }
        ul.appendChild(li);
      });
      if (acc.error) {
        const li = document.createElement("li");
        li.innerHTML = `<span class="msg err">${esc(acc.account || "")}：${esc(acc.error)}</span>`;
        ul.appendChild(li);
      }
    });
    if (payload.state === "running") {
      status.textContent = payload.message || `拉取中…（${payload.job_id || ""}）`;
    } else if (payload.state && payload.state !== "finished") {
      status.textContent = payload.message || `任务结束：${payload.state}（exit ${payload.exit_code}）`;
    } else {
      status.textContent = total ? `共 ${total} 个会话` : "未获取到会话（需先登录，或会话列表为空）";
    }
  }

  async function pollFetch(jobId) {
    if (!jobId || state.fetchPolling) return;
    state.fetchPolling = true;
    try {
      const st = await api(`/api/jobs/${encodeURIComponent(jobId)}?wait_seconds=1`);
      renderLiveConversations(st);
      if (st.state && st.state !== "running") {
        if (state.fetchTimer) {
          clearInterval(state.fetchTimer);
          state.fetchTimer = null;
        }
        const kind = st.exit_code === 0 ? "ok" : "err";
        setMsg($("#friends-msg"), st.message || `拉取结束（exit ${st.exit_code}）`, kind);
        if (st.exit_code !== 0) toast(st.message || `拉取失败（exit ${st.exit_code}）`, "err");
        $("#btn-fetch-friends").disabled = false;
        $("#friends-account").disabled = false;
        await refreshAccounts();
      }
    } catch (e) {
      setMsg($("#friends-msg"), e.message, "err");
      toast(e.message, "err");
    } finally {
      state.fetchPolling = false;
    }
  }

  $("#btn-fetch-friends").addEventListener("click", async () => {
    $("#btn-fetch-friends").disabled = true;
    const account = $("#friends-account").value;
    state.fetchAccount = account;
    $("#friends-account").disabled = true;
    $("#friends-live-title").textContent = "抖音会话 · " + (accountLabel(account) || "当前账号");
    const box = $("#friends-live");
    box.classList.remove("hidden");
    $("#friends-live-list").innerHTML = "";
    $("#friends-live-status").textContent = "正在启动浏览器拉取会话…";
    setMsg($("#friends-msg"), "正在拉取会话…", "");
    if (state.fetchTimer) {
      clearInterval(state.fetchTimer);
      state.fetchTimer = null;
    }
    try {
      const data = await api("/api/friends/fetch", {
        method: "POST",
        body: JSON.stringify({ account, wait_seconds: 3 }),
      });
      renderLiveConversations(data);
      const jobId = data.job_id;
      if (data.state === "running" && jobId) {
        state.fetchTimer = setInterval(() => pollFetch(jobId), 2000);
      } else {
        $("#btn-fetch-friends").disabled = false;
        $("#friends-account").disabled = false;
        setMsg(
          $("#friends-msg"),
          data.message || (data.exit_code === 0 ? "拉取完成" : `结束（exit ${data.exit_code}）`),
          data.exit_code === 0 ? "ok" : "err"
        );
      }
    } catch (e) {
      $("#btn-fetch-friends").disabled = false;
      $("#friends-account").disabled = false;
      $("#friends-live-status").textContent = "";
      setMsg($("#friends-msg"), e.message, "err");
      toast(e.message, "err");
    }
  });

  async function restoreLogin(recentJobs) {
    if (state.loginSession || state.loginRestoring || loginStarting || state.loginCancelling) return;
    const job = recentJobs.find(item => item.kind === "login" && ["running", "blocked"].includes(item.state));
    if (!job) return;
    const version = state.loginVersion;
    state.loginRestoring = true;
    try {
      const payload = await api(`/api/login/${encodeURIComponent(job.job_id)}`);
      if (version !== state.loginVersion || state.loginSession) return;
      if (payload.new_account && loginNeedsPolling(payload)) {
        mountAddLogin(false);
        // 刷新后模块变量丢失：从后端任务参数找回本次登录的代理引用，重试才不会静默直连。
        addProxyRef = payload.proxy_ref || "";
      }
      showLogin(payload);
      if (loginNeedsPolling(payload)) {
        if (payload.account && !payload.new_account) navigate("login", payload.account);
        state.loginTimer = setInterval(pollLogin, 2500);
      } else await finishLogin(payload, version);
    } finally {
      state.loginRestoring = false;
    }
  }

  function showLogin(payload) {
    if (state.loginSession !== payload.session_id) {
      $("#login-code").value = "";
      $("#login-phone").value = "";
      loginSms.resendUntil = loginSms.verifyUntil = 0;
      loginSms.busy = false;
      state.qrRefreshing = false;
      setMsg($("#login-sms-msg"), "", "");
    }
    state.loginSession = payload.session_id;
    loginSms.payload = payload;
    const terminal = ["success", "failed", "expired", "cancelled"].includes(payload.status);
    const settling = loginSettling(payload);
    if (payload.account && !payload.new_account) $("#login-account").value = payload.account;
    $("#login-account").disabled = !!payload.session_id && (!terminal || settling);
    $("#btn-login-cancel").disabled = !payload.session_id || terminal || payload.status === "finalizing";
    $("#btn-login-cancel").classList.toggle("hidden", !payload.session_id || terminal || payload.status === "finalizing");
    $("#btn-login-start").disabled = !!payload.session_id && (!terminal || settling);
    loginProgress.render(payload, loginAccountLabel(payload.account));
    $("#account-add-retry").classList.toggle("hidden", !addingAccount || !terminal || payload.status === "success");
    $("#account-add-edit-proxy").classList.toggle("hidden", !addingAccount || !terminal || payload.status === "success");
    $("#account-add-retry").disabled = settling || loginStarting;
    $("#account-add-edit-proxy").disabled = settling || loginStarting;
    if (settling) setMsg($("#login-connection-msg"), payload.job_state === "blocked" ? "会话状态待核对，请稍后重试。" : "正在结束本次登录，请稍候…", "");
    const smsOn = !terminal && payload.status === "sms_required";
    $("#login-sms").classList.toggle("hidden", !smsOn);
    $("#login-phone-row").classList.toggle("hidden", payload.phone_input === false);
    $("#login-sms-title").textContent = payload.secondary_verification ? "扫码后二次验证" : "短信验证";
    const phoneHint = payload.phone_hint;
    const smsSent = payload.sms_sent || (payload.message || "").includes("已发送") || (payload.sms?.resend_in || 0) > 0;
    if (smsSent && $("#login-sms-msg").textContent.startsWith("正在请求验证码")) {
      setMsg($("#login-sms-msg"), "", "");
    }
    $("#login-sms-description").textContent = payload.phone_input === false
      ? `验证码${smsSent ? "已发送" : "会发送"}到抖音账号绑定的手机${phoneHint ? `（${phoneHint}）` : "（未能读取到号码提示）"}，无需输入手机号。`
      : payload.secondary_verification
        ? `${phoneHint ? `抖音提示绑定手机：${phoneHint}。` : "未能读取到绑定手机号提示。"}请填写本次扫码账号绑定的完整手机号，再获取短信验证码。`
        : "请填写用于登录抖音的手机号，获取并输入短信验证码。";
    if (payload.sms) loginSms.resendUntil = Date.now() + (payload.sms.resend_in || 0) * 1000;
    const action = payload.sms_action;
    if (action && ["queued", "running", "waiting", "error", "unconfirmed"].includes(action.status)) {
      setMsg($("#login-sms-msg"), action.message, ["error", "unconfirmed"].includes(action.status) ? "err" : "");
    }
    const showPlatform = smsOn && payload.screen_path && ["error", "unconfirmed"].includes(action?.status);
    $("#login-platform-details").classList.toggle("hidden", !showPlatform);
    if (showPlatform) {
      $("#login-platform-details").open = true;
      $("#login-platform-screen").src = "/api/media?path=" + encodeURIComponent(payload.screen_path) + "&t=" + Date.now();
    }
    if (terminal) { $("#login-code").value = ""; $("#login-phone").value = ""; }
    updateLoginSmsControls();
    let url = null;
    try {
      if (!payload.url) throw new Error("No login link");
      const parsed = new URL(payload.url, location.origin);
      if (["http:", "https:"].includes(parsed.protocol)) url = parsed.href;
    } catch { /* No valid login link yet. */ }
    $("#login-url").innerHTML =
      url && ["pending", "scanned", "sms_required", "verification_required"].includes(payload.status)
        ? `<a class="ghost" href="${esc(url)}" target="_blank" rel="noopener noreferrer">打开登录验证页面</a>`
        : payload.duplicate_of
          ? `<button type="button" class="ghost" data-duplicate-goto="${esc(payload.duplicate_of)}">前往「${esc(payload.duplicate_label || payload.duplicate_of)}」</button>`
          : "";
    const qrButton = $("#login-qr-refresh");
    qrButton.disabled = state.qrRefreshing || payload.qr_refreshing;
    qrButton.textContent = qrButton.disabled ? "正在刷新二维码…" : "刷新二维码";
  }

  function updateLoginSmsControls() {
    const payload = loginSms.payload;
    const active = payload && payload.status === "sms_required";
    const wait = Math.max(0, Math.ceil((loginSms.resendUntil - Date.now()) / 1000));
    const limits = payload && payload.sms || {};
    const pendingAction = ["queued", "running", "waiting"].includes(payload?.sms_action?.status);
    $("#login-sms-send").disabled = !active || loginSms.busy || pendingAction || wait > 0 || limits.sends_left === 0;
    $("#login-sms-send").textContent = wait ? `${wait} 秒后重发` : payload?.sms_sent ? "重新获取验证码" : "获取验证码";
    $("#login-sms-verify").disabled = !active || loginSms.busy || pendingAction || Date.now() < loginSms.verifyUntil || limits.verifies_left === 0;
  }

  async function submitLoginSms(action) {
    if (loginSms.busy || !loginSms.payload || loginSms.payload.status !== "sms_required") return;
    const session = state.loginSession;
    const version = state.loginVersion;
    const body = action === "verify" ? { code: $("#login-code").value }
      : loginSms.payload.phone_input === false ? {} : { phone: $("#login-phone").value };
    const operation = {};
    loginSms.busy = operation;
    updateLoginSmsControls();
    try {
      const result = await loginRequest(`/api/login/${encodeURIComponent(session)}/sms/${action}`, {
        method: "POST", body: JSON.stringify(body),
      });
      if (version !== state.loginVersion || session !== state.loginSession) return;
      if (action === "verify") { $("#login-code").value = ""; loginSms.verifyUntil = Date.now() + 3000; }
      else loginSms.resendUntil = Date.now() + 60000;
      setMsg($("#login-sms-msg"), result.message, "");
    } catch (error) {
      if (version === state.loginVersion && session === state.loginSession) {
        setMsg($("#login-sms-msg"), error.name === "AbortError"
          ? "请求超时，结果尚未确认，请等待页面反馈。" : error.message, "err");
      }
    } finally {
      if (loginSms.busy === operation) loginSms.busy = false;
      if (version === state.loginVersion && session === state.loginSession) {
        updateLoginSmsControls(); await pollLogin();
      }
    }
  }
  $("#login-sms-send").addEventListener("click", () => submitLoginSms("send"));
  $("#login-sms-verify").addEventListener("click", () => submitLoginSms("verify"));
  $("#login-code").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !$("#login-sms-verify").disabled) {
      event.preventDefault(); submitLoginSms("verify");
    }
  });
  setInterval(() => { updateLoginSmsControls(); loginProgress.tick(); }, 1000);

  $("#login-qr-refresh").addEventListener("click", async () => {
    if (state.qrRefreshing || !state.loginSession || loginSms.payload?.status !== "pending" || !loginSms.payload?.qr_path) return;
    const session = state.loginSession, version = state.loginVersion;
    const operation = {};
    state.qrRefreshing = operation;
    $("#login-qr-refresh").disabled = true;
    try {
      const result = await loginRequest(`/api/login/${encodeURIComponent(session)}/qr/refresh`, {
        method: "POST", body: "{}",
      });
      if (version === state.loginVersion && session === state.loginSession) {
        loginProgress.retryImage(); setMsg($("#login-msg"), result.message, "");
      }
    } catch (error) {
      if (version === state.loginVersion && session === state.loginSession) setMsg($("#login-msg"),
        error.name === "AbortError" ? "请求超时，正在核对二维码状态。" : error.message, "err");
    } finally {
      if (state.qrRefreshing === operation) state.qrRefreshing = false;
      if (version === state.loginVersion && session === state.loginSession) await pollLogin();
    }
  });

  async function finishLogin(data, version) {
    if (loginSettling(data)) return;
    clearInterval(state.loginTimer);
    state.loginTimer = null;
    try { await refreshAccounts(loginRequest); }
    catch (error) {
      if (version === state.loginVersion && data.session_id === state.loginSession) {
        setMsg($("#login-connection-msg"), "登录结果已确认，正在重新读取账号列表…", "");
        state.loginTimer = setInterval(pollLogin, 2500);
      }
      return;
    }
    if (version !== state.loginVersion || data.session_id !== state.loginSession) return;
    if (data.status === "success") {
      const reused = data.login_source === "saved_session" || String(data.message || "").startsWith("已有登录状态有效");
      toast(`账号 ${accountLabel(data.account || "")} ${reused ? "已恢复登录" : "登录成功"}`, "ok");
      if (addingAccount && data.new_account) {
        unmountAddLogin();
        accountControls.openAccount(data.account, { discardConfirmed: true });
        navigate("account-overview", data.account);
        return;
      }
      const accountsTab = tabs.find((tab) => tab.dataset.tab === "accounts");
      if (accountsTab?.getAttribute("aria-selected") === "true"
          && !$("#tab-login").classList.contains("hidden")
          && $("#login-account").value === data.account) navigate("account-overview", data.account);
    }
  }

  async function pollLogin() {
    if (!state.loginSession || state.loginPolling === state.loginVersion || state.loginCancelling) return;
    const sessionId = state.loginSession;
    const version = state.loginVersion;
    state.loginPolling = version;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const data = await api(`/api/login/${encodeURIComponent(sessionId)}?wait_seconds=2`, { signal: controller.signal });
      if (version !== state.loginVersion || sessionId !== state.loginSession) return;
      showLogin(data);
      if (["success", "failed", "expired", "cancelled"].includes(data.status)) {
        await finishLogin(data, version);
      }
    } catch (e) {
      if (version === state.loginVersion) loginProgress.connectionLost();
    } finally {
      clearTimeout(timeout);
      if (state.loginPolling === version) state.loginPolling = null;
    }
  }

  async function startLogin(newAccount = false, proxyRef = "") {
    if (loginStarting || state.loginCancelling || (state.loginSession && loginNeedsPolling(loginSms.payload))) return;
    loginStarting = true;
    const version = ++state.loginVersion;
    if (newAccount) {
      addProxyRef = proxyRef;
      $("#account-add-prepare").classList.add("hidden");
      $("#account-add-login").classList.remove("hidden");
      $("#tab-login").classList.remove("hidden");
    }
    const account = $("#login-account").value;
    $("#login-account").disabled = true;
    $("#btn-login-start").disabled = true;
    $("#account-add-retry").disabled = true;
    $("#login-qr").classList.add("hidden");
    $("#login-sms").classList.add("hidden");
    $("#login-url").replaceChildren();
    $("#login-qr-refresh").classList.add("hidden");
    state.loginSession = null;
    $("#btn-login-cancel").classList.add("hidden");
    loginProgress.reset();
    loginProgress.render({ status: "starting", stage: "browser", elapsed_seconds: 0,
      new_account: newAccount }, newAccount ? "" : loginAccountLabel(account));
    setMsg($("#login-msg"), "正在启动登录，请稍候…", "");
    try {
      // wait_seconds 0：后端启动任务后立即返回，二维码交给下面的轮询接上；
      // 弹窗因此马上进入可交互状态，「关闭/取消」不用等浏览器起来。
      const connectBody = { wait_seconds: 0 };
      if (proxyRef) connectBody.proxy = proxyRef;
      const data = await api(newAccount ? "/api/accounts/connect" : "/api/login/start", {
        method: "POST",
        body: JSON.stringify(newAccount ? connectBody : { account, wait_seconds: 0 }),
      });
      if (state.addClosePending) {
        state.addClosePending = false;
        const ref = addProxyRef;
        unmountAddLogin();
        if (data.session_id) cancelLoginInBackground(data.session_id, data, ref);
        return;
      }
      if (version !== state.loginVersion) return;
      showLogin(data);
      if (state.loginTimer) clearInterval(state.loginTimer);
      state.loginTimer = null;
      if (loginNeedsPolling(data)) state.loginTimer = setInterval(pollLogin, 2500);
      else await finishLogin(data, version);
    } catch (e) {
      if (state.addClosePending) {
        state.addClosePending = false;
        unmountAddLogin();
        toast(e.message, "err");
        return;
      }
      if (version !== state.loginVersion) return;
      showLogin({ session_id: null, status: "failed", message: e.message, account,
        new_account: newAccount });
    } finally {
      loginStarting = false;
      $("#account-add-retry").disabled = loginSettling(loginSms.payload);
      $("#account-add-edit-proxy").disabled = loginSettling(loginSms.payload);
    }
  }
  $("#btn-login-start").addEventListener("click", () => startLogin());

  async function cancelLogin() {
    if (addingAccount) { closeAddAccount(); return; }
    if (!state.loginSession || state.loginCancelling || loginSms.payload?.status === "finalizing") return;
    const version = ++state.loginVersion;
    state.loginCancelling = true;
    $("#btn-login-cancel").disabled = true;
    $("#account-add-close").disabled = true;
    setMsg($("#login-msg"), "正在取消登录并关闭浏览器…", "");
    try {
      const data = await loginRequest(`/api/login/${encodeURIComponent(state.loginSession)}/cancel`, {
        method: "POST",
        body: "{}",
      });
      if (version !== state.loginVersion) return;
      showLogin(data);
      if (state.loginTimer) clearInterval(state.loginTimer);
      state.loginTimer = null;
      if (addingAccount && ["cancelled", "expired", "failed"].includes(data.status)) unmountAddLogin();
      else if (data.status === "success") await finishLogin(data, version);
      else state.loginTimer = setInterval(pollLogin, 2500);
    } catch (e) {
      $("#btn-login-cancel").disabled = false;
      setMsg($("#login-msg"), e.message, "err");
    } finally {
      state.loginCancelling = false;
      $("#account-add-close").disabled = false;
    }
  }
  $("#btn-login-cancel").addEventListener("click", cancelLogin);

  // 去重拒绝后的「前往已有账号」：关掉添加弹窗，切到那个账号的概况。
  $("#login-url").addEventListener("click", (event) => {
    const button = event.target.closest("[data-duplicate-goto]");
    if (!button) return;
    const name = button.dataset.duplicateGoto;
    $("#login-url").replaceChildren();
    if (addingAccount) unmountAddLogin();
    accountControls.openAccount(name, { discardConfirmed: true });
    navigate("account-overview", name);
  });

  function setJobBusy(busy) {
    state.jobBusy = busy;
    for (const id of ["btn-run-now", "account-check"]) {
      $("#" + id).disabled = busy;
    }
  }

  function renderJob(job) {
    $("#run-result").textContent = JSON.stringify(job, null, 2);
    $("#job-details").classList.remove("hidden");
    const box = $("#job-summary");
    box.className = "job-summary";
    const result = job.result || {};
    const skipped = job.exit_code === 75 || result.busy;
    const label = skipped ? "本次未执行" : jobLabel(job);
    const title = result.dry_run ? "测试" : kindLabel(job.kind);
    const parts = [`<div class="section-title"><h3>${esc(title)}</h3><span class="pill ${label === "执行成功" ? "ok" : label === "执行失败" ? "bad" : ""}">${esc(label)}</span></div>`];
    if (job.kind === "check" && job.started_at) parts.push(`<p class="hint">检测时间：${esc(displayTime(job.started_at))}</p>`);
    if (emptyRun(job)) parts.push(`<p class="hint">${esc(emptyRunReason())}</p>`);
    else if (job.message || result.message) parts.push(`<p>${esc(job.message || result.message)}</p>`);
    (result.accounts || []).forEach((account) => {
      const name = account.account || account.name;
      parts.push(`<div class="section-title"><h3>${esc(accountLabel(name))}</h3></div>`);
      if (job.kind === "check") {
        const loginState = account.page_state === "ready"
          ? account.ok === true ? "已登录" : "待核实"
          : { logged_out: "登录失效", verify: "需要安全验证", unknown: "页面未就绪" }[account.page_state] || "尚未确认";
        parts.push(`<dl><dt>检查结果</dt><dd>${account.ok === true ? "通过" : account.ok === false || account.error ? "需处理" : "检查中"}</dd><dt>登录状态</dt><dd>${esc(loginState)}</dd><dt>当前 IP</dt><dd>${esc(account.egress_ip_now || "暂未获取")}</dd><dt>IP 一致性</dt><dd>${account.ip_consistent === true ? "一致" : account.ip_consistent === false ? "不一致" : "待检查"}</dd></dl>`);
        if (account.ok === false) parts.push(`<p class="msg err">${esc(account.health?.reason || account.error || "检查未通过，请稍后重试。")}</p>`);
      } else {
        const rows = (Array.isArray(account.results) ? account.results : []).filter(item => item && typeof item.friend === "string");
        const prior = new Set([
          ...(Array.isArray(account.already_sent) ? account.already_sent : []),
          ...rows.filter(item => item.outcome === "already_sent").map(item => item.friend),
        ]);
        const previousDay = new Set(rows.filter(item => item.outcome === "previous_day_sent").map(item => item.friend));
        const newlySent = (account.sent || []).length;
        const failed = Object.keys(account.failed || {}).length;
        const accountState = job.state === "finished" ? verdictLabel(account.verdict) : jobLabel(job);
        parts.push(`<p class="hint">${esc(accountState)} · ${result.dry_run ? "测试对象" : "本次发送"} ${newlySent} 位${prior.size ? ` · 今日已发送，本次跳过 ${prior.size} 位` : ""}${previousDay.size ? ` · 跨日前已发送，本次跳过 ${previousDay.size} 位` : ""} · 失败 ${failed} 位</p>`);
        if (!result.dry_run && prior.size && !previousDay.size && !newlySent && !failed && account.verdict === "done") {
          parts.push('<p class="hint">本次对象今日已完成，无需重复发送。</p>');
        }
        const shown = new Set(rows.map(item => item.friend));
        for (const friend of prior) {
          if (!shown.has(friend)) {
            rows.push({ friend, outcome: "already_sent", reason: "今日已发送，本次跳过；同一对象每天只发送一次。" });
            shown.add(friend);
          }
        }
        for (const [friend, reason] of Object.entries(account.failed || {})) {
          if (!shown.has(friend)) { rows.push({ friend, outcome: "failed", reason }); shown.add(friend); }
        }
        for (const sentFriend of account.sent || []) {
          const friend = result.dry_run && sentFriend.endsWith("(dry-run)")
            ? sentFriend.slice(0, -"(dry-run)".length) : sentFriend;
          if (!shown.has(friend)) { rows.push({ friend, outcome: result.dry_run ? "found" : "ok" }); shown.add(friend); }
        }
        if (rows.length) parts.push('<ul class="task-recipient-results" aria-label="对象执行结果">');
        rows.forEach(item => {
          const uncertain = ["sending", "unconfirmed"].includes(item.outcome);
          const success = ["ok", "sent", "dry_run", "found", "already_sent", "previous_day_sent"].includes(item.outcome);
          const label = uncertain || item.held ? "失败"
            : item.outcome === "already_sent" ? "今日已发送，本次跳过"
            : item.outcome === "previous_day_sent" ? "跨日前已发送，本次跳过"
            : result.dry_run && success ? "测试通过"
            : success ? "本次已发送" : outcomeLabel(item.outcome);
          parts.push(`<li><div class="recipient-result-heading"><strong>${esc(item.friend)}</strong><span class="pill ${success && !item.held ? "ok" : ""}">${esc(label)}</span></div>`);
          if (success && !item.held) {
            if (item.reason) parts.push(`<p class="hint">${esc(item.reason)}</p>`);
          } else {
            const failure = window.deliveryFailure(item);
            parts.push(`<p>原因：${esc(failure.reason)}</p><p class="hint">${esc(failure.guidance)}</p>`);
            parts.push(failureActionMarkup(failure, name));
            if (item.reason && item.reason !== failure.reason) parts.push(`<details class="failure-details"><summary>原始错误</summary><pre>${esc(item.reason)}</pre></details>`);
          }
          parts.push('</li>');
        });
        if (rows.length) parts.push('</ul>');
      }
      if (account.error || account.note) {
        const note = String(account.error || account.note);
        parts.push(`<p class="msg err">${esc(note.includes("待核对") ? "部分对象发送失败，已停止重复尝试。" : note)}</p>`);
      }
    });
    if (job.state === "running") parts.push('<p class="hint">任务正在后台执行，结果会自动更新。</p>');
    box.innerHTML = parts.join("");
  }

  async function observeJob(job) {
    renderJob(job);
    state.jobId = job.job_id;
    if (job.state === "running" && job.job_id) {
      state.jobTimer = setTimeout(pollJob, 2000);
      return;
    }
    setJobBusy(false);
    const skipped = job.exit_code === 75 || job.result?.busy;
    const empty = emptyRun(job);
    // 空任务按当前账号的实际状态归因，不用后端那段「请检查…」清单；状态行与结果卡显示同一句。
    const runMessage = empty
      ? emptyRunReason()
      : job.message || (skipped ? "已有任务在执行，本次未执行。" : jobLabel(job));
    setMsg($("#run-msg"), runMessage, empty || skipped ? "" : job.exit_code === 0 ? "ok" : "err");
    try { await refreshAll(); }
    catch (error) { toast("任务已结束，但概览未更新：" + error.message, "err"); }
  }

  async function pollJob() {
    state.jobTimer = null;
    try {
      await observeJob(await api(`/api/jobs/${encodeURIComponent(state.jobId)}?wait_seconds=1`));
    } catch (error) {
      setMsg($("#run-msg"), "暂时无法更新结果：" + error.message + "。正在重试…", "err");
      state.jobTimer = setTimeout(pollJob, 5000);
    }
  }

  async function startJob(path, body) {
    if (state.jobBusy) return;
    if (state.jobTimer) clearTimeout(state.jobTimer);
    state.jobTimer = null;
    setJobBusy(true);
    setMsg($("#run-msg"), "正在提交任务…", "");
    try {
      const data = await api(path, { method: "POST", body: JSON.stringify(body) });
      await observeJob(data);
    } catch (error) {
      setJobBusy(false);
      setMsg($("#run-msg"), error.message, "err");
    }
  }

  $("#btn-run-now").addEventListener("click", () => {
    const account = $("#account-edit-name").value;
    if (!account) return;
    state.lastRunAccount = account;
    if (accountControls.hasDraft()) {
      setMsg($("#run-msg"), "发送使用已保存的配置；请先保存或撤销修改。", "err"); return;
    }
    const count = (state.accounts.find(a => a.name === account)?.friends || []).length;
    if (!confirm(`确认执行「${accountLabel(account)}」的今日发送任务？仅向名单中的 ${count} 位对象发送，今日已发送成功的对象会自动跳过。`)) return;
    startJob("/api/run", { account, dry_run: false, wait_seconds: 3 });
  });

  function formatRunStamp(stamp) {
    const m = stamp.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})(?:-(\d+))?$/);
    if (!m) return stamp;
    const [, y, mo, d, h, mi, s, n] = m;
    const sameYear = new Date().getFullYear() === Number(y);
    const base = (sameYear ? `${mo}-${d}` : `${y}-${mo}-${d}`) + ` ${h}:${mi}:${s}`;
    return n ? `${base} 第${n}次` : base;
  }

  function runDayLabel(iso) {
    const now = new Date();
    const isoOf = (dt) => `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
    if (iso === isoOf(now)) return "今天";
    const yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    if (iso === isoOf(yesterday)) return "昨天";
    const m = iso && iso.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!m) return "更早";
    return Number(m[1]) === now.getFullYear() ? `${Number(m[2])}月${Number(m[3])}日` : iso;
  }

  function renderRunOptions(sel, runs) {
    const selected = sel.value;
    if (selected && !runs.includes(selected)) runs = [...runs, selected];
    sel.innerHTML = '<option value="">最近一次</option>';
    let group = null;
    let day = "";
    for (const run of runs) {
      const m = run.match(/^(\d{4})(\d{2})(\d{2})-/);
      const iso = m ? `${m[1]}-${m[2]}-${m[3]}` : "";
      if (iso !== day) {
        day = iso;
        group = document.createElement("optgroup");
        group.label = runDayLabel(iso);
        sel.appendChild(group);
      }
      const option = document.createElement("option");
      option.value = run;
      option.textContent = formatRunStamp(run);
      group.appendChild(option);
    }
    sel.value = selected;
  }

  window.createLogViewer({
    api,
    formatRunStamp,
    onRuns(runs) {
      const select = $("#logs-run");
      const current = select.value;
      renderRunOptions(select, runs);
      if (runs.includes(current)) select.value = current;
    },
  });

  $("#btn-refresh").addEventListener("click", async () => {
    $("#btn-refresh").disabled = true;
    try { await refreshAll(); toast("状态已更新", "ok"); }
    catch (error) { toast(error.message, "err"); }
    finally { $("#btn-refresh").disabled = false; }
  });
  $("#btn-logout").addEventListener("click", async () => {
    try {
      await api("/auth/logout", { method: "POST" });
      redirectToLogin();
    } catch (error) {
      toast(error.message, "err");
    }
  });
  window.createPanelSettings({ api, onPasswordChanged: redirectToLogin });
  window.createUpdateControls({ api });

  const workspace = window.createAccountWorkspace({
    isLoginActive: name => $("#login-account").disabled && $("#login-account").value === name,
    onSelect(name) {
      if (!switchManagedAccount(name)) return false;
      syncManagedAccount(name);
      workspace.selectSection("overview");
      return true;
    },
    onSection(section) {
      if (section === "login") navigate("login", $("#account-edit-name").value);
      else workspace.selectSection(section);
    },
    onToggle: toggleAccountEnabled,
  });
  const accountControls = window.createAccountControls({ api, startJob, refreshAll, navigate,
    isTargetMutationActive: name => targetMutations.has(name),
    isScheduleDraft: () => scheduleState.dirty || scheduleState.saving,
    onScheduleChanged(info) {
      scheduleState.revision += 1;
      renderSchedule(info);
    },
    onAccountDeleted: message => toast(message, "ok"),
    onAccountSelected: syncManagedAccount,
  });
  let backgroundRefreshing = false;
  const healthTimer = setInterval(async () => {
    if (document.hidden || backgroundRefreshing) return;
    backgroundRefreshing = true;
    try { await refreshAll(); } catch (error) { toast(error.message, "err"); }
    finally { backgroundRefreshing = false; }
  }, 10000);

  window.addEventListener("pagehide", () => {
    clearInterval(healthTimer);
    clearTimeout(state.jobTimer);
    clearInterval(state.loginTimer);
    clearInterval(state.fetchTimer);
  });

  refreshAll().catch((e) => {
    $("#status-box").textContent = e.message;
  });
})();
