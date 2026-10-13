/* Account workspace renders one account; identifiers remain separate from labels. */
window.accountDisplayName = account => account?.remark || account?.nickname || account?.display_name || account?.name || "";
window.accountNameDuplicated = (account, accounts = []) =>
  accounts.filter(item => window.accountDisplayName(item) === window.accountDisplayName(account)).length > 1;
window.accountOptionLabel = (account, accounts = []) => {
  const label = window.accountDisplayName(account);
  return window.accountNameDuplicated(account, accounts) ? `${label}（${account.name}）` : label;
};
/* 列表一格放不下完整标识：自动标识省掉固定前缀，留下能区分账号的部分。 */
window.accountShortId = name => String(name || "").replace(/^account_/, "");
/* 详情标题已经显示的字段不再重复：标题是备注时补昵称，标题不是内部标识时补标识。 */
window.accountIdentityNote = account => {
  const label = window.accountDisplayName(account);
  const parts = [];
  if (account?.nickname && account.nickname !== label) parts.push(`抖音昵称：${account.nickname}`);
  if (account?.name && account.name !== label) parts.push(account.name);
  return parts.join(" · ");
};
/* 运行时结论 → 界面用词。用户意愿由 enabled 单独表达，这里不出现。 */
window.accountVerdicts = {
  logged_out: { label: "登录失效", fix: "重新登录", needsLogin: true },
  blocked: { label: "需要安全验证", fix: "重新登录", needsLogin: true },
  ip_mismatch: { label: "出口 IP 已变化", fix: "重新登录", needsLogin: true },
  network_error: { label: "上次网络检查失败", fix: "", needsLogin: false },
  corrupt: { label: "状态记录损坏", fix: "", needsLogin: false },
};
window.accountHealthLabel = status =>
  window.accountVerdicts[status]?.label || { ready: "环境正常", unknown: "尚未核对" }[status] || "尚未核对";
window.accountConnectionState = account => {
  const verdict = window.accountVerdicts[account?.health?.status];
  if (!account?.logged_in) return { kind: "new", label: "未登录", requiresLogin: true, actionLabel: "扫码登录" };
  if (verdict) return { kind: "problem", label: verdict.label, requiresLogin: verdict.needsLogin, actionLabel: verdict.fix };
  return { kind: "connected", label: "已登录", requiresLogin: false, actionLabel: "" };
};
/* 运行结论里的说明只在异常结论时才算原因：正常账号的 reason 是上一次的成功说明。 */
window.accountProblemReason = account => {
  const health = account?.health || {};
  return ["ready", "unknown"].includes(health.status || "unknown") ? "" : String(health.reason || "");
};

/* 计划相关的时间只说「下一次」：按服务器时区说今天／明天，缺时间基准时退回绝对时间。 */
window.scheduleTimeLabel = (value, now) => {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!match) return String(value || "");
  const day = `${match[1]}-${match[2]}-${match[3]}`;
  const time = `${match[4]}:${match[5]}`;
  const today = String(now || "").slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(today)) return `${day} ${time}`;
  const tomorrow = new Date(Date.parse(`${today}T00:00:00Z`) + 86400000).toISOString().slice(0, 10);
  if (day === today) return `今天 ${time}`;
  if (day === tomorrow) return `明天 ${time}`;
  return `${Number(match[2])} 月 ${Number(match[3])} 日 ${time}`;
};

