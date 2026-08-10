"use strict";

(() => {
  const LOCALE_KEY = "dev-mesh-observer.locale";
  const THEME_KEY = "dev-mesh-observer.theme";
  const locales = ["zh-CN", "en"];
  const themes = ["auto", "light", "dark"];

  const messages = {
    "zh-CN": {
      "app.description": "Dev Mesh 多工作区协作观测控制台",
      "brand.home": "Dev Mesh Observer 首页",
      "connection.connecting": "正在连接",
      "connection.refreshing": "正在刷新",
      "connection.collecting": "正在采集",
      "connection.online": "Observer 在线",
      "connection.failed": "连接失败",
      "connection.collectFailed": "采集失败",
      "preferences.language": "语言",
      "preferences.theme": "显示主题",
      "theme.auto": "跟随系统",
      "theme.light": "亮色",
      "theme.dark": "暗色",
      "hero.eyebrow": "本地协作观测",
      "hero.title1": "看清 Agent 之间",
      "hero.title2": "发生了什么。",
      "hero.copy": "从 immutable events 还原 workspace、run、handoff 与资源热点。控制台只写 Observer 数据库，不改变任何源工作区。",
      "toolbar.label": "观察窗口",
      "since.1h": "最近 1 小时",
      "since.24h": "最近 24 小时",
      "since.48h": "最近 48 小时",
      "since.7d": "最近 7 天",
      "since.4w": "最近 4 周",
      "action.refresh": "刷新视图",
      "action.collect": "立即采集",
      "metrics.label": "观察摘要",
      "metrics.workspaces": "工作区",
      "metrics.events": "窗口内事件",
      "metrics.openRuns": "开放 Run",
      "metrics.pendingHandoffs": "Pending Handoff",
      "metrics.issues": "Integrity Issues",
      "metrics.waiting": "等待数据",
      "metrics.issueHint": "采集与不可变性检查",
      "metrics.available": "{count} 个可访问",
      "metrics.active": "{count} 个活跃工作区",
      "metrics.runs": "{closed} 已关闭 / {total} 总计",
      "metrics.handoffs": "{accepted} 已接受 / {offered} 发起",
      "analytics.label": "协作诊断",
      "analytics.kicker": "诊断",
      "conflicts.title": "冲突汇总",
      "conflicts.signals": "冲突信号",
      "conflicts.contentions": "争用",
      "conflicts.refresh": "合并冲突",
      "conflicts.blocked": "队列阻塞",
      "conflicts.attention": "需关注",
      "conflicts.hotspots": "冲突相关路径",
      "conflicts.noHotspots": "当前窗口没有路径级冲突证据",
      "conflicts.signalCount": "{count} 次信号",
      "transactions.title": "事务",
      "transactions.observed": "观察到的事务",
      "transactions.published": "已发布",
      "transactions.conflicted": "曾冲突",
      "transactions.aborted": "已中止",
      "transactions.noData": "当前窗口没有 transaction 事件",
      "transaction.status.active": "进行中",
      "transaction.status.paused": "已暂停",
      "transaction.status.prepared": "已准备",
      "transaction.status.ready": "待发布",
      "transaction.status.refreshing": "正在刷新",
      "transaction.status.conflicted": "发生冲突",
      "transaction.status.publishing": "正在发布",
      "transaction.status.published": "已发布",
      "transaction.status.aborted": "已中止",
      "transaction.status.needs-attention": "需要关注",
      "transaction.status.observed": "已观察",
      "protocolUse.title": "协议利用",
      "protocolUse.solo": "疑似单 Agent 协议",
      "protocolUse.collaborative": "观察到协作",
      "protocolUse.lifecycleOnly": "仅生命周期",
      "protocolUse.open": "开放未判定",
      "protocolUse.noSolo": "当前窗口未发现疑似 solo-protocol run",
      "protocolUse.heuristic": "仅判定已结束 run：产生了协议事件，但未观察到并行 run、父子关系、message、handoff、contention 或 transaction。",
      "protocolUse.eventCount": "{count} 个协议事件",
      "catalog.kicker": "目录",
      "catalog.workspaces": "工作区",
      "workspace.add": "添加工作区",
      "workspace.none": "尚未登记工作区，请点击“添加工作区”。",
      "workspace.events": "{count} 条事件",
      "workspace.unknown": "未知工作区",
      "inflight.kicker": "进行中",
      "inflight.title": "正在进行",
      "inflight.openRuns": "开放 Runs",
      "inflight.handoffs": "等待 Handoff",
      "inflight.noRuns": "暂无开放 run",
      "inflight.noHandoffs": "暂无 pending handoff",
      "activity.kicker": "活动",
      "activity.workspace": "工作区",
      "activity.ownership": "所有者",
      "activity.owner": "Agent Owner",
      "activity.pressure": "压力",
      "activity.resource": "语义资源",
      "activity.none": "当前窗口暂无活动",
      "timeline.kicker": "不可变事件流",
      "timeline.title": "协作时间线",
      "filters.workspace": "工作区",
      "filters.event": "事件类型",
      "filters.owner": "Owner",
      "filters.run": "Run ID",
      "filters.allWorkspaces": "全部工作区",
      "filters.allEvents": "全部事件",
      "filters.allOwners": "全部 Owner",
      "filters.apply": "应用",
      "events.waiting": "等待事件",
      "events.none": "当前筛选条件下没有事件。",
      "events.unknown": "未知事件",
      "issues.kicker": "完整性",
      "issues.title": "采集问题",
      "issues.none": "未发现采集或不可变性问题。",
      "footer.pending": "尚未刷新",
      "footer.updated": "更新于 {time}",
      "eventDetail.kicker": "事件详情",
      "eventDetail.title": "事件详情",
      "action.close": "关闭",
      "workspaceDialog.kicker": "允许扫描目录",
      "workspaceDialog.title": "添加工作区",
      "workspaceDialog.path": "工作区或父目录的绝对路径",
      "workspaceDialog.placeholder": "/Users/you/projects/my-workspace",
      "workspaceDialog.help": "添加即允许 Observer 只读扫描该路径下的 .agent-coordination/events。它不会修改源工作区。",
      "workspaceDialog.depth": "向下扫描深度",
      "workspaceDialog.depth0": "仅当前目录",
      "workspaceDialog.depth2": "向下 2 层",
      "workspaceDialog.depth5": "向下 5 层",
      "workspaceDialog.depth10": "向下 10 层",
      "workspaceDialog.registered": "已登记扫描根",
      "workspaceDialog.noRoots": "尚无扫描根",
      "workspaceDialog.cancel": "取消",
      "workspaceDialog.submit": "添加并采集",
      "workspaceDialog.submitting": "正在添加",
      "toast.collectComplete": "采集完成：新增 {count} 条事件",
      "toast.workspaceAdded": "已添加 {count} 个工作区，新增 {events} 条事件",
      "toast.rootAdded": "扫描根已保存，但暂未发现 .agent-coordination/events",
      "time.unknown": "时间未知",
      "noscript": "此控制台需要 JavaScript 才能显示 Observer 数据。"
    },
    en: {
      "app.description": "Dev Mesh multi-workspace coordination observer console",
      "brand.home": "Dev Mesh Observer home",
      "connection.connecting": "Connecting",
      "connection.refreshing": "Refreshing",
      "connection.collecting": "Collecting",
      "connection.online": "Observer online",
      "connection.failed": "Connection failed",
      "connection.collectFailed": "Collection failed",
      "preferences.language": "Language",
      "preferences.theme": "Display theme",
      "theme.auto": "System",
      "theme.light": "Light",
      "theme.dark": "Dark",
      "hero.eyebrow": "LOCAL COORDINATION INTELLIGENCE",
      "hero.title1": "See what happens",
      "hero.title2": "between agents.",
      "hero.copy": "Reconstruct workspaces, runs, handoffs, and resource pressure from immutable events. The console writes only to the Observer database and never changes a source workspace.",
      "toolbar.label": "Observation window",
      "since.1h": "Last hour",
      "since.24h": "Last 24 hours",
      "since.48h": "Last 48 hours",
      "since.7d": "Last 7 days",
      "since.4w": "Last 4 weeks",
      "action.refresh": "Refresh view",
      "action.collect": "Collect now",
      "metrics.label": "Observation summary",
      "metrics.workspaces": "Workspaces",
      "metrics.events": "Events in window",
      "metrics.openRuns": "Open runs",
      "metrics.pendingHandoffs": "Pending handoffs",
      "metrics.issues": "Integrity issues",
      "metrics.waiting": "Waiting for data",
      "metrics.issueHint": "Collection and immutability checks",
      "metrics.available": "{count} available",
      "metrics.active": "{count} active workspaces",
      "metrics.runs": "{closed} closed / {total} total",
      "metrics.handoffs": "{accepted} accepted / {offered} offered",
      "analytics.label": "Coordination diagnostics",
      "analytics.kicker": "DIAGNOSTICS",
      "conflicts.title": "Conflict summary",
      "conflicts.signals": "Conflict signals",
      "conflicts.contentions": "Contentions",
      "conflicts.refresh": "Merge conflicts",
      "conflicts.blocked": "Queue blocks",
      "conflicts.attention": "Needs attention",
      "conflicts.hotspots": "Conflict-related paths",
      "conflicts.noHotspots": "No path-level conflict evidence in this window",
      "conflicts.signalCount": "{count} signals",
      "transactions.title": "Transactions",
      "transactions.observed": "Observed transactions",
      "transactions.published": "Published",
      "transactions.conflicted": "Ever conflicted",
      "transactions.aborted": "Aborted",
      "transactions.noData": "No transaction events in this window",
      "transaction.status.active": "Active",
      "transaction.status.paused": "Paused",
      "transaction.status.prepared": "Prepared",
      "transaction.status.ready": "Ready",
      "transaction.status.refreshing": "Refreshing",
      "transaction.status.conflicted": "Conflicted",
      "transaction.status.publishing": "Publishing",
      "transaction.status.published": "Published",
      "transaction.status.aborted": "Aborted",
      "transaction.status.needs-attention": "Needs attention",
      "transaction.status.observed": "Observed",
      "protocolUse.title": "Protocol use",
      "protocolUse.solo": "Likely solo protocol",
      "protocolUse.collaborative": "Collaboration observed",
      "protocolUse.lifecycleOnly": "Lifecycle only",
      "protocolUse.open": "Open, unclassified",
      "protocolUse.noSolo": "No likely solo-protocol runs in this window",
      "protocolUse.heuristic": "Closed runs only: protocol events occurred, but no concurrent run, parent-child relation, message, handoff, contention, or transaction was observed.",
      "protocolUse.eventCount": "{count} protocol events",
      "catalog.kicker": "CATALOG",
      "catalog.workspaces": "Workspaces",
      "workspace.add": "Add workspace",
      "workspace.none": "No workspace is registered. Choose “Add workspace” to begin.",
      "workspace.events": "{count} events",
      "workspace.unknown": "Unknown workspace",
      "inflight.kicker": "IN FLIGHT",
      "inflight.title": "In progress",
      "inflight.openRuns": "Open runs",
      "inflight.handoffs": "Pending handoffs",
      "inflight.noRuns": "No open runs",
      "inflight.noHandoffs": "No pending handoffs",
      "activity.kicker": "ACTIVITY",
      "activity.workspace": "Workspace",
      "activity.ownership": "OWNERSHIP",
      "activity.owner": "Agent owner",
      "activity.pressure": "PRESSURE",
      "activity.resource": "Semantic resource",
      "activity.none": "No activity in this window",
      "timeline.kicker": "IMMUTABLE EVENT STREAM",
      "timeline.title": "Coordination timeline",
      "filters.workspace": "Workspace",
      "filters.event": "Event type",
      "filters.owner": "Owner",
      "filters.run": "Run ID",
      "filters.allWorkspaces": "All workspaces",
      "filters.allEvents": "All events",
      "filters.allOwners": "All owners",
      "filters.apply": "Apply",
      "events.waiting": "Waiting for events",
      "events.none": "No events match these filters.",
      "events.unknown": "Unknown event",
      "issues.kicker": "INTEGRITY",
      "issues.title": "Collection issues",
      "issues.none": "No collection or immutability issues found.",
      "footer.pending": "Not refreshed yet",
      "footer.updated": "Updated {time}",
      "eventDetail.kicker": "EVENT DETAIL",
      "eventDetail.title": "Event detail",
      "action.close": "Close",
      "workspaceDialog.kicker": "ALLOWLIST A DIRECTORY",
      "workspaceDialog.title": "Add workspace",
      "workspaceDialog.path": "Absolute path to a workspace or parent directory",
      "workspaceDialog.placeholder": "/Users/you/projects/my-workspace",
      "workspaceDialog.help": "Adding this path allows Observer to read only .agent-coordination/events below it. The source workspace is never modified.",
      "workspaceDialog.depth": "Scan depth",
      "workspaceDialog.depth0": "Current directory only",
      "workspaceDialog.depth2": "2 levels deep",
      "workspaceDialog.depth5": "5 levels deep",
      "workspaceDialog.depth10": "10 levels deep",
      "workspaceDialog.registered": "Registered scan roots",
      "workspaceDialog.noRoots": "No scan roots yet",
      "workspaceDialog.cancel": "Cancel",
      "workspaceDialog.submit": "Add and collect",
      "workspaceDialog.submitting": "Adding",
      "toast.collectComplete": "Collection complete: {count} new events",
      "toast.workspaceAdded": "Added {count} workspaces and {events} new events",
      "toast.rootAdded": "Scan root saved, but no .agent-coordination/events was found yet",
      "time.unknown": "Unknown time",
      "noscript": "JavaScript is required to display Observer data."
    }
  };

  function readPreference(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (_) {
      return null;
    }
  }

  function writePreference(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (_) {
      // Preferences remain session-local when browser storage is unavailable.
    }
  }

  function browserLocale() {
    const candidates = navigator.languages || [navigator.language];
    return candidates.some((value) => String(value).toLowerCase().startsWith("zh"))
      ? "zh-CN"
      : "en";
  }

  const state = {
    locale: locales.includes(readPreference(LOCALE_KEY))
      ? readPreference(LOCALE_KEY)
      : browserLocale(),
    theme: themes.includes(readPreference(THEME_KEY))
      ? readPreference(THEME_KEY)
      : "auto",
  };

  function applyTheme() {
    if (state.theme === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.dataset.theme = state.theme;
  }

  function t(key, variables = {}) {
    const template = messages[state.locale][key] || messages.en[key] || key;
    return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, name) =>
      Object.prototype.hasOwnProperty.call(variables, name) ? String(variables[name]) : `{${name}}`
    );
  }

  function translateDocument(root = document) {
    root.querySelectorAll("[data-i18n]").forEach((element) => {
      element.textContent = t(element.dataset.i18n);
    });
    ["placeholder", "aria-label", "title"].forEach((attribute) => {
      const marker = `data-i18n-${attribute}`;
      root.querySelectorAll(`[${marker}]`).forEach((element) => {
        element.setAttribute(attribute, t(element.getAttribute(marker)));
      });
    });
    const description = document.querySelector('meta[name="description"]');
    if (description) description.content = t("app.description");
  }

  function setLocale(locale) {
    if (!locales.includes(locale)) return;
    state.locale = locale;
    document.documentElement.lang = locale;
    writePreference(LOCALE_KEY, locale);
    translateDocument();
  }

  function setTheme(theme) {
    if (!themes.includes(theme)) return;
    state.theme = theme;
    writePreference(THEME_KEY, theme);
    applyTheme();
  }

  document.documentElement.lang = state.locale;
  applyTheme();
  window.DevMeshPreferences = {
    get locale() { return state.locale; },
    get theme() { return state.theme; },
    locales,
    themes,
    setLocale,
    setTheme,
    t,
    translateDocument,
  };
})();
