/* --- 复制到剪贴板（带回退与"已复制"反馈） --- */
import { ref, type Ref } from "vue";

export function useCopy(feedbackMs = 1500) {
  const copiedKey: Ref<string | null> = ref(null);
  let timer: ReturnType<typeof setTimeout> | null = null;

  async function copyText(text: string, key = "default"): Promise<boolean> {
    if (!text) return false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        textarea.remove();
      }
      copiedKey.value = key;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        copiedKey.value = null;
      }, feedbackMs);
      return true;
    } catch {
      return false;
    }
  }

  return { copiedKey, copyText };
}
