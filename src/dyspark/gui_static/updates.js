/* Version metadata is untrusted text. Only the checked commit may be requested. */
window.createUpdateControls = ({ api }) => {
  const $ = selector => document.querySelector(selector);
  const active = new Set(["queued", "preparing", "switching", "verifying", "recovering"]);
  const labels = {
    queued: "更新请求已提交，正在等待管理器…",
    preparing: "正在准备新版本，暂时停止启动任务和修改设置…",
    switching: "正在切换版本并重启服务…",
    verifying: "正在验证新版本是否正常运行…",
    recovering: "新版本未能启动，正在恢复旧版本…",
    complete: "更新完成，服务已通过健康检查。",
    restored: "更新未完成，已恢复原版本。",
    failed: "更新失败，原服务继续运行。",
    recovery_failed: "旧版恢复失败，任务保持暂停。请按恢复说明检查部署日志。",
  };
  let info = null, timer = null, busy = false, stopped = false, reconnecting = false, actionError = "";
  const short = value => /^[0-9a-f]{40}$/.test(value || "") ? value.slice(0, 12) : "无法识别";

  function render(value) {
    info = value;
    const pending = active.has(value.phase);
    $("#update-current").textContent = short(value.current);
    $("#update-latest").textContent = value.latest ? short(value.latest) : "尚未获取";
    $("#update-checked").textContent = value.checked_at
      ? new Date(value.checked_at * 1000).toLocaleString("zh-CN", { hour12: false }) : "尚未检查";
    $("#update-summary").textContent = value.summary || "";
    $("#update-badge").textContent = pending ? "更新中" : value.checking ? "检查中"
      : value.latest && value.latest !== value.current ? "有新版本" : value.latest ? "已是最新" : "等待检查";
    $("#update-progress").textContent = labels[value.phase] || (value.managed
      ? "每 6 小时自动检查，更新后需要重新登录面板。"
      : "当前为普通启动方式。启用更新管理器后，即可一键更新并重启。配置与登录状态会保留。");
    $("#update-error").textContent = [value.check_error, value.error, actionError].filter(Boolean).join("；");
    $("#update-check").disabled = busy || pending || value.checking;
    $("#update-apply").disabled = busy || !value.can_update;
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try { return await api(path, { ...options, signal: controller.signal }); }
    finally { clearTimeout(timeout); }
  }

  function schedule() {
    if (stopped) return;
    clearTimeout(timer);
    timer = setTimeout(refresh, active.has(info?.phase) || reconnecting ? 2000 : 10000);
  }

  async function refresh() {
    if (busy || document.hidden) { schedule(); return; }
    try {
      const value = await request("/api/update");
      reconnecting = false;
      render(value);
    } catch (error) {
      reconnecting = active.has(info?.phase) || reconnecting;
      $("#update-error").textContent = reconnecting
        ? "服务正在重启或暂时无法连接，正在等待恢复。重新连接后请登录查看更新结果。"
        : "无法读取更新状态：" + error.message;
      $("#update-apply").disabled = true;
    } finally { schedule(); }
  }

  async function action(path, body) {
    if (busy) return;
    busy = true;
    actionError = "";
    if (info) render(info);
    try {
      render(await request(path, { method: "POST", body: JSON.stringify(body || {}) }));
    } catch (error) {
      actionError = error.message;
      $("#update-error").textContent = actionError;
    } finally {
      busy = false;
      // Re-read after errors, too: a request may have reached the server before disconnecting.
      await refresh();
    }
  }

  $("#update-check").addEventListener("click", () => action("/api/update/check"));
  $("#update-apply").addEventListener("click", () => {
    if (!info?.can_update || busy) return;
    if (!window.confirm("更新期间会暂停任务并短暂重启服务，配置和登录状态会保留。未保存的页面编辑会丢失。继续更新吗？")) return;
    action("/api/update/apply", { target: info.latest });
  });
  window.addEventListener("pagehide", () => { stopped = true; clearTimeout(timer); });
  refresh();
};