window.createAccountWorkspace = ({ onSelect, onSection, onToggle, isLoginActive = () => false }) => {
  const $ = selector => document.querySelector(selector);
  const sections = new Set(["overview", "targets", "login", "messages", "settings"]);
  const compactAccounts = window.matchMedia("(max-width: 1100px)");
  let accounts = [], selected = "", section = "overview", pickerOpen = false;

  function selectSection(value) {
    if (!sections.has(value)) return;
    const account = accounts.find(item => item.name === selected);
    if (value === "login" && !window.accountConnectionState(account).requiresLogin && !isLoginActive(selected)) value = "overview";
    section = value;
    document.querySelectorAll("[data-account-pane]").forEach(pane => {
      pane.classList.toggle("hidden", pane.dataset.accountPane !== section);
    });
    document.querySelectorAll("[data-account-section]").forEach(button => {
      const active = button.dataset.accountSection === section;
      button.setAttribute("aria-pressed", String(active));
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    $("#account-edit-form").classList.toggle("hidden", !["targets", "messages", "settings"].includes(section));
  }

  function renderList(resetScroll = false) {
    const list = $("#account-directory");
    const scrollTop = resetScroll ? 0 : list.scrollTop;
    const active = document.activeElement;
    const focused = active?.closest(".account-list-item")?.querySelector("[data-select-account]")?.dataset.selectAccount;
    const focusedToggle = active?.matches('[role="switch"]');
    const query = $("#account-search").value.trim().toLocaleLowerCase();
    const filter = $("#account-filter").value;
    list.replaceChildren();
    const filtered = accounts.filter(account => {
      const matchesQuery = [account.name, account.nickname, account.remark, account.display_name]
        .some(value => String(value || "").toLocaleLowerCase().includes(query));
      const enabled = account.enabled !== false;
      const matchesFilter = filter === "all" || (filter === "enabled" && enabled)
        || (filter === "disabled" && !enabled)
        || (filter === "attention" && window.accountConnectionState(account).kind !== "connected");
      return matchesQuery && matchesFilter;
    });
    for (const account of filtered) {
      const item = document.createElement("div");
      item.className = "account-list-item";
      const main = document.createElement("button");
      main.type = "button"; main.className = "account-list-main";
      main.dataset.selectAccount = account.name;
      main.setAttribute("aria-pressed", String(account.name === selected));
      if (account.name === selected) main.setAttribute("aria-current", "true");
      const avatar = document.createElement("span"); avatar.className = "account-list-avatar"; avatar.setAttribute("aria-hidden", "true");
      window.renderAvatar(avatar, window.accountDisplayName(account), account.avatar_url);
      const copy = document.createElement("span"); copy.className = "account-list-copy";
      const title = document.createElement("span"); title.className = "account-list-name";
      title.textContent = window.accountDisplayName(account);
      title.title = window.accountOptionLabel(account, accounts);
      copy.append(title);
      // 同名账号靠内部标识区分；标识单独一行，避免名称被逐字折断。
      if (window.accountNameDuplicated(account, accounts)) {
        const id = document.createElement("span"); id.className = "account-list-id";
        id.textContent = window.accountShortId(account.name);
        copy.append(id);
      }
      const conn = window.accountConnectionState(account);
      const info = document.createElement("span"); info.className = "account-list-meta";
      const countText = `${account.friends?.length || 0} 位对象`;
      const meta = [countText, account.enabled === false ? "已停用" : "", conn.kind === "connected" ? "" : conn.label].filter(Boolean);
      for (const text of meta) {
        const part = document.createElement("span"); part.className = "account-meta-part"; part.textContent = text;
        info.append(part);
      }
      info.title = meta.join(" · ");
      copy.append(info);
      main.append(avatar, copy);
      const paused = account.enabled === false;
      const toggle = document.createElement("button");
      toggle.type = "button"; toggle.className = "switch" + (paused ? "" : " on");
      toggle.setAttribute("role", "switch"); toggle.setAttribute("aria-checked", String(!paused));
      toggle.setAttribute("aria-label", (paused ? "启用 " : "停用 ") + window.accountOptionLabel(account, accounts));
      toggle.addEventListener("click", () => onToggle?.(account.name, paused));
      item.append(main, toggle); list.append(item);
      if (focused === account.name) (focusedToggle ? toggle : main).focus({ preventScroll: true });
    }
    if (!filtered.length) {
      const message = document.createElement("p"); message.className = "hint";
      message.textContent = accounts.length ? "没有匹配的账号" : "暂无抖音账号。";
      list.append(message);
    }
    list.scrollTop = scrollTop;
    $("#account-directory-count").textContent = query || filter !== "all" ? `${filtered.length} / ${accounts.length}` : `${accounts.length} 个`;
  }

  function revealSelectedAccount() {
    const list = $("#account-directory");
    const button = [...list.querySelectorAll("[data-select-account]")].find(item => item.dataset.selectAccount === selected);
    if (!button) return;
    const card = button.closest(".account-list-item").getBoundingClientRect();
    const rail = list.getBoundingClientRect();
    if (!rail.height) return;
    if (card.top < rail.top) list.scrollTop -= rail.top - card.top;
    else if (card.bottom > rail.bottom) list.scrollTop += card.bottom - rail.bottom;
  }

  function setPickerOpen(open, restoreFocus = false) {
    pickerOpen = open;
    $(".account-rail").classList.toggle("picker-open", open);
    $("#account-picker-toggle").setAttribute("aria-expanded", String(open));
    $("#account-search").value = "";
    $("#account-filter").value = "all";
    renderList();
    if (open) {
      revealSelectedAccount();
      const button = [...$("#account-directory").querySelectorAll("[data-select-account]")].find(item => item.dataset.selectAccount === selected);
      (button || $("#account-search")).focus({ preventScroll: true });
    } else if (restoreFocus && compactAccounts.matches) $("#account-picker-toggle").focus({ preventScroll: true });
  }
  compactAccounts.addEventListener("change", () => setPickerOpen(false));

  function render(data, name) {
    accounts = data || [];
    const changed = selected !== name;
    if (changed) { selected = name; selectSection("overview"); }
    renderList();
    if (changed) revealSelectedAccount();
    const account = accounts.find(item => item.name === selected);
    $("#account-detail").classList.toggle("hidden", !account);
    $("#account-empty").classList.toggle("hidden", !!account);
    $("#account-picker-name").textContent = account ? window.accountOptionLabel(account, accounts) : "选择账号";
    $("#account-picker-toggle").setAttribute("aria-label", account ? `切换账号，当前：${window.accountOptionLabel(account, accounts)}` : "选择账号");
    window.renderAvatar($("#account-picker-avatar"), window.accountDisplayName(account), account?.avatar_url);
    $("#account-picker-meta").textContent = account ? [account.enabled === false ? "已停用" : "", window.accountConnectionState(account).label].filter(Boolean).join(" · ") : "暂无账号";
    if (!account) return;
    const label = window.accountDisplayName(account);
    const connection = window.accountConnectionState(account);
    const loginActive = isLoginActive(selected);
    $("#account-section-login").classList.toggle("hidden", !connection.requiresLogin && !loginActive);
    if (section === "login" && !connection.requiresLogin && !loginActive) selectSection("overview");
    $("#account-detail-name").textContent = label;
    window.renderAvatar($("#account-avatar"), label, account.avatar_url);
    $("#account-detail-subtitle").textContent = window.accountIdentityNote(account);
    const needsAttention = connection.kind !== "connected";
    $("#account-connection-badge").textContent = connection.label;
    $("#account-connection-badge").className = `pill ${needsAttention ? "bad" : ""}`;
    $("#account-connection-badge").classList.toggle("hidden", !needsAttention);
    $("#account-enabled-badge").classList.toggle("hidden", account.enabled !== false);
    $("#account-login").classList.toggle("hidden", !connection.requiresLogin && !loginActive);
    $("#account-login").textContent = loginActive ? "继续登录验证" : connection.actionLabel;
    // 登录失效时回答「怎么办」：走重新登录，不是添加账号。
    $("#account-login-hint").textContent = "";
    const targetCount = account.friends?.length || 0;
    $("#run-hint").textContent = targetCount ? `${targetCount} 位发送对象` : "未配置对象";
  }

  $("#account-search").addEventListener("input", () => renderList(true));
  $("#account-filter").addEventListener("change", () => renderList(true));
  $("#account-picker-toggle").addEventListener("click", () => setPickerOpen(!pickerOpen));
  $(".account-rail").addEventListener("keydown", event => {
    if (event.key !== "Escape" || !pickerOpen || !compactAccounts.matches) return;
    event.preventDefault();
    setPickerOpen(false, true);
  });
  $("#account-directory").addEventListener("click", event => {
    const button = event.target.closest("[data-select-account]");
    if (button && onSelect(button.dataset.selectAccount) === true && compactAccounts.matches) setPickerOpen(false, true);
  });
  $("#account-sections").addEventListener("click", event => {
    const button = event.target.closest("[data-account-section]");
    if (button) onSection(button.dataset.accountSection);
  });
  $("#account-sections").addEventListener("keydown", event => {
    const button = event.target.closest("[data-account-section]");
    if (!button) return;
    const visible = [...$("#account-sections").querySelectorAll("button")].filter(item => !item.classList.contains("hidden"));
    const index = visible.indexOf(button);
    let next;
    if (["ArrowRight", "ArrowDown"].includes(event.key)) next = (index + 1) % visible.length;
    if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = (index - 1 + visible.length) % visible.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = visible.length - 1;
    if (next === undefined) return;
    event.preventDefault();
    onSection(visible[next].dataset.accountSection);
    visible[next].focus();
  });
  return { render, selectSection, currentSection: () => section };
};
