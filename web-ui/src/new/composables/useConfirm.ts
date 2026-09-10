/* --- Promise 化确认框 --- */
import { useUiStore } from "../stores/ui";
import type { ConfirmOptions } from "../stores/ui";

export function useConfirm() {
  const ui = useUiStore();
  return (message: string, options?: ConfirmOptions) => ui.askConfirm(message, options);
}
