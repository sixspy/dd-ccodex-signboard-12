window.SB_VIEWS = window.SB_VIEWS || {};
window.SB_VIEWS["history-view"] = {
  template: `
  <div class="panel">
    <h2>变更历史 <span class="badge">signboard-12</span></h2>
    <p class="lead">发布、撤屏、覆盖、台账与远程推屏治理动作同源可追溯（演练短路 / 实推 / 互斥争用）。</p>
    <div class="row-form">
      <button type="button" :class="filter==='' ? 'primary' : 'ghost'" @click="setFilter('')">全部</button>
      <button type="button" :class="filter==='remote_push' ? 'primary' : 'ghost'" @click="setFilter('remote_push')">远程推屏相关</button>
      <button type="button" :class="filter==='remote_push_contend' ? 'primary' : 'ghost'" @click="setFilter('remote_push_contend')">仅互斥争用</button>
    </div>
    <table>
      <thead><tr><th>ID</th><th>实体</th><th>动作</th><th>详情</th><th>操作者</th></tr></thead>
      <tbody>
        <tr v-for="h in items" :key="h.id">
          <td>{{ h.id }}</td>
          <td>{{ h.entity_kind }}#{{ h.entity_id }}</td>
          <td>
            <span class="badge" :class="badgeClass(h.action)">{{ actionLabel(h.action) }}</span>
          </td>
          <td>{{ h.detail }}</td><td>{{ h.actor }}</td>
        </tr>
      </tbody>
    </table>
  </div>`,
  data() { return { items: [], filter: "" }; },
  async mounted() { await this.load(); },
  methods: {
    async load() {
      const qs = this.filter ? ("?action=" + encodeURIComponent(this.filter) + "&limit=200") : "";
      this.items = (await SB.api("/api/history" + qs)).items;
    },
    async setFilter(f) { this.filter = f; await this.load(); },
    actionLabel(a) {
      if (a === "remote_push_official") return "远程推屏·实推落正式";
      if (a === "remote_push_drill_blocked") return "远程推屏·演练短路";
      if (a === "remote_push_contend") return "远程推屏·互斥争用";
      return a;
    },
    badgeClass(a) {
      if (a === "remote_push_official") return "";
      if (a === "remote_push_contend") return "bad";
      if (a === "remote_push_drill_blocked") return "warn";
      return "";
    }
  }
};
