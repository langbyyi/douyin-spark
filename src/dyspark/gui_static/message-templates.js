/* Each input represents one message, including all of its line breaks. */
window.createMessageTemplateEditor = ({ container, firstInput, addButton, inheritButton, globalContainer, globalList, customizeButton, presetContainer, variables, count, state, onChange }) => {
  const maximum = 100;
  let entries = [], disabled = false, activeInput = null, inherited = true, globalTemplates = [];
  const length = value => Array.from(value).length;
  let presets = [], presetButtons = [];
  const emptyEditor = () => entries.length === 1 && !entries[0].input.value.trim();
  const currentTexts = () => inherited ? globalTemplates : emptyEditor() ? [] : entries.map(entry => entry.input.value);
  const additionsFor = preset => {
    const existing = new Set(currentTexts().map(text => text.trim()));
    return preset.templates.filter(text => !existing.has(text.trim()));
  };
  function copyGlobalsToDraft() {
    inherited = false;
    entries = globalTemplates.map((text, index) => createEntry(text, index === 0 ? firstInput : undefined));
  }

  function renderPresets() {
    presetContainer.replaceChildren(); presetButtons = [];
    presets.forEach(preset => {
      const card = document.createElement("section"); card.className = "template-preset-card";
      const label = document.createElement("strong"); label.textContent = preset.name;
      const description = document.createElement("p"); description.className = "hint"; description.textContent = preset.description;
      const example = document.createElement("p"); example.className = "template-preset-example"; example.textContent = preset.example;
      const button = document.createElement("button"); button.type = "button"; button.className = "ghost";
      button.dataset.templatePreset = preset.id; button.textContent = `添加${preset.name}`;
      button.addEventListener("click", () => {
        const additions = additionsFor(preset);
        if (disabled || !additions.length || currentTexts().length + additions.length > maximum) return;
        if (inherited) copyGlobalsToDraft();
        else if (emptyEditor()) entries = [];
        const start = entries.length;
        additions.forEach(text => entries.push(createEntry(text, entries.length ? undefined : firstInput)));
        render(); activeInput = entries[start].input; activeInput.focus(); onChange();
      });
      presetButtons.push({ button, preset }); card.append(label, description, example, button); presetContainer.append(card);
    });
  }

  function updateCounts() {
    entries.forEach(entry => {
      const size = length(entry.input.value);
      entry.counter.textContent = `${size} / 2000`;
      entry.counter.classList.toggle("err", size > 2000);
      entry.input.setAttribute("aria-invalid", String(size > 2000));
    });
    count.textContent = `${inherited ? globalTemplates.length : entries.length} / ${maximum}`;
    state.textContent = inherited ? "使用全局模板" : entries.length === 1 && !entries[0].input.value.trim() ? "留空使用全局模板" : "账号专用文案";
  }

  function renderGlobals() {
    container.classList.toggle("hidden", inherited);
    globalContainer.classList.toggle("hidden", !inherited);
    globalList.replaceChildren();
    globalTemplates.forEach((text, index) => {
      const item = document.createElement("section"); item.className = "inherited-template-item";
      const label = document.createElement("strong"); label.textContent = `文案 ${index + 1}`;
      const content = document.createElement("p"); content.className = "template-content"; content.textContent = text;
      item.append(label, content); globalList.append(item);
    });
    if (!globalTemplates.length) {
      const message = document.createElement("p"); message.className = "hint"; message.textContent = "全局文案尚未加载，请刷新页面";
      globalList.append(message);
    }
  }

  function updateControls() {
    presetButtons.forEach(({ button, preset }) => {
      const additions = additionsFor(preset);
      button.disabled = disabled || !additions.length || (inherited && !globalTemplates.length) || currentTexts().length + additions.length > maximum;
      button.title = !additions.length ? "这些文案已在当前模板中" : currentTexts().length + additions.length > maximum ? "添加后将超过 100 条模板" : "添加到本账号所有发送对象共用的模板";
    });
    addButton.disabled = disabled || (inherited ? globalTemplates.length >= maximum : entries.length >= maximum);
    inheritButton.disabled = disabled || inherited;
    customizeButton.disabled = disabled || !globalTemplates.length || globalTemplates.length > maximum;
    variables.querySelectorAll("button").forEach(button => { button.disabled = disabled || inherited; });
    entries.forEach((entry, index) => {
      entry.input.disabled = disabled || inherited;
      entry.remove.disabled = disabled;
      entry.copy.disabled = disabled || entries.length >= maximum;
      entry.up.disabled = disabled || index === 0;
      entry.down.disabled = disabled || index === entries.length - 1;
    });
  }

  function render() {
    container.replaceChildren();
    entries.forEach((entry, index) => {
      entry.input.id = index === 0 ? "account-templates" : `account-template-${index + 1}`;
      entry.label.htmlFor = entry.input.id;
      entry.label.textContent = `消息模板 ${index + 1}`;
      for (const [action, label] of [["remove", "移除"], ["copy", "复制"], ["up", "上移"], ["down", "下移"]]) {
        entry[action].setAttribute("aria-label", `${label}消息模板 ${index + 1}`);
        entry[action].title = `${label}消息模板 ${index + 1}`;
      }
      container.append(entry.row);
    });
    updateControls();
    updateCounts();
    renderGlobals();
  }

  function createEntry(value, input = document.createElement("textarea")) {
    const row = document.createElement("div"); row.className = "message-template";
    const label = document.createElement("label");
    const heading = document.createElement("div"); heading.className = "message-template-heading";
    const counter = document.createElement("span"); counter.className = "template-char-count hint";
    heading.append(label, counter);
    const actions = document.createElement("div"); actions.className = "message-template-actions";
    const controls = {};
    for (const [action, text] of [["up", "↑"], ["down", "↓"], ["copy", "复制"], ["remove", "移除"]]) {
      const button = document.createElement("button"); button.type = "button";
      button.className = "ghost"; button.textContent = text; button.dataset.templateAction = action;
      controls[action] = button; actions.append(button);
    }
    input.rows = 3; input.removeAttribute("maxlength"); input.value = value;
    input.placeholder = "填写一条消息，可换行";
    const entry = { row, label, input, counter, ...controls };
    row.append(heading, actions, input);
    entry.remove.addEventListener("click", () => {
      if (disabled) return;
      const index = entries.indexOf(entry);
      entries.splice(index, 1);
      if (!entries.length) entries.push(createEntry("", firstInput));
      render();
      entries[Math.min(index, entries.length - 1)].input.focus();
      onChange();
    });
    entry.copy.addEventListener("click", () => {
      if (disabled || entries.length >= maximum) return;
      const copy = createEntry(input.value);
      entries.splice(entries.indexOf(entry) + 1, 0, copy);
      render(); copy.input.focus(); onChange();
    });
    for (const [action, step] of [["up", -1], ["down", 1]]) {
      entry[action].addEventListener("click", () => {
        const index = entries.indexOf(entry), next = index + step;
        if (disabled || next < 0 || next >= entries.length) return;
        [entries[index], entries[next]] = [entries[next], entries[index]];
        render(); input.focus(); onChange();
      });
    }
    return entry;
  }

  container.addEventListener("focusin", event => {
    if (event.target.matches("textarea")) activeInput = event.target;
  });
  container.addEventListener("input", () => { updateCounts(); updateControls(); });
  inheritButton.addEventListener("click", () => {
    if (disabled) return;
    inherited = true;
    entries = [createEntry("", firstInput)]; activeInput = firstInput;
    render(); customizeButton.focus(); onChange();
  });
  customizeButton.addEventListener("click", () => {
    if (disabled || !inherited || !globalTemplates.length || globalTemplates.length > maximum) return;
    copyGlobalsToDraft();
    render(); entries[0].input.focus(); onChange();
  });
  variables.addEventListener("click", event => {
    const button = event.target.closest("[data-template-variable]");
    if (!button || disabled || inherited) return;
    const input = entries.find(entry => entry.input === activeInput)?.input || entries[0]?.input;
    if (!input) return;
    const token = `{${button.dataset.templateVariable}}`;
    const start = input.selectionStart, end = input.selectionEnd;
    const value = input.value.slice(0, start) + token + input.value.slice(end);
    if (length(value) > 2000) { state.textContent = "插入后将超过 2000 个字符"; input.focus(); return; }
    input.setRangeText(token, start, end, "end"); input.focus();
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });

  addButton.addEventListener("click", () => {
    if (disabled || (inherited ? globalTemplates.length >= maximum : entries.length >= maximum)) return;
    if (inherited) copyGlobalsToDraft();
    const entry = createEntry("", entries.length ? undefined : firstInput); entries.push(entry); render();
    entry.input.focus(); onChange();
  });

  return {
    write(values) {
      inherited = !values.length;
      entries = (values.length ? values : [""]).map((value, index) =>
        createEntry(value, index === 0 ? firstInput : undefined));
      activeInput = entries[0].input;
      render();
    },
    updateGlobals(values) { globalTemplates = [...values]; renderGlobals(); updateCounts(); updateControls(); },
    updatePresets(values) {
      if (JSON.stringify(presets) !== JSON.stringify(values)) { presets = values; renderPresets(); }
      updateControls();
    },
    values: () => inherited ? [] : entries.map(entry => entry.input.value),
    read() {
      if (inherited) return [];
      const values = entries.map(entry => entry.input.value.trim());
      if (values.length === 1 && !values[0]) return [];
      if (values.length > maximum) throw new Error(`消息模板最多 ${maximum} 条`);
      values.forEach((value, index) => {
        let message = "";
        if (!value) message = `消息模板 ${index + 1} 为空，请填写或移除`;
        else if (length(entries[index].input.value) > 2000) message = `消息模板 ${index + 1} 超过 2000 个字符`;
        else if (/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/u.test(value)) message = `消息模板 ${index + 1} 包含异常控制字符`;
        if (message) { const error = new Error(message); error.input = entries[index].input; throw error; }
      });
      return values;
    },
    setDisabled(value) { disabled = value; updateControls(); },
  };
};
