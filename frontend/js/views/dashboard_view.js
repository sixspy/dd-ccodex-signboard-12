window.SB_VIEWS = window.SB_VIEWS || {};
window.SB_VIEWS["dashboard-view"] = {
  template: `
  <div class="panel">
    <h2>总览</h2>
    <p class="lead">{{ meta.skeleton || "加载中…" }}</p>
    <div class="stats">
      <div class="stat" v-for="s in cards" :key="s.k"><b>{{ s.v }}</b><span>{{ s.l }}</span></div>
    </div>
  </div>`,
  data() { return { meta: {}, cards: [] }; },
  async mounted() {
    const d = await SB.api("/api/dashboard");
    this.meta = d;
    this.cards = [
      { k: "screens", l: "屏幕总数", v: d.screens },
      { k: "online", l: "在线屏幕", v: d.online_screens },
      { k: "assets", l: "可播素材", v: d.ready_assets },
      { k: "pubs", l: "正式发布", v: d.official_publications },
      { k: "shadows", l: "待升正影子", v: d.open_shadows },
      { k: "ov", l: "生效覆盖", v: d.active_overrides },
      { k: "rp", l: "推屏治理事件", v: d.remote_push_events },
      { k: "drill", l: "演练短路", v: d.remote_push_drills },
      { k: "contend", l: "互斥争用", v: d.remote_push_contentions },
    ];
  }
};
