/* --- 邮箱池 store：五种渠道表单（Outlook/iCloud/SMSBower/Maddy/Fastmail）+ 已导入邮箱列表 ---
   行为对齐旧版 fillForms/save*：配置从 /api/config 的 mail.providers 填充，
   保存后刷新列表；密钥字段留空即沿用服务器已保存值。 */
import { reactive, ref } from "vue";
import { defineStore } from "pinia";
import { fetchConfig } from "../api/config";
import {
  deleteMailboxes, fetchFastmailAccounts, fetchMailboxes, fetchMaddyDomains,
  isMaskedSecret, parseDomainList, saveMailboxes, updateMailboxState,
  type FastmailPoolRow, type Mailbox, type MailSaveResult,
} from "../api/mail";

export const useMailStore = defineStore("mail", () => {
  /* --- 渠道表单 --- */
  const forms = reactive({
    outlookText: "",
    outlookMode: "imap",
    icloudText: "",
    icloudApiBase: "https://icloud.xbovo.online",
    icloudEnable: true,
    smsbowerEnable: false,
    smsbowerRegisterChild: true,
    smsbowerApiBase: "https://smsbower.page",
    smsbowerApiKey: "",
    smsbowerApiKeyConfigured: false,
    smsbowerService: "dr",
    smsbowerDomains: ["gmail.com", "icloud.com"] as string[],
    smsbowerMaxPrice: "",
    maddyEnable: false,
    maddyApiBase: "https://120.53.232.195/api",
    maddyApiToken: "",
    maddyApiKeyConfigured: false,
    maddyDefaultDomain: "xb1219.com",
    maddyDomainsText: "",
    maddyLocalLength: 12,
    maddyCapacity: 500,
    maddyMailboxPassword: "",
    fastmailEnable: false,
    fastmailApiBase: "http://127.0.0.1:8686",
    fastmailAdminKey: "",
    fastmailAccountsText: "",
    fastmailCapacity: 500,
    fastmailPoolRows: [] as FastmailPoolRow[],
  });

  /* --- 已导入邮箱列表 --- */
  const mailboxes = ref<Mailbox[]>([]);
  const loading = ref(false);
  const loadError = ref("");

  /** 从 /api/config 的 mail.providers 填充五个渠道表单（对齐旧版 fillForms） */
  async function fillForms() {
    const { config } = await fetchConfig();
    const providers = config.mail?.providers || [];
    const provider = (type: string) =>
      (providers.find((item) => item.type === type) || {}) as Record<string, unknown>;

    const outlook = provider("outlook_token");
    const icloud = provider("icloud_api");
    const smsbower = provider("smsbower_mail");
    const maddy = provider("maddy_webui");
    const fastmail = provider("fastmail_api");

    forms.outlookText = "";
    forms.outlookMode = String(outlook.mode || "imap");
    forms.icloudText = "";
    forms.icloudApiBase = String(icloud.api_base || "https://icloud.xbovo.online");
    forms.icloudEnable = icloud.enable !== false;

    forms.smsbowerEnable = smsbower.enable === true;
    forms.smsbowerRegisterChild = smsbower.register_child !== false;
    forms.smsbowerApiBase = String(smsbower.api_base || "https://smsbower.page");
    const smsbowerMasked = String(smsbower.api_key_masked || "");
    const smsbowerReturned = String(smsbower.api_key || "");
    forms.smsbowerApiKey = "";
    forms.smsbowerApiKeyConfigured = Boolean(
      smsbower.has_api_key || smsbowerMasked || smsbowerReturned,
    );
    forms.smsbowerService = String(smsbower.service || "dr");
    const domainSource = Array.isArray(smsbower.domains)
      ? smsbower.domains as unknown[]
      : smsbower.domain ? [smsbower.domain] : [];
    const configuredDomains = [...new Set(domainSource.map((domain) => String(domain).trim().toLowerCase()))]
      .filter((domain) => ["gmail.com", "icloud.com"].includes(domain));
    forms.smsbowerDomains = configuredDomains.length ? configuredDomains : ["gmail.com", "icloud.com"];
    forms.smsbowerMaxPrice = smsbower.max_price == null ? "" : String(smsbower.max_price);

    forms.maddyEnable = maddy.enable === true;
    forms.maddyApiBase = String(maddy.api_base || "https://120.53.232.195/api");
    const masked = String(maddy.api_key_masked || maddy.api_token_masked || "");
    const returned = String(maddy.api_key || maddy.api_token || "");
    forms.maddyApiToken = isMaskedSecret(returned) ? "" : returned;
    forms.maddyApiKeyConfigured = Boolean(maddy.has_api_key || maddy.has_api_token || masked || (isMaskedSecret(returned) && returned));
    forms.maddyDefaultDomain = String(maddy.default_domain || maddy.domain || "xb1219.com");
    forms.maddyDomainsText = Array.isArray(maddy.domains) ? (maddy.domains as string[]).join("\n") : "";
    forms.maddyLocalLength = Number(maddy.local_length || 12);
    forms.maddyCapacity = Number(maddy.capacity || 500);
    forms.maddyMailboxPassword = "";

    forms.fastmailEnable = fastmail.enable === true;
    forms.fastmailApiBase = String(fastmail.api_base || "http://127.0.0.1:8686");
    forms.fastmailAdminKey = "";
    forms.fastmailAccountsText = String(fastmail.accounts || "");
    forms.fastmailCapacity = Number(fastmail.capacity || 500);
    forms.fastmailPoolRows = [];
  }

  async function load() {
    loading.value = true;
    loadError.value = "";
    try {
      mailboxes.value = await fetchMailboxes();
    } catch (error) {
      loadError.value = error instanceof Error ? error.message : "加载失败";
    } finally {
      loading.value = false;
    }
  }

  /** Maddy 密钥 payload：仅当输入了新 Key（非脱敏值）才提交 */
  function maddyKeyPayload(): Record<string, string> {
    const value = forms.maddyApiToken.trim();
    return value && !isMaskedSecret(value) ? { api_key: value } : {};
  }

  /** SMSBower 密钥 payload：脱敏值和空值都不得写回配置。 */
  function smsbowerKeyPayload(): Record<string, string> {
    const value = forms.smsbowerApiKey.trim();
    return value && !isMaskedSecret(value) ? { api_key: value } : {};
  }

  async function saveOutlook(): Promise<MailSaveResult> {
    const result = await saveMailboxes({
      provider_type: "outlook_token", mailboxes: forms.outlookText, mode: forms.outlookMode, append: true,
    });
    if (result.ok !== false) {
      forms.outlookText = "";
      await load();
    }
    return result;
  }

  async function saveIcloud(): Promise<MailSaveResult> {
    const result = await saveMailboxes({
      provider_type: "icloud_api", mailboxes: forms.icloudText,
      api_base: forms.icloudApiBase, enable: forms.icloudEnable, append: true,
    });
    if (result.ok !== false) {
      forms.icloudText = "";
      await load();
    }
    return result;
  }

  async function saveSmsbower(): Promise<MailSaveResult> {
    if (!forms.smsbowerDomains.length) throw new Error("请至少选择一个 SMSBower 邮箱域名");
    const service = forms.smsbowerService.trim();
    if (!service) throw new Error("请输入 SMSBower 服务代码");
    const maxPrice = String(forms.smsbowerMaxPrice || "").trim();
    if (maxPrice && (!/^\d+(?:\.\d+)?$/.test(maxPrice) || Number(maxPrice) <= 0)) {
      throw new Error("SMSBower 最高价必须是大于 0 的金额，留空表示不限价");
    }
    const keyPayload = smsbowerKeyPayload();
    const result = await saveMailboxes({
      provider_type: "smsbower_mail", mailboxes: "",
      ...(forms.smsbowerApiBase.trim() ? { api_base: forms.smsbowerApiBase.trim() } : {}),
      ...keyPayload,
      enable: forms.smsbowerEnable,
      register_child: forms.smsbowerRegisterChild,
      service,
      domains: [...forms.smsbowerDomains],
      max_price: maxPrice,
      append: true,
    });
    if (result.ok !== false) {
      if (Object.keys(keyPayload).length) forms.smsbowerApiKeyConfigured = true;
      forms.smsbowerApiKey = "";
      await load();
    }
    return result;
  }

  async function loadMaddyDomains() {
    const result = await fetchMaddyDomains(forms.maddyApiBase, maddyKeyPayload().api_key);
    if (result.ok !== false) {
      const items = result.items || [];
      forms.maddyDomainsText = items.join("\n");
      forms.maddyDefaultDomain = result.default_domain || items[0] || forms.maddyDefaultDomain;
    }
    return result;
  }

  async function saveMaddy(): Promise<MailSaveResult> {
    const keyPayload = maddyKeyPayload();
    const result = await saveMailboxes({
      provider_type: "maddy_webui", mailboxes: "",
      api_base: forms.maddyApiBase, ...keyPayload,
      enable: forms.maddyEnable,
      default_domain: forms.maddyDefaultDomain,
      domains: parseDomainList(forms.maddyDomainsText),
      local_length: forms.maddyLocalLength,
      capacity: forms.maddyCapacity,
      mailbox_password: forms.maddyMailboxPassword,
      append: true,
    });
    if (result.ok !== false) {
      if (Object.keys(keyPayload).length) forms.maddyApiKeyConfigured = true;
      forms.maddyApiToken = "";
      forms.maddyMailboxPassword = "";
      await load();
    }
    return result;
  }

  async function loadFastmailPool() {
    const result = await fetchFastmailAccounts(forms.fastmailApiBase, forms.fastmailAdminKey || undefined);
    if (result.ok !== false) {
      forms.fastmailPoolRows = result.items || [];
    }
    return result;
  }

  async function saveFastmail(): Promise<MailSaveResult> {
    const result = await saveMailboxes({
      provider_type: "fastmail_api", mailboxes: "",
      api_base: forms.fastmailApiBase,
      admin_key: forms.fastmailAdminKey,
      accounts: forms.fastmailAccountsText,
      capacity: forms.fastmailCapacity,
      enable: forms.fastmailEnable,
      append: true,
    });
    if (result.ok !== false) {
      forms.fastmailAdminKey = "";
      await load();
    }
    return result;
  }

  /* --- 列表操作：删除 / 释放状态（本地同步，避免整表重载） --- */
  async function remove(items: Mailbox[]): Promise<MailSaveResult> {
    const result = await deleteMailboxes(items.map(({ email, provider_type }) => ({ email, provider_type })));
    if (result.ok !== false) {
      const removed = new Set(items.map((item) => `${item.provider_type}:${item.email.toLowerCase()}`));
      mailboxes.value = mailboxes.value.filter(
        (item) => !removed.has(`${item.provider_type}:${item.email.toLowerCase()}`),
      );
    }
    return result;
  }

  async function setState(emails: string[], action: string, states: string[] = []): Promise<MailSaveResult> {
    const result = await updateMailboxState(emails, action, states);
    if (result.ok !== false) {
      const selected = new Set(emails.map((email) => email.toLowerCase()));
      mailboxes.value = mailboxes.value.map((item) => {
        const current = String(item.state || "available").toLowerCase();
        const matchEmail = !selected.size || selected.has(item.email.toLowerCase());
        const matchState = !states.length || states.includes(current);
        if (!matchEmail || !matchState) return item;
        return { ...item, state: action === "release" ? "available" : action, reason: action === "release" ? "" : item.reason };
      });
    }
    return result;
  }

  return {
    forms, mailboxes, loading, loadError,
    fillForms, load,
    saveOutlook, saveIcloud, saveSmsbower, loadMaddyDomains, saveMaddy, loadFastmailPool, saveFastmail,
    remove, setState,
  };
});
