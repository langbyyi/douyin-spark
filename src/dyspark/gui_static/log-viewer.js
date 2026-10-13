(() => {
  const levels = { DEBUG: "调试", INFO: "信息", WARNING: "警告", ERROR: "错误", CRITICAL: "严重" };

  window.parseLogLines = lines => {
    const entries = [];
    for (const line of lines) {
      const match = line.match(/^(?:(\d{4}-\d{2}-\d{2}) )?(\d{2}:\d{2}:\d{2}(?:[,.]\d+)?)\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+([^:\s]+):\s?(.*)$/);
      if (match) {
        const [, date = "", time, level, logger, message] = match;
        entries.push({ date, time, level, logger, message, rawLines: [line] });
      } else if (entries.length) {
        entries[entries.length - 1].message += "\n" + line;
        entries[entries.length - 1].rawLines.push(line);
      } else {
        entries.push({ date: "", time: "", level: "", logger: "", message: line, rawLines: [line] });
      }
    }
    return entries;
  };

  window.createLogViewer = ({ api, formatRunStamp, onRuns }) => {
    const $ = selector => document.querySelector(selector);
    const box = $("#logs-box"), raw = $("#logs-raw");
    const refreshButton = $("#btn-logs"), rawToggle = $("#logs-raw-toggle");
    const title = $("#logs-title"), updated = $("#logs-updated"), count = $("#logs-count");
    const columns = $(".logs-column-head");
    let rawMode = false;
    let requestId = 0;
    let hasLines = false;
    let hasMatches = false;
    let entries = [];
    let originalLines = [];
    let controller = null;

    function applyView() {
      const showRaw = rawMode && hasMatches;
      box.classList.toggle("hidden", showRaw);
      raw.classList.toggle("hidden", !showRaw);
      columns.classList.toggle("hidden", showRaw || !hasMatches);
      rawToggle.setAttribute("aria-pressed", String(rawMode));
      rawToggle.textContent = rawMode ? "易读视图" : "原始日志";
      rawToggle.disabled = !hasLines;
    }

    function showMessage(message, failed = false) {
      const element = document.createElement("p");
      element.id = "logs-empty";
      element.className = "logs-empty" + (failed ? " err" : "");
      element.setAttribute("role", failed ? "alert" : "status");
      element.textContent = message;
      box.replaceChildren(element);
      raw.textContent = "";
      hasLines = false;
      hasMatches = false;
      count.classList.add("hidden");
      applyView();
    }

    function renderLines(lines, displayLines = lines) {
      originalLines = lines;
      entries = window.parseLogLines(lines);
      const readable = window.parseLogLines(displayLines);
      if (readable.length === entries.length) {
        entries.forEach((entry, index) => { entry.displayMessage = readable[index].message; });
      }
      renderFiltered();
    }

    function renderFiltered() {
      if (!entries.length) return;
      const selectedLevel = $("#logs-level").value;
      const keyword = $("#logs-search").value.trim().toLocaleLowerCase();
      const matches = entries.filter(entry => {
        const levelMatch = !selectedLevel || (selectedLevel === "problems"
          ? ["WARNING", "ERROR", "CRITICAL"].includes(entry.level)
          : selectedLevel === "ERROR" ? ["ERROR", "CRITICAL"].includes(entry.level) : entry.level === selectedLevel);
        const searchable = [entry.date, entry.time, entry.level, levels[entry.level], entry.logger, entry.message, entry.displayMessage].join(" ").toLocaleLowerCase();
        return levelMatch && (!keyword || searchable.includes(keyword));
      });
      const fragment = document.createDocumentFragment();
      for (const entry of matches) {
        const row = document.createElement("div");
        row.className = "log-entry" + (entry.level ? ` log-entry-${entry.level.toLowerCase()}` : " log-entry-plain");
        const time = document.createElement("time");
        time.className = "log-time";
        time.title = [entry.date, entry.time].filter(Boolean).join(" ");
        if (entry.date) {
          time.dateTime = `${entry.date}T${entry.time.replace(",", ".")}`;
          const date = document.createElement("span");
          date.className = "log-date";
          date.textContent = entry.date.slice(5);
          time.appendChild(date);
        }
        time.appendChild(document.createTextNode(entry.time.slice(0, 8) || "—"));
        const level = document.createElement("span");
        level.className = "log-level";
        level.textContent = levels[entry.level] || "文本";
        level.title = entry.level || "未带级别的日志";
        const content = document.createElement("div");
        content.className = "log-content";
        if (entry.logger) {
          const logger = document.createElement("span");
          logger.className = "log-logger";
          logger.textContent = entry.logger;
          content.appendChild(logger);
        }
        const message = document.createElement("span");
        message.className = "log-message";
        message.textContent = entry.displayMessage ?? entry.message;
        content.appendChild(message);
        row.append(time, level, content);
        fragment.appendChild(row);
      }
      box.replaceChildren(fragment);
      if (!matches.length) {
        const empty = document.createElement("p");
        empty.id = "logs-empty";
        empty.className = "logs-empty";
        empty.setAttribute("role", "status");
        empty.textContent = "没有匹配的日志";
        box.appendChild(empty);
      }
      box.scrollTop = 0;
      raw.textContent = matches.flatMap(entry => entry.rawLines).join("\n");
      hasLines = true;
      hasMatches = matches.length > 0;
      count.textContent = selectedLevel || keyword
        ? `${matches.length} / ${entries.length} 条 · ${originalLines.length} 行`
        : `${originalLines.length} 行`;
      count.classList.remove("hidden");
      applyView();
    }

    async function refresh() {
      const currentRequest = ++requestId;
      controller?.abort();
      const currentController = new AbortController();
      controller = currentController;
      const signal = currentController.signal;
      entries = [];
      originalLines = [];
      const service = $("#logs-service").value;
      const run = $("#logs-run").value;
      const sourceLabel = $("#logs-service").selectedOptions[0].textContent;
      const limit = service ? 200 : 120;
      $("#logs-run-label").hidden = Boolean(service);
      $("#logs-limit").textContent = `最多显示最近 ${limit} 行`;
      title.textContent = service ? sourceLabel : run ? formatRunStamp(run) : "最近一次运行";
      title.removeAttribute("title");
      updated.textContent = "正在加载…";
      refreshButton.disabled = true;
      refreshButton.textContent = "刷新中…";
      box.setAttribute("aria-busy", "true");
      showMessage("正在加载日志…");
      const query = service
        ? `?service=${encodeURIComponent(service)}&lines=${limit}`
        : run ? `?run=${encodeURIComponent(run)}&lines=${limit}` : `?lines=${limit}`;
      let timer;
      try {
        const timeout = new Promise((resolve, reject) => {
          timer = setTimeout(() => { reject(new Error("日志读取超时，请刷新重试")); currentController.abort(); }, 15000);
        });
        const data = await Promise.race([api("/api/logs" + query, { signal }), timeout]);
        if (currentRequest !== requestId) return;
        if (!service && Array.isArray(data.recent_runs)) onRuns(data.recent_runs);
        title.textContent = service ? sourceLabel : data.run ? formatRunStamp(data.run) : "最近一次运行";
        if (data.run && !service) title.title = data.run;
        const lines = Array.isArray(data.lines) ? data.lines : [];
        if (lines.length) renderLines(lines, Array.isArray(data.display_lines) ? data.display_lines : lines);
        else showMessage(service ? "暂无服务日志，服务产生新记录后可刷新查看。" : "暂无运行日志，执行一次发送或检查后可刷新查看。");
        updated.textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
      } catch (error) {
        if (currentRequest !== requestId) return;
        title.textContent = "加载失败";
        updated.textContent = "请稍后刷新重试";
        showMessage(error.message || "无法读取日志", true);
      } finally {
        clearTimeout(timer);
        if (currentRequest === requestId) {
          refreshButton.disabled = false;
          refreshButton.textContent = "刷新";
          box.setAttribute("aria-busy", "false");
        }
      }
    }

    refreshButton.addEventListener("click", refresh);
    $("#logs-service").addEventListener("change", refresh);
    $("#logs-run").addEventListener("change", refresh);
    $("#logs-level").addEventListener("change", renderFiltered);
    $("#logs-search").addEventListener("input", renderFiltered);
    rawToggle.addEventListener("click", () => { rawMode = !rawMode; applyView(); });
    return { refresh };
  };
})();
