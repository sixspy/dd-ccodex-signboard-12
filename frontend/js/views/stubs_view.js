window.SB_VIEWS = window.SB_VIEWS || {};
window.SB_VIEWS["stubs-view"] = {
  template: `
  <div>
    <div class="panel">
      <h2>空桩桥 · 远程推屏治理 <span class="badge">signboard-12</span></h2>
      <p class="lead">远程推屏演练禁写正式备注：演练走甲路径，在正式写之前短路，屏幕正式字段零改动；只有显式实推才在命名互斥锁内写 <code>screens.note</code>。</p>

      <div class="row-form">
        <label>屏幕ID
          <input data-testid="push-screen-id" v-model.number="form.screen_id" type="number" min="1" placeholder="屏幕ID" />
        </label>
        <label style="flex:1;min-width:260px;">推屏备注 note
          <input data-testid="push-note" v-model="form.note" placeholder="演练脏字不会进正式备注" />
        </label>
        <button class="ghost" type="button" @click="loadScreen">读取当前正式备注</button>
      </div>

      <div v-if="current" class="lead" data-testid="current-official-note" style="margin:8px 0;">
        当前屏幕 <b>#{{ current.id }} {{ current.code }}</b>（{{ current.title }}）的正式备注：
        <code>{{ current.note || "（空）" }}</code>
      </div>

      <div class="row-form">
        <button data-testid="drill-btn" class="ghost" type="button" @click="submit('drill')" :disabled="busy">演练（甲：短路拒写）</button>
        <button data-testid="real-push-btn" class="primary" type="button" @click="submit('live')" :disabled="busy">实推（写正式备注）</button>
        <button data-testid="contend-btn" class="ghost" type="button" @click="contendMutex" :disabled="busy">双会话抢「远程推屏演练互斥」</button>
        <button data-testid="release-lock-btn" class="ghost" type="button" @click="releaseMutex" :disabled="busy">释放命名锁</button>
      </div>
      <p class="err" data-testid="push-error" v-if="error">{{ error }}</p>

      <div v-if="pair" data-testid="contend-pair" style="display:flex;gap:12px;flex-wrap:wrap;margin:10px 0;">
        <div v-for="(r,i) in pair" :key="i"
             :style="{flex:'1',minWidth:'240px',padding:'12px',borderRadius:'10px',
                      background: r.status===200 ? '#10331f' : '#3a1620',
                      border: '1px solid ' + (r.status===200 ? '#2f7d4f' : '#a23a52')}">
          <b>会话 {{ i+1 }}：HTTP {{ r.status }}</b>
          <div :style="{color: r.status===200 ? '#7fe0a8' : '#ff9db0', marginTop:'6px'}">
            {{ r.status===200 ? '胜方：获得互斥/可落正式' : '败方：记争用，禁止成功句' }}
          </div>
          <pre style="white-space:pre-wrap;margin:8px 0 0;background:#0d1828;padding:8px;border-radius:8px;">{{ JSON.stringify(r.body, null, 2) }}</pre>
        </div>
      </div>

      <pre v-if="out" data-testid="push-output" style="white-space:pre-wrap;background:#0d1828;padding:12px;border-radius:10px;">{{ out }}</pre>
    </div>

    <div class="panel">
      <h3>空桩清单</h3>
      <p class="lead">media_bridge 仍固定 501；remote_push 已按 signboard-12 加治理（裸探针仍返回 501）。</p>
      <div class="row-form">
        <button class="ghost" @click="call('remote_push')">裸探针试调远程推屏（{}）</button>
        <button class="ghost" @click="call('media_bridge')">试调媒资桥</button>
      </div>
      <pre style="white-space:pre-wrap;background:#0d1828;padding:12px;border-radius:10px;">{{ stubOut }}</pre>
      <table>
        <thead><tr><th>键</th><th>标题</th><th>说明</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="s in items" :key="s.key">
            <td>{{ s.key }}</td><td>{{ s.title }}</td><td>{{ s.desc }}</td><td>{{ s.status }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>`,
  data() {
    return {
      items: [],
      stubOut: "",
      out: "",
      error: "",
      busy: false,
      pair: null,
      current: null,
      form: { screen_id: 1, note: "演练脏备注-不应进正式字段" },
    };
  },
  async mounted() {
    this.items = (await SB.api("/api/stubs")).items;
    this.loadScreen();
  },
  methods: {
    async loadScreen() {
      this.error = "";
      try {
        const data = await SB.api("/api/screens");
        const sid = Number(this.form.screen_id);
        this.current = data.items.find((s) => s.id === sid) || null;
      } catch (e) {
        this.error = e.message;
      }
    },
    async submit(mode) {
      this.error = "";
      this.out = "";
      this.pair = null;
      this.busy = true;
      const payload = mode === "drill"
        ? { screen_id: Number(this.form.screen_id), dry_run: true, note: this.form.note }
        : { screen_id: Number(this.form.screen_id), dry_run: false, confirm: true, note: this.form.note };
      try {
        const r = await fetch("/api/stubs/remote_push", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + SB.token },
          body: JSON.stringify(payload),
        });
        const body = await r.json().catch(() => ({}));
        this.out = "HTTP " + r.status + "\n" + JSON.stringify(body, null, 2);
        if (!r.ok) this.error = (body && (body.detail || body.message)) || ("HTTP " + r.status);
        await this.loadScreen();
      } catch (e) {
        this.error = String(e);
      } finally {
        this.busy = false;
      }
    },
    async _rawPost(url, payload) {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + SB.token },
        body: JSON.stringify(payload || {}),
      });
      const body = await r.json().catch(() => ({}));
      return { status: r.status, body };
    },
    async contendMutex() {
      this.error = "";
      this.out = "";
      this.busy = true;
      this.pair = null;
      const key = "远程推屏演练互斥";
      // 双请求同发：严格互斥 + UNIQUE 线性化，即使同一会话也只许一个领取成功。
      const [r1, r2] = await Promise.all([
        this._rawPost("/api/locks/acquire", { lock_key: key, ttl_sec: 60 }),
        this._rawPost("/api/locks/acquire", { lock_key: key, ttl_sec: 60 }),
      ]);
      this.pair = [r1, r2];
      this.busy = false;
    },
    async releaseMutex() {
      this.error = "";
      try {
        await SB.api("/api/locks/release", {
          method: "POST",
          body: JSON.stringify({ lock_key: "远程推屏演练互斥" }),
        });
        this.out = "已释放「远程推屏演练互斥」";
        this.pair = null;
      } catch (e) {
        this.error = e.message;
      }
    },
    async call(name) {
      this.stubOut = "调用中…";
      try {
        const res = await fetch("/api/stubs/" + name, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + SB.token },
          body: "{}",
        });
        this.stubOut = "HTTP " + res.status + "\n" + (await res.text());
      } catch (e) {
        this.stubOut = String(e);
      }
    },
  },
};
