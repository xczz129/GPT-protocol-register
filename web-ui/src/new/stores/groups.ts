/* --- 账号分组 store --- */
import { defineStore } from "pinia";
import {
  createGroup, deleteGroup, fetchGroups, moveAccountsToGroup, renameGroup,
  type AccountGroup,
} from "../api/groups";

export const useGroupsStore = defineStore("groups", {
  state: () => ({
    items: [] as AccountGroup[],
    loaded: false,
  }),
  getters: {
    defaultGroup: (s) => s.items.find((g) => g.is_default) || s.items[0],
    nameOf: (s) => (id: string) => s.items.find((g) => g.id === id)?.name || "默认分组",
  },
  actions: {
    async load(force = false) {
      if (this.loaded && !force) return;
      const data = await fetchGroups();
      this.items = data.items || [];
      this.loaded = true;
    },
    async create(name: string) {
      const { group } = await createGroup(name);
      this.items.push(group);
      return group;
    },
    async rename(id: string, name: string) {
      const { group } = await renameGroup(id, name);
      const index = this.items.findIndex((g) => g.id === id);
      if (index >= 0) this.items[index] = group;
      return group;
    },
    async remove(id: string) {
      const result = await deleteGroup(id);
      this.items = this.items.filter((g) => g.id !== id);
      return result;
    },
    async move(groupId: string, keys: string[], deleteAll = false) {
      return moveAccountsToGroup(groupId, keys, deleteAll);
    },
  },
});
