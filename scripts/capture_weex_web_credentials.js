(() => {
  const id = "weex-auth-capture";
  document.getElementById(id)?.remove();

  const auth = (window.__WEEX_AUTH__ = {});
  const panel = document.createElement("div");
  panel.id = id;
  panel.style.cssText =
    "position:fixed;top:20px;right:20px;z-index:2147483647;width:400px;padding:16px;background:#fff;color:#111;border:1px solid #bbb;border-radius:6px;box-shadow:0 12px 36px #0006;font:14px Arial";
  panel.innerHTML = `
    <b style="font-size:16px">WEEX 凭据捕获</b>
    <p data-status style="margin:12px 0">请点击页面中的“当前委托”</p>
    <textarea readonly style="display:none;box-sizing:border-box;width:100%;height:130px;padding:8px;font:12px monospace"></textarea>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">
      <button data-copy style="display:none">复制配置</button>
      <button data-close>关闭</button>
    </div>`;
  document.body.append(panel);

  const status = panel.querySelector("[data-status]");
  const textarea = panel.querySelector("textarea");
  const copyButton = panel.querySelector("[data-copy]");
  const closeButton = panel.querySelector("[data-close]");
  const originalHeader = XMLHttpRequest.prototype.setRequestHeader;
  const originalFetch = window.fetch;
  let complete = false;

  const stop = () => {
    XMLHttpRequest.prototype.setRequestHeader = originalHeader;
    window.fetch = originalFetch;
  };

  const capture = (name, value) => {
    const key = String(name).toLowerCase();
    if (key === "u-token" && value) auth.WEEX_WEB_CC_TOKEN = String(value);
    if (key === "terminalcode" && value) auth.WEEX_WEB_TERMINAL_CODE = String(value);
    if (complete || !auth.WEEX_WEB_CC_TOKEN || !auth.WEEX_WEB_TERMINAL_CODE) return;

    complete = true;
    stop();
    textarea.value =
      `WEEX_WEB_CC_TOKEN=${auth.WEEX_WEB_CC_TOKEN}\n` +
      `WEEX_WEB_TERMINAL_CODE=${auth.WEEX_WEB_TERMINAL_CODE}`;
    textarea.style.display = "block";
    copyButton.style.display = "inline-block";
    status.textContent = "捕获完成，请复制配置到项目 .env";
    console.info("[WEEX] 捕获完成");
  };

  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    capture(name, value);
    return originalHeader.call(this, name, value);
  };

  window.fetch = function (input, init = {}) {
    try {
      const headers = new Headers(input instanceof Request ? input.headers : undefined);
      new Headers(init.headers || {}).forEach((value, name) => headers.set(name, value));
      headers.forEach((value, name) => capture(name, value));
    } catch {}
    return originalFetch.apply(this, arguments);
  };

  copyButton.onclick = async () => {
    textarea.select();
    try {
      await navigator.clipboard.writeText(textarea.value);
      copyButton.textContent = "已复制";
    } catch {
      copyButton.textContent = document.execCommand("copy") ? "已复制" : "请手动复制";
    }
  };

  closeButton.onclick = () => {
    stop();
    panel.remove();
  };

  console.info("[WEEX] 监听已启动，请点击“当前委托”");
})();
