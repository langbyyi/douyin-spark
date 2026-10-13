/* Password inputs stay in this dialog and are cleared on close or success. */
window.createPanelSettings = ({ api, onPasswordChanged }) => {
  const $ = selector => document.querySelector(selector);
  const dialog = $("#password-dialog"), form = $("#password-form");
  let busy = false;
  $("#btn-password").addEventListener("click", () => {
    form.reset(); $("#password-msg").textContent = "";
    dialog.showModal(); $("#password-current").focus();
  });
  $("#password-close").addEventListener("click", () => { if (!busy) dialog.close(); });
  dialog.addEventListener("cancel", event => { if (busy) event.preventDefault(); });
  dialog.addEventListener("close", () => form.reset());
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (busy) return;
    const body = {
      current_password: $("#password-current").value,
      new_password: $("#password-new").value,
      confirm_password: $("#password-confirm").value,
    };
    if (body.new_password !== body.confirm_password) {
      $("#password-msg").textContent = "两次输入的新密码不一致"; return;
    }
    busy = true;
    form.querySelectorAll("input, button").forEach(input => { input.disabled = true; });
    $("#password-close").disabled = true;
    $("#password-msg").textContent = "正在修改密码…";
    try {
      await api("/auth/password", { method: "POST", body: JSON.stringify(body) });
      form.reset();
      try { sessionStorage.setItem("spark-password-changed", "1"); } catch { /* Storage is optional. */ }
      onPasswordChanged();
    } catch (error) { $("#password-msg").textContent = error.message; }
    finally {
      busy = false;
      form.querySelectorAll("input, button").forEach(input => { input.disabled = false; });
      $("#password-close").disabled = false;
    }
  });
};
