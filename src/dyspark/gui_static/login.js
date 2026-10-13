(() => {
  const form = document.querySelector("#panel-login");
  const password = document.querySelector("#panel-password");
  const reveal = document.querySelector("#login-reveal");
  const error = document.querySelector("#login-error");
  const button = document.querySelector("#login-submit");
  const SUBMIT_LABEL = button.textContent;
  let retryTimer = null;

  function message(text, kind) {
    error.textContent = text || "";
    error.className = "msg" + (kind ? " " + kind : "");
  }

  // 会话过期由 app.js 以 ?expired=1 跳转过来；提示一次后抹掉参数，刷新不再重复。
  const query = new URLSearchParams(window.location.search);
  if (query.get("expired") === "1") {
    message("登录会话已过期，请重新登录。", "info");
    query.delete("expired");
    const rest = query.toString();
    history.replaceState(null, "", window.location.pathname + (rest ? "?" + rest : ""));
  }
  try {
    if (sessionStorage.getItem("spark-password-changed") === "1") {
      message("密码已修改，请使用新密码登录。", "info");
      sessionStorage.removeItem("spark-password-changed");
    }
  } catch { /* Storage is optional. */ }

  reveal.addEventListener("click", () => {
    const shown = password.type === "text";
    password.type = shown ? "password" : "text";
    reveal.textContent = shown ? "显示" : "隐藏";
    reveal.title = shown ? "显示密码" : "隐藏密码";
    reveal.setAttribute("aria-pressed", String(!shown));
    password.focus();
  });

  // 触发限流后按 Retry-After 倒计时，避免用户在锁定窗口内反复提交。
  function waitBeforeRetry(response) {
    const header = Number(response.headers.get("Retry-After"));
    let left = Number.isFinite(header) && header > 0 ? Math.ceil(Math.min(header, 600)) : 60;
    button.disabled = true;
    const paint = () => {
      if (left <= 0) {
        retryTimer = null;
        button.disabled = false;
        button.textContent = SUBMIT_LABEL;
        return;
      }
      button.textContent = left + " 秒后可重试";
      left -= 1;
      retryTimer = setTimeout(paint, 1000);
    };
    paint();
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (retryTimer) return;
    button.disabled = true;
    button.textContent = "正在验证…";
    message("");
    let response;
    try {
      response = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ password: password.value }),
        credentials: "same-origin",
      });
    } catch {
      button.disabled = false;
      button.textContent = SUBMIT_LABEL;
      message("无法连接面板服务，请确认面板仍在运行。", "err");
      password.focus();
      return;
    }
    const result = await response.json().catch(() => ({}));
    if (response.ok) {
      password.value = "";
      window.location.replace("/");
      return;
    }
    if (response.status === 429) {
      message(result.error || "登录尝试过于频繁，请稍后重试", "err");
      waitBeforeRetry(response);
      password.focus();
      return;
    }
    button.disabled = false;
    button.textContent = SUBMIT_LABEL;
    message(result.error || (response.status === 401 ? "密码错误" : "登录失败，请稍后重试"), "err");
    password.focus();
  });
})();
