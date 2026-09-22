import {t} from "/i18n.js";

// The picker owns navigation, request cancellation, selection and registration feedback.
export function createRootPicker({request, onSaved, notify}) {
  const ids = ["root-dialog", "root-form", "root-path", "root-list", "dialog-error", "close-dialog", "cancel-root",
    "directory-list", "directory-breadcrumbs", "directory-search", "directory-hidden", "directory-home",
    "directory-up", "directory-go", "directory-status", "selected-directory", "save-root"];
  const ui = Object.fromEntries(ids.map(id => [id, document.getElementById(id)]));
  let roots = [];
  let current = null;
  let loading = false;
  let saving = false;
  let generation = 0;
  let controller = null;
  let searchTimer = null;
  const make = (tag, cls, text) => {
    const node = document.createElement(tag);
    node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const button = (label, action, cls = "button secondary") => {
    const node = make("button", cls, label);
    node.type = "button";
    node.disabled = saving;
    node.addEventListener("click", action);
    return node;
  };
  function error(message = "") {
    ui["dialog-error"].textContent = message;
    ui["dialog-error"].hidden = !message;
  }
  function render() {
    const registered = roots.includes(current?.path);
    ui["save-root"].disabled = saving || loading || !current || registered;
    ui["save-root"].textContent = t(saving ? "picker.saving" : registered ? "picker.followed" : "picker.select");
    ui["directory-up"].disabled = saving || loading || !current?.parent;
    ui["directory-home"].disabled = saving;
    ui["directory-go"].disabled = saving;
    ui["directory-search"].disabled = saving;
    ui["directory-hidden"].disabled = saving;
    ui["root-path"].disabled = saving;
    ui["close-dialog"].disabled = saving;
    ui["cancel-root"].disabled = saving;
    ui["selected-directory"].textContent = current?.path || t("picker.choose");
    ui["selected-directory"].title = current?.path || "";
    ui["directory-list"].setAttribute("aria-busy", String(loading));
    ui["directory-status"].textContent = loading ? t("picker.loading")
      : current?.truncated ? t("picker.limited", {count: current.limit})
      : t("picker.count", {count: current?.directories.length ?? 0});
    ui["root-list"].replaceChildren(...roots.map(path => {
      const node = button(path, () => navigate(path), "root-shortcut");
      node.title = path;
      return node;
    }));
    if (!roots.length) ui["root-list"].append(make("span", "muted", t("picker.noRoots")));
    ui["directory-breadcrumbs"].replaceChildren(...(current?.breadcrumbs ?? []).map(part =>
      button(part.name, () => navigate(part.path), "breadcrumb")));
    ui["directory-list"].replaceChildren(...(loading ? [] : current?.directories ?? []).map(entry => {
      const node = button("", () => navigate(entry.path), "directory-row");
      node.append(make("span", "directory-icon", "▱"), make("span", "directory-name", entry.name), make("span", "directory-chevron", "›"));
      node.setAttribute("aria-label", t("picker.open", {name: entry.name}));
      return node;
    }));
    if (!loading && current && !current.directories.length) {
      ui["directory-list"].append(make("p", "picker-empty", t(ui["directory-search"].value ? "picker.noMatch" : "picker.noChildren")));
    }
  }
  async function browse(path) {
    const token = ++generation;
    controller?.abort();
    controller = new AbortController();
    loading = true;
    error();
    render();
    const query = new URLSearchParams({query: ui["directory-search"].value, hidden: ui["directory-hidden"].checked ? "1" : "0"});
    if (path) query.set("path", path);
    try {
      const value = await request(`/api/directories?${query}`, {signal: controller.signal});
      if (token !== generation || !ui["root-dialog"].open) return;
      current = value;
      ui["root-path"].value = value.path;
    } catch (failure) {
      if (token === generation && failure.name !== "AbortError") error(`${t("picker.failed")} ${failure.message}`);
    } finally {
      if (token === generation) {
        loading = false;
        render();
      }
    }
  }
  function navigate(path) {
    if (saving) return;
    clearTimeout(searchTimer);
    ui["directory-search"].value = "";
    return browse(path).then(() => {
      if (ui["root-dialog"].open && !saving) ui["directory-search"].focus({preventScroll: true});
    });
  }
  async function open() {
    if (ui["root-dialog"].open) return;
    ui["root-dialog"].showModal();
    saving = false;
    current = null;
    error();
    const token = ++generation;
    loading = true;
    render();
    try {
      const value = await request("/api/roots");
      if (token !== generation || !ui["root-dialog"].open) return;
      roots = value.roots;
      await navigate(roots[0]);
    } catch (failure) {
      if (token === generation) { loading = false; error(failure.message); render(); }
    }
  }
  ui["close-dialog"].addEventListener("click", () => ui["root-dialog"].close());
  ui["cancel-root"].addEventListener("click", () => ui["root-dialog"].close());
  ui["root-dialog"].addEventListener("close", () => {
    ++generation;
    controller?.abort();
    clearTimeout(searchTimer);
  });
  ui["root-dialog"].addEventListener("cancel", event => {if (saving) event.preventDefault();});
  ui["directory-home"].addEventListener("click", () => navigate(current?.home));
  ui["directory-up"].addEventListener("click", () => navigate(current?.parent));
  ui["directory-go"].addEventListener("click", () => navigate(ui["root-path"].value.trim()));
  ui["root-path"].addEventListener("keydown", event => {
    if (event.key === "Enter") {event.preventDefault(); navigate(ui["root-path"].value.trim());}
  });
  ui["directory-search"].addEventListener("keydown", event => {if (event.key === "Enter") event.preventDefault();});
  ui["directory-search"].addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => browse(current?.path), 180);
  });
  ui["directory-hidden"].addEventListener("change", () => browse(current?.path));
  ui["root-form"].addEventListener("submit", async event => {
    event.preventDefault();
    if (saving || loading || !current || roots.includes(current.path)) return;
    const selected = current.path;
    saving = true;
    clearTimeout(searchTimer);
    error();
    render();
    try {
      const value = await request("/api/roots", {method: "POST", body: JSON.stringify({path: selected})});
      roots = value.roots;
      ui["root-dialog"].close();
      notify(t(value.collection?.refreshed === false ? "picker.savedPending" : "picker.saved", {path: selected}), value.collection?.refreshed === false);
      await onSaved();
    } catch (failure) {error(failure.message);}
    finally {saving = false; render();}
  });
  return {open, render};
}
