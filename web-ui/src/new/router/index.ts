import { createRouter, createWebHashHistory, type RouteRecordRaw } from "vue-router";

/* --- 页面分区（懒加载） --- */
const DashboardView = () => import("../views/DashboardView.vue");
const AccountsView = () => import("../views/accounts/AccountsView.vue");
const TasksView = () => import("../views/tasks/TasksView.vue");
const MailView = () => import("../views/register/MailView.vue");
const ProxyView = () => import("../views/register/ProxyView.vue");
const FingerprintView = () => import("../views/register/FingerprintView.vue");
const CredentialExportView = () => import("../views/accounts/CredentialExportView.vue");
const RebindView = () => import("../views/accounts/RebindView.vue");
const ProtocolLoginView = () => import("../views/accounts/ProtocolLoginView.vue");
const TrialView = () => import("../views/integrations/TrialView.vue");
const CliproxyView = () => import("../views/integrations/CliproxyView.vue");
const IntegrationView = () => import("../views/integrations/IntegrationView.vue");
const LogsView = () => import("../views/system/LogsView.vue");
const OperationsView = () => import("../views/system/OperationsView.vue");

const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/dashboard" },
  { path: "/dashboard", name: "dashboard", component: DashboardView, meta: { title: "总览", subtitle: "指标、运行状态与快捷入口", section: "总览" } },

  /* --- 注册中心 --- */
  { path: "/register/tasks", component: TasksView, meta: { title: "任务调度", subtitle: "创建、调度并观察账号注册任务", section: "注册中心" } },
  { path: "/register/mail", component: MailView, meta: { title: "邮箱池", subtitle: "维护 Outlook、iCloud、域名邮箱、Maddy 与 Fastmail", section: "注册中心" } },
  { path: "/register/proxy", component: ProxyView, meta: { title: "代理设置", subtitle: "配置注册出站、资源代理与清障服务", section: "注册中心" } },
  { path: "/register/fingerprint", component: FingerprintView, meta: { title: "指纹环境", subtitle: "检查可用运行环境并管理指纹参数", section: "注册中心" } },

  /* --- 账号管理 --- */
  { path: "/accounts", component: AccountsView, meta: { title: "账号列表", subtitle: "搜索、测活、分组并导出已注册账号", section: "账号管理" } },
  { path: "/accounts/export", component: CredentialExportView, meta: { title: "凭据导出", subtitle: "按邮箱筛选并导出账号、密码和 2FA", section: "账号管理" } },
  { path: "/accounts/rebind", component: RebindView, meta: { title: "邮箱换绑", subtitle: "将账号换绑到邮箱池中的未占用地址", section: "账号管理" } },
  { path: "/accounts/reauth", component: ProtocolLoginView, meta: { title: "协议登录", subtitle: "按邮箱批量重新登录并查看账号状态", section: "账号管理" } },

  /* --- 资格与集成 --- */
  { path: "/integrations/trial", component: TrialView, meta: { title: "Plus 免费试用", subtitle: "试用资格检查与试用代理池", section: "资格与集成" } },
  { path: "/integrations/cliproxy", component: CliproxyView, meta: { title: "中转管理", subtitle: "查看 cliproxy 中转账号额度并下载凭据", section: "资格与集成" } },
  { path: "/integrations/chatgpt2api", component: IntegrationView, meta: { title: "chatgpt2api", subtitle: "管理账号自动上传与连接状态", section: "资格与集成" } },

  /* --- 系统 --- */
  { path: "/system/logs", component: LogsView, meta: { title: "实时日志", subtitle: "查看任务执行过程与异常信息", section: "系统" } },
  { path: "/system/operations", component: OperationsView, meta: { title: "运维中心", subtitle: "统计、自动维护、备份恢复、并发与面板安全", section: "系统" } },
  /* 设置页已并入运维中心，保留重定向避免旧链接 404 */
  { path: "/system/settings", redirect: "/system/operations" },

  /* --- 旧 hash 路由重定向（保证收藏夹链接不死） --- */
  { path: "/task", redirect: "/register/tasks" },
  { path: "/fingerprint", redirect: "/register/fingerprint" },
  { path: "/mail", redirect: "/register/mail" },
  { path: "/proxy", redirect: "/register/proxy" },
  { path: "/plus-eligibility", redirect: "/integrations/trial" },
  { path: "/chatgpt2api", redirect: "/integrations/chatgpt2api" },
  { path: "/cliproxy", redirect: "/integrations/cliproxy" },
  { path: "/logs", redirect: "/system/logs" },
  { path: "/rebind", redirect: "/accounts/rebind" },
  { path: "/credential-export", redirect: "/accounts/export" },
  { path: "/protocol-login", redirect: "/accounts/reauth" },
  { path: "/operations", redirect: "/system/operations" },

  { path: "/:pathMatch(.*)*", redirect: "/dashboard" },
];

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
});

export interface NavItem {
  to: string;
  label: string;
  desc: string;
  icon: string;
}

export interface NavSection {
  label: string;
  items: NavItem[];
}

/* --- 侧栏导航定义（icon 为 AppSidebar 内置图标键） --- */
export const navSections: NavSection[] = [
  { label: "总览", items: [{ to: "/dashboard", label: "总览", desc: "指标与快捷入口", icon: "gauge" }] },
  {
    label: "注册中心",
    items: [
      { to: "/register/tasks", label: "任务调度", desc: "创建与调度注册任务", icon: "terminal" },
      { to: "/register/mail", label: "邮箱池", desc: "Outlook / iCloud / Maddy", icon: "mail" },
      { to: "/register/proxy", label: "代理设置", desc: "出站与清障代理", icon: "network" },
      { to: "/register/fingerprint", label: "指纹环境", desc: "浏览器指纹参数", icon: "activity" },
    ],
  },
  {
    label: "账号管理",
    items: [
      { to: "/accounts", label: "账号列表", desc: "测活、分组与导出", icon: "contact" },
      { to: "/accounts/export", label: "凭据导出", desc: "账号密码与 2FA", icon: "file-down" },
      { to: "/accounts/rebind", label: "邮箱换绑", desc: "换绑到空闲邮箱", icon: "swap" },
      { to: "/accounts/reauth", label: "协议登录", desc: "批量重新登录", icon: "login" },
    ],
  },
  {
    label: "资格与集成",
    items: [
      { to: "/integrations/trial", label: "Plus 免费试用", desc: "资格检查与代理池", icon: "shield" },
      { to: "/integrations/cliproxy", label: "中转管理", desc: "cliproxy 额度与凭据", icon: "relay" },
      { to: "/integrations/chatgpt2api", label: "chatgpt2api", desc: "自动上传与连接", icon: "cable" },
    ],
  },
  {
    label: "系统",
    items: [
      { to: "/system/logs", label: "实时日志", desc: "任务执行日志流", icon: "list" },
      { to: "/system/operations", label: "运维中心", desc: "维护、备份、并发与安全", icon: "cloud-cog" },
    ],
  },
];
