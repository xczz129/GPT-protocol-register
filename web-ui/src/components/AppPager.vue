<script setup>
defineProps({ page: Number, pages: Number, total: Number });
defineEmits(["update:page"]);
</script>

<template>
  <div class="vue-pager">
    <button class="btn secondary" :disabled="page <= 1" @click="$emit('update:page', page - 1)">上一页</button>
    <span>第 {{ total ? page : 0 }} / {{ pages }} 页 · 共 {{ total }} 条</span>
    <button class="btn secondary" :disabled="page >= pages" @click="$emit('update:page', page + 1)">下一页</button>
  </div>
</template>
