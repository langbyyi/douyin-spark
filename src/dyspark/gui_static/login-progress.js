(() => {
  const terminal = status => ["success", "failed", "expired", "cancelled"].includes(status);
  const redact = text => String(text || "").replace(/(\w+:\/\/)[^/\s@]+@/g, "$1***@");
  const labels = { browser: "启动浏览器", connection: "检查网络连接", page: "打开登录页",
    loading: "加载登录组件", scan: "等待扫码", confirm: "在手机上确认", verify: "完成验证", save: "保存账号" };
  function failureMessage(raw) {
    if (/407|ERR_INVALID_AUTH_CREDENTIALS|authentication|认证/i.test(raw)) return "代理认证失败，请检查用户名和密码。";
    if (/ERR_PROXY|ERR_TUNNEL|代理.*失败/i.test(raw)) return "无法连接代理，请检查地址或更换代理。";
    if (/timeout|超时/i.test(raw)) return "登录页加载超时，请重试。";
    if (/net::|Page\.|BrowserType\.|connect|TLS/i.test(raw)) return "登录页连接失败，请检查网络后重试。";
    return /[\u4e00-\u9fff]/.test(raw) ? redact(raw).slice(0, 180) : "登录未完成，请重试。";
  }
  window.createLoginProgress = ({ setMsg }) => {
    const $ = id => document.getElementById(id);
    let current = null, observedAt = 0, elapsed = 0, imageKey = "", imageRetry = 0, failedImageKey = "";
    function imageFailed() {
      $("login-qr").classList.add("hidden");
      $("login-qr-placeholder").classList.remove("hidden");
      $("login-qr-placeholder").querySelector("strong").textContent = "图片加载失败";
      $("login-qr-placeholder").querySelector("p").textContent = "点击下方按钮重新加载";
      $("login-image-retry").classList.remove("hidden");
    }
    $("login-qr").addEventListener("error", () => {
      if (!imageKey) return;
      failedImageKey = imageKey; imageFailed();
    });
    $("login-image-retry").addEventListener("click", () => {
      imageKey = ""; failedImageKey = ""; imageRetry += 1;
      if (current) render(current, $("login-session-account").textContent);
    });
    function tick() {
      if (!current || terminal(current.status)) {
        $("login-elapsed").textContent = "";
        $("login-loading-hint").classList.add("hidden");
        $("login-loading-hint").textContent = "";
        return;
      }
      const seconds = Math.max(0, Math.floor(elapsed + (Date.now() - observedAt) / 1000));
      $("login-elapsed").textContent = `已等待 ${seconds < 60 ? seconds + " 秒" : Math.floor(seconds / 60) + " 分 " + seconds % 60 + " 秒"}`;
      const loading = ["starting", "pending"].includes(current.status) && !current.qr_path;
      const slow = loading && seconds >= 20;
      $("login-loading-hint").classList.toggle("hidden", !slow);
      $("login-loading-hint").textContent = slow ? "页面加载较慢，请稍候。" : "";
    }
    function render(payload, accountLabel) {
      const previous = current;
      current = payload;
      const ended = terminal(payload.status);
      const changed = previous?.session_id !== payload.session_id;
      if (changed || Number.isFinite(payload.elapsed_seconds)) {
        observedAt = Date.now(); elapsed = Math.max(0, payload.elapsed_seconds || 0);
      }
      if (changed) { imageKey = ""; failedImageKey = ""; $("login-error-details").open = false; $("login-route-details").open = false; }
      $("login-progress").classList.remove("hidden");
      $("login-progress").dataset.ended = String(ended);
      const stage = { scanned: "confirm", sms_required: "verify", verification_required: "verify", finalizing: "save" }[payload.status]
        || (payload.status === "pending" && payload.qr_path ? "scan" : payload.stage || (payload.status === "pending" ? "loading" : "browser"));
      $("login-progress").dataset.loading = String(!ended && ["browser", "connection", "page", "loading", "save"].includes(stage));
      const reused = payload.login_source === "saved_session" || String(payload.message || "").startsWith("已有登录状态有效");
      $("login-progress-title").textContent = { success: reused ? "已恢复已有登录" : "登录成功", failed: "登录失败", expired: "登录已过期", cancelled: "登录已取消" }[payload.status] || labels[stage] || "正在登录";
      $("login-session-account").textContent = accountLabel || "新账号";
      $("login-session-line").classList.toggle("hidden", !!payload.new_account);
      const raw = payload.message || payload.status_text || "";
      const message = payload.status === "failed" ? failureMessage(raw)
        : payload.status === "expired" ? "登录已过期，请重新登录。"
        : payload.status === "cancelled" ? "登录已取消。"
        : /^正在(启动|打开|加载|获取|检查)|^等待扫码/.test(raw) ? "" : redact(raw);
      setMsg($("login-msg"), message, ["failed", "expired"].includes(payload.status) ? "err" : payload.status === "success" ? "ok" : "");
      setMsg($("login-connection-msg"), "", "");
      const failed = payload.status === "failed";
      $("login-error-details").classList.toggle("hidden", !failed || !raw);
      $("login-error-detail").textContent = failed ? redact(raw).slice(0, 2000) : "";
      $("login-route-details").classList.toggle("hidden", !payload.session_id || (!payload.proxy && !payload.route_known));
      $("login-route").textContent = payload.proxy ? `代理 ${redact(payload.proxy)}` : "直接连接";
      const img = $("login-qr");
      const verification = payload.status === "verification_required";
      const path = verification ? payload.screen_path : payload.qr_path;
      const visible = !ended && path && ["pending", "scanned", "verification_required"].includes(payload.status);
      img.classList.toggle("hidden", !visible);
      img.alt = verification ? "抖音安全验证画面" : "抖音登录二维码";
      if (visible) {
        const version = verification ? payload.screen_version : payload.qr_version;
        const key = `${payload.session_id}|${path}|${version || 0}|${imageRetry}`;
        if (imageKey !== key) {
          imageKey = key;
          failedImageKey = "";
          img.src = "/api/media?path=" + encodeURIComponent(path) + "&v=" + (version || 0) + "&r=" + imageRetry;
        }
      } else { imageKey = ""; img.removeAttribute("src"); }
      $("login-qr-placeholder").classList.toggle("hidden", ended || payload.status === "finalizing");
      const panel = $("login-qr").closest(".login-qr-panel");
      panel.classList.toggle("hidden", (ended && !payload.duplicate_of) || payload.status === "finalizing");
      panel.classList.toggle("login-terminal", ended);
      const placeholder = $("login-qr-placeholder");
      placeholder.querySelector("strong").textContent = "正在准备二维码";
      placeholder.querySelector("p").textContent = "二维码出现后使用抖音 App 扫码";
      $("login-image-retry").classList.add("hidden");
      if (visible && imageKey === failedImageKey) imageFailed();
      $("login-qr-refresh").classList.toggle("hidden", ended || payload.status !== "pending" || !payload.qr_path);
      $("btn-login-start").textContent = ended && payload.status !== "success" ? "重新登录" : "开始登录";
      tick();
    }
    function reset() {
      current = null; imageKey = ""; failedImageKey = ""; elapsed = 0;
      for (const id of ["login-progress", "login-error-details", "login-route-details", "login-qr", "login-qr-refresh", "login-image-retry"]) $(id).classList.add("hidden");
      $("login-qr").removeAttribute("src");
      $("login-qr").closest(".login-qr-panel").classList.remove("hidden", "login-terminal");
      $("login-qr-placeholder").classList.remove("hidden");
      $("login-qr-placeholder").querySelector("strong").textContent = "二维码登录";
      $("login-qr-placeholder").querySelector("p").textContent = "开始后使用抖音扫码";
      $("login-error-detail").textContent = "";
      setMsg($("login-msg"), "尚未开始", "");
      setMsg($("login-connection-msg"), "", "");
      $("btn-login-start").textContent = "开始登录";
    }
    return { render, reset, tick,
      retryImage() { imageKey = ""; imageRetry += 1; },
      connectionLost() { setMsg($("login-connection-msg"), "暂时无法获取登录状态，正在重连…", ""); },
    };
  };
})();
