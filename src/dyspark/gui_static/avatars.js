/* Display metadata has no role in recipient matching or send eligibility. */
window.safeAvatarUrl = raw => {
  if (typeof raw !== "string" || raw.length > 4096 || !/^https:\/\//i.test(raw)
      || /^https:\/\/[^/?#]*@/i.test(raw) || /[\s\\\p{Cc}\p{Cf}]/u.test(raw)) return "";
  try {
    const url = new URL(raw);
    return url.protocol === "https:" && url.hostname && url.port !== "0"
      && !url.username && !url.password ? url.href : "";
  } catch (_) { return ""; }
};

window.renderAvatar = (element, label, rawUrl, fallback = "抖") => {
  element.replaceChildren();
  element.setAttribute("aria-hidden", "true");
  const placeholder = document.createElement("span");
  placeholder.textContent = Array.from(String(label || ""))[0] || fallback;
  element.append(placeholder);
  const url = window.safeAvatarUrl(rawUrl);
  if (!url) return element;
  const image = document.createElement("img");
  image.alt = "";
  image.referrerPolicy = "no-referrer";
  image.loading = "lazy";
  image.addEventListener("load", () => { placeholder.hidden = true; });
  image.addEventListener("error", () => { image.remove(); placeholder.hidden = false; });
  image.src = url;
  element.append(image);
  return element;
};

window.conversationIdentity = (name, metadata = {}, streak, updatedAt = "") => {
  const identity = document.createElement("span");
  identity.className = "conversation-identity";
  const avatar = document.createElement("span");
  avatar.className = "conversation-avatar";
  window.renderAvatar(avatar, name, metadata.avatar_url, "聊");
  const copy = document.createElement("span");
  copy.className = "conversation-copy";
  const title = document.createElement("span");
  title.className = "conversation-name";
  title.textContent = String(name || "");
  const details = document.createElement("span");
  details.className = "conversation-details";
  const kind = metadata.kind;
  const badge = document.createElement("span");
  badge.className = "conversation-kind" + (kind === "group" ? " group" : "");
  badge.textContent = kind === "group" ? "群聊" : kind === "private" ? "个人" : "待识别";
  details.append(badge);
  if (kind === "group" && Number.isInteger(metadata.member_count) && metadata.member_count > 0) {
    const count = document.createElement("span");
    count.className = "conv-meta";
    count.textContent = `${metadata.member_count} 人`;
    details.append(count);
  }
  if (streak != null) {
    const days = document.createElement("span");
    days.className = "conv-meta";
    days.textContent = `火花 ${streak} 天`;
    if (updatedAt) days.title = `火花数据更新于 ${updatedAt}`;
    details.append(days);
  }
  copy.append(title, details);
  identity.append(avatar, copy);
  return identity;
};
