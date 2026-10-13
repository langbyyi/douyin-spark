/* Account editor owns its draft; background status updates never replace it.
   显示名与账号状态由 account-workspace.js 统一定义，本文件不重复实现。 */
window.createAccountControls = ({ api, startJob, refreshAll, navigate, onAccountSelected, onAccountDeleted, isScheduleDraft = () => false, isTargetMutationActive = () => false, onScheduleChanged }) => {
  const $ = selector => document.querySelector(selector);
  let accounts = [], globalTemplates = [], selected = "", dirty = false, saving = false, selectionVersion = 0;
  let draftBaseline = null;
  let schedulerBusy = false;
  let schedulerInfo = {}, schedulerNow = "";
  const msg = (id, value) => { $(id).textContent = value || ""; };
  const current = () => accounts.find(a => a.name === selected);
  const templateEditor = window.createMessageTemplateEditor({
    container: $("#account-template-list"), firstInput: $("#account-templates"),
    addButton: $("#account-template-add"), inheritButton: $("#account-template-inherit"),
    globalContainer: $("#account-global-templates"), globalList: $("#account-global-template-list"), customizeButton: $("#account-template-customize"),
    presetContainer: $("#account-template-presets"),
    variables: $("#account-template-variables"), count: $("#account-template-count"), state: $("#account-template-state"),
    onChange: draftChanged,
  });
  function selectAccount(name) {
    if (name !== selected) {
      selectionVersion += 1;
    }
    selected = name;
  }
  function openAccount(name, { discardConfirmed = false } = {}) {
    if (saving || !accounts.some(a => a.name === name)) return false;
    if (name === selected) return true;
    if (dirty && !discardConfirmed && !confirm("切换账号会放弃未保存的修改，继续吗？")) return false;
    selectAccount(name); $("#account-edit-name").value = selected;
    msg("#account-edit-msg", ""); loadEditor();
    return true;
  }
  const editorConfig = account => JSON.stringify([account?.name, account?.remark, account?.friends, account?.templates, account?.proxy_ref, account?.proxy_editable]);
  function draftChanged() {
    dirty = !!draftBaseline && (JSON.stringify(templateEditor.values()) !== draftBaseline.templates || $("#account-friends").value !== draftBaseline.friends || $("#account-remark").value !== draftBaseline.remark || $("#account-proxy-ref").value !== draftBaseline.proxy);
    msg("#account-edit-msg", ""); buttons();
  }
  function templateError(error) {
    msg("#account-edit-msg", error.message);
    if (error.input) { navigate?.("messages", selected); error.input.focus(); }
  }
  function buttons() {
    $("#account-friends").disabled = saving || isTargetMutationActive(selected);
    templateEditor.setDisabled(saving || !current());
    $("#account-save").disabled = saving || !dirty || !current();
    $("#account-reset").disabled = saving || !dirty;
    $("#account-save-bar").classList.toggle("hidden", !dirty && !saving);
    $("#account-draft-status").textContent = saving ? "保存中…" : dirty ? "未保存" : "";
    $("#account-save").textContent = saving ? "保存中…" : "保存修改";
    $("#account-edit-name").disabled = saving;
    $("#account-delete").disabled = saving || !current();
  }
  function renderHealth() {
    const account = current();
    const box = $("#account-health");
    box.replaceChildren();
    if (!account) return;
    const h = account.health || {};
    const connection = window.accountConnectionState(account);
    const status = document.createElement("div");
    status.className = "connection-summary";
    const label = document.createElement("strong");
    label.textContent = connection.label;
    status.appendChild(label);
    if (!connection.requiresLogin) {
      const checkedState = document.createElement("span");
      checkedState.className = "hint";
      checkedState.textContent = window.accountHealthLabel(h.status);
      status.appendChild(checkedState);
    }
    box.appendChild(status);
    // 能不能发由发送结果说话：需要人处理的结论已由标题旁的标记和下面的按钮表达，
    // 这里只补程序自己会重试的异常（网络失败、记录损坏）与它们的具体原因。
    const reason = window.accountProblemReason(account);
    if (reason && !window.accountConnectionState(account).requiresLogin) {
      const p = document.createElement("p"); p.className = "hint"; p.textContent = reason; box.append(p);
    }
    const details = document.createElement("details"); details.className = "health-details";
    const summary = document.createElement("summary"); summary.textContent = "连接详情";
    details.append(summary);
    const checked = h.checked_at ? new Date(h.checked_at) : null;
    const checkedLabel = checked && !Number.isNaN(checked.getTime())
      ? checked.toLocaleString("zh-CN", { hour12: false }) : "暂无记录";
    for (const value of [
      `登录时出口 IP：${account.egress_ip_at_login || "未记录"}`,
      `最近检查出口 IP：${h.current_ip || "未检查"}`,
      `状态更新时间：${checkedLabel}`,
    ].filter(Boolean)) {
      const p = document.createElement("p"); p.className = "hint"; p.textContent = value; details.append(p);
    }
    box.append(details);
    onAccountSelected?.(selected);
  }
  function loadEditor() {
    const account = current();
    if (!account) return;
    $("#account-remark").value = account.remark || "";
    $("#account-friends").value = (account.friends || []).join("\n");
    templateEditor.write(account.templates || []);
    // 显示裸名称（MAIN_PROXY），保存原样提交，由后端规范成 ${MAIN_PROXY} 存储。
    $("#account-proxy-ref").value = (account.proxy_ref || "").replace(/^\$\{/, "").replace(/\}$/, "");
    $("#account-proxy-ref").disabled = !account.proxy_editable;
    draftBaseline = {
      remark: $("#account-remark").value,
      friends: $("#account-friends").value,
      templates: JSON.stringify(templateEditor.values()),
      proxy: $("#account-proxy-ref").value,
    };
    dirty = false; buttons(); renderHealth();
  }
  function updateAccounts(data) {
    const previousEditor = editorConfig(current());
    globalTemplates = data.global_templates || [];
    templateEditor.updateGlobals(globalTemplates);
    templateEditor.updatePresets(data.message_presets || []);
    accounts = data.accounts || [];
    const select = $("#account-edit-name");
    select.replaceChildren();
    for (const account of accounts) {
      const option = document.createElement("option"); option.value = account.name; option.textContent = window.accountOptionLabel(account, accounts); select.append(option);
    }
    if (!accounts.some(a => a.name === selected)) { selectAccount(accounts[0]?.name || ""); dirty = false; }
    select.value = selected;
    if (!dirty && !saving && (!draftBaseline || previousEditor !== editorConfig(current()))) loadEditor();
    else {
      if (dirty && !saving && draftBaseline && $("#account-friends").value === draftBaseline.friends) {
        $("#account-friends").value = (current()?.friends || []).join("\n");
        draftBaseline.friends = $("#account-friends").value;
      }
      buttons(); renderHealth();
    }
  }
  function updateTargets(name, friends) {
    const account = accounts.find(item => item.name === name);
    if (!account || !Array.isArray(friends)) return;
    account.friends = [...friends];
    if (name === selected && draftBaseline && $("#account-friends").value === draftBaseline.friends) {
      $("#account-friends").value = friends.join("\n");
      draftBaseline.friends = $("#account-friends").value;
    }
  }
  $("#account-edit-name").addEventListener("change", event => {
    if (!openAccount(event.target.value)) event.target.value = selected;
  });
  $("#account-edit-form").addEventListener("input", draftChanged);
  $("#account-reset").addEventListener("click", () => { loadEditor(); msg("#account-edit-msg", ""); });
  const lines = value => value.split("\n").map(s => s.trim()).filter(Boolean);
  $("#account-edit-form").addEventListener("submit", async event => {
    event.preventDefault(); if (saving || !dirty || !current()) return;
    let templates;
    const templatesChanged = JSON.stringify(templateEditor.values()) !== draftBaseline.templates;
    try { if (templatesChanged) templates = templateEditor.read(); }
    catch (error) { templateError(error); return; }
    saving = true; buttons();
    $("#account-edit-form").querySelectorAll("input, textarea").forEach(el => { el.disabled = true; });
    const account = current();
    const body = { account: selected };
    if ($("#account-remark").value.trim() !== draftBaseline.remark) body.remark = $("#account-remark").value.trim();
    if ($("#account-friends").value !== draftBaseline.friends) body.friends = lines($("#account-friends").value);
    if (templatesChanged) body.templates = templates;
    if (account.proxy_editable && $("#account-proxy-ref").value.trim() !== draftBaseline.proxy) body.proxy = $("#account-proxy-ref").value.trim();
    let committed = false;
    try {
      const result = await api("/api/accounts/update", { method: "POST", body: JSON.stringify(body) });
      committed = true;
      const saved = accounts.find(item => item.name === body.account);
      if (saved) {
        for (const field of ["remark", "friends", "templates"]) if (Object.hasOwn(body, field)) saved[field] = body[field];
        if (Object.hasOwn(body, "proxy")) saved.proxy_ref = body.proxy;
      }
      dirty = false; msg("#account-edit-msg", result.message); await refreshAll();
    } catch (error) { msg("#account-edit-msg", committed ? `配置已保存，但刷新失败：${error.message}` : error.message); }
    finally {
      saving = false;
      $("#account-edit-form").querySelectorAll("input, textarea").forEach(el => { el.disabled = false; });
      $("#account-proxy-ref").disabled = !current()?.proxy_editable;
      $("#account-friends").disabled = isTargetMutationActive(selected);
      if (committed) loadEditor(); else buttons();
    }
  });
  $("#account-delete").addEventListener("click", async () => {
    const account = current();
    if (saving || !account) return;
    if (!confirm(`删除「${window.accountDisplayName(account)}」？将移除账号和对象配置，停止后续发送。登录资料与历史记录保留。${dirty ? "未保存的修改将丢弃。" : ""}`)) return;
    const version = selectionVersion;
    saving = true; buttons(); msg("#account-delete-msg", "正在删除…");
    try {
      const result = await api("/api/accounts/delete", { method: "POST", body: JSON.stringify({ account: account.name }) });
      if (selected === account.name) { dirty = false; selectAccount(""); }
      // 删除已完成，留在面板上的「正在删除…」会跟着下一个账号显示，清掉；结果由提示条说明。
      msg("#account-delete-msg", "");
      saving = false;
      await refreshAll();
      onAccountDeleted?.(result.message);
    } catch (error) { if (version === selectionVersion) msg("#account-delete-msg", error.message); }
    finally { saving = false; buttons(); }
  });
  $("#account-check").addEventListener("click", () => {
    if (dirty) { msg("#run-msg", "检查使用已保存配置；请先保存或撤销修改"); return; }
    msg("#run-msg", "");
    startJob("/api/check", { account: selected, wait_seconds: 3 });
  });
  $("#account-login").addEventListener("click", () => {
    if (navigate) { navigate("login", selected); return; }
    if (!$("#login-account").disabled) $("#login-account").value = selected;
    document.querySelector('[data-tab="accounts"]').click();
    $("#tab-login").classList.remove("hidden");
  });
  function updateScheduler(info, now) {
    schedulerInfo = info;
    if (now) schedulerNow = now;
    $("#scheduler-badge").textContent = info.enabled ? "已启用" : "已停用";
    $("#scheduler-badge").className = `pill ${info.enabled ? "ok" : ""}`;
    $("#scheduler-next").textContent = !info.enabled ? "未启用"
      : info.next_run ? window.scheduleTimeLabel(info.next_run, schedulerNow) : "待安排";
    $("#scheduler-status").textContent = info.active_job ? "任务执行中" : !info.enabled ? "已停用"
      : info.running && info.leader ? "等待执行" : "服务未就绪";
    $("#scheduler-last").textContent = info.last_trigger ? window.scheduleTimeLabel(info.last_trigger, schedulerNow) : "暂无";
    $("#scheduler-error").textContent = info.error || "";
    const active = accounts.filter(a => a.enabled !== false);
    const names = active.map(a => window.accountDisplayName(a));
    // 账号多的时候只列前几个，完整名单放进 title。
    $("#scheduler-accounts").textContent = active.length
      ? `${active.length} 个 · ${names.slice(0, 6).join("、")}${names.length > 6 ? " 等" : ""}`
      : "暂无";
    $("#scheduler-accounts").title = names.join("、");
    const toggle = $("#scheduler-switch");
    toggle.classList.toggle("on", !!info.enabled);
    toggle.setAttribute("aria-checked", String(!!info.enabled));
    toggle.disabled = schedulerBusy || isScheduleDraft();
    $("#schedule-next-runs").classList.toggle("hidden", !info.enabled || !$("#schedule-next-runs").textContent);
  }
  async function switchSchedule(enabled) {
    if (schedulerBusy || isScheduleDraft()) return;
    if (enabled && !confirm("按每日计划自动发送消息，确认启用？")) return;
    schedulerBusy = true;
    const toggle = $("#scheduler-switch");
    toggle.disabled = true;
    toggle.setAttribute("aria-busy", "true");
    msg("#scheduler-msg", "");
    try {
      await api("/api/schedule/control", { method: "POST", body: JSON.stringify({ enabled }) });
      updateScheduler({ ...schedulerInfo, enabled });
      onScheduleChanged(await api("/api/schedule"));
      msg("#scheduler-msg", "");
    }
    catch (error) { msg("#scheduler-msg", error.message); }
    finally {
      schedulerBusy = false;
      toggle.setAttribute("aria-busy", "false");
      updateScheduler(schedulerInfo);
    }
  }
  $("#scheduler-switch").addEventListener("click", () => switchSchedule(!schedulerInfo.enabled));
  let savedKey = "", savedToken = "", notificationInfo = null, notificationBusy = false;
  let notificationRevision = 0, notificationRequestId = 0;
  function notificationControls() {
    for (const id of ["notify-save", "notify-switch", "notify-clear-serverchan", "notify-clear-pushplus"]) {
      $("#" + id).disabled = notificationBusy;
    }
    const ready = notificationInfo?.enabled && (notificationInfo.push_configured || notificationInfo.pushplus_configured);
    $("#notification-test").disabled = notificationBusy || !ready;
  }
  function beginNotificationAction() {
    if (notificationBusy) return false;
    notificationBusy = true; notificationRevision += 1; notificationControls();
    return true;
  }
  async function endNotificationAction() {
    notificationRevision += 1; notificationBusy = false; notificationControls();
    await refreshNotifications().catch(error => msg("#notification-msg", error.message));
  }
  async function refreshNotifications() {
    const revision = notificationRevision, requestId = ++notificationRequestId;
    const result = await api("/api/notifications");
    if (revision !== notificationRevision || requestId !== notificationRequestId || notificationBusy) return;
    renderNotifications(result);
  }
  function renderNotifications(result) {
    notificationInfo = result;
    const channels = [];
    if (result.push_configured) channels.push(`Server酱·尾号 ${result.push_key_tail}`);
    if (result.pushplus_configured) channels.push(`PushPlus·尾号 ${result.pushplus_token_tail}`);
    const pushState = !result.enabled
      ? "已关闭，执行结果不会推送"
      : channels.length
        ? `已开启 · ${channels.join("、")}`
        : "已开启，但尚未配置渠道";
    const failure = result.last_failure;
    const when = failure ? (failure.at || "").slice(5, 16).replace("T", " ") : "";
    const failureNote = failure
      ? `；最近推送失败：${when} ${failure.title || "通知"}${failure.detail ? `（${failure.detail}）` : ""}`
      : "";
    msg("#notification-state", pushState + failureNote);
    const notifySwitch = $("#notify-switch");
    notifySwitch.classList.toggle("on", !!result.enabled);
    notifySwitch.setAttribute("aria-checked", String(!!result.enabled));
    // 输入框明文回显已保存的 key/token；与上次服务端值不同的内容视为正在编辑的草稿，不覆盖。
    const keyInput = $("#notify-key"), tokenInput = $("#notify-pushplus");
    if (keyInput.value === savedKey) keyInput.value = result.serverchan_key || "";
    if (tokenInput.value === savedToken) tokenInput.value = result.pushplus_token || "";
    savedKey = result.serverchan_key || "";
    savedToken = result.pushplus_token || "";
    notificationControls();
    $("#notification-test").title = !result.enabled
      ? "先打开右上角开关"
      : channels.length ? "" : "请先在上方保存任一推送渠道";
    $("#notify-clear-serverchan").classList.toggle("hidden", !result.push_configured);
    $("#notify-clear-pushplus").classList.toggle("hidden", !result.pushplus_configured);
  }
  async function clearChannel(which) {
    const label = which === "serverchan" ? "Server酱" : "PushPlus";
    if (notificationBusy) return;
    if (!confirm(`确定移除${label}的已存配置？移除后该渠道不再接收推送。`)) return;
    if (!beginNotificationAction()) return;
    try {
      const result = await api("/api/notifications/save", {
        method: "POST",
        body: JSON.stringify({
          [which === "serverchan" ? "clear_serverchan" : "clear_pushplus"]: true,
          enabled: $("#notify-switch").classList.contains("on"),
        }),
      });
      if (typeof result.enabled === "boolean") renderNotifications(result);
      msg("#notification-msg", result.message);
    } catch (error) { msg("#notification-msg", error.message); }
    finally { await endNotificationAction(); }
  }
  $("#notify-switch").addEventListener("click", async () => {
    const notifySwitch = $("#notify-switch");
    const enabled = !notifySwitch.classList.contains("on");
    if (!beginNotificationAction()) return;
    try {
      const result = await api("/api/notifications/save", {
        method: "POST",
        body: JSON.stringify({ enabled }),
      });
      if (typeof result.enabled === "boolean") renderNotifications(result);
      msg("#notification-msg", result.message);
    } catch (error) {
      msg("#notification-msg", error.message);
    } finally { await endNotificationAction(); }
  });
  $("#notify-clear-serverchan").addEventListener("click", () => clearChannel("serverchan"));
  $("#notify-clear-pushplus").addEventListener("click", () => clearChannel("pushplus"));
  $("#notification-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!beginNotificationAction()) return;
    try {
      const result = await api("/api/notifications/save", {
        method: "POST",
        body: JSON.stringify({
          serverchan_key: $("#notify-key").value.trim(),
          pushplus_token: $("#notify-pushplus").value.trim(),
          enabled: $("#notify-switch").classList.contains("on"),
        }),
      });
      if (typeof result.enabled === "boolean") renderNotifications(result);
      msg("#notification-msg", result.message);
    } catch (error) { msg("#notification-msg", error.message); }
    finally { await endNotificationAction(); }
  });
  $("#notification-test").addEventListener("click", async () => {
    if (!beginNotificationAction()) return;
    try { const result = await api("/api/notifications/test", { method: "POST", body: "{}" }); msg("#notification-msg", result.message); }
    catch (error) { msg("#notification-msg", error.message); }
    finally { await endNotificationAction(); }
  });
  return { updateAccounts, updateTargets, updateScheduler, refreshNotifications, openAccount, hasDraft: () => dirty,
    hasTargetDraft: () => !!draftBaseline && $("#account-friends").value !== draftBaseline.friends, isSaving: () => saving };
};
