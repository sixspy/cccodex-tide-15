<script setup>
import { computed, ref } from "vue";

const token = ref(localStorage.getItem("tide.token") || "");
const profile = ref(JSON.parse(localStorage.getItem("tide.profile") || "null"));
const username = ref("admin");
const password = ref("123456");
const loginError = ref("");
const batches = ref([]);
const current = ref(null);
const title = ref("");
const rawText = ref("");
const formError = ref("");
const busy = ref(false);

const keyState = ref({ today: "", setting: null, options: [], ready: false });
const keyList = ref([]);
const keyDetail = ref(null);
const keyDetailDate = ref("");
const keyError = ref("");
const keyBusy = ref(false);
const conflicts = ref([]);

const canWrite = computed(() => profile.value && profile.value.role === "writer");
const currentSetting = computed(() => keyState.value.setting);
const keyReady = computed(() => Boolean(currentSetting.value));
const blockedTotal = computed(() =>
  keyList.value.reduce((sum, item) => sum + (item.blocked_count || 0), 0)
);
const queuedConflicts = computed(() => conflicts.value.length);

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token.value) headers.Authorization = `Bearer ${token.value}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    let message = "请求没有完成";
    if (typeof detail === "string") message = detail;
    else if (detail && typeof detail === "object") message = detail.message || "请求没有完成";
    const error = new Error(message);
    error.detail = detail;
    throw error;
  }
  return data;
}

async function refresh() {
  batches.value = await request("/api/batches");
  if (!current.value && batches.value.length) {
    await openBatch(batches.value[0].id);
  }
}

async function refreshKeys() {
  const [state, list, queue] = await Promise.all([
    request("/api/dedup-key"),
    request("/api/dedup-keys"),
    request("/api/conflicts"),
  ]);
  keyState.value = state;
  keyList.value = list.keys;
  conflicts.value = queue.conflicts;
}

async function refreshAll() {
  await Promise.all([refresh(), refreshKeys()]);
}

async function openBatch(id) {
  current.value = await request(`/api/batches/${id}`);
}

async function submitLogin() {
  loginError.value = "";
  try {
    const data = await request("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value, password: password.value }),
    });
    token.value = data.access_token;
    profile.value = { username: data.username, role: data.role, title: data.title };
    localStorage.setItem("tide.token", token.value);
    localStorage.setItem("tide.profile", JSON.stringify(profile.value));
    await refreshAll();
  } catch (error) {
    loginError.value = error.message;
  }
}

async function pinKey(parts) {
  keyError.value = "";
  keyBusy.value = true;
  try {
    await request("/api/dedup-key", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parts }),
    });
    await refreshKeys();
  } catch (error) {
    keyError.value = error.message;
  } finally {
    keyBusy.value = false;
  }
}

async function openKeyDetail(item) {
  keyError.value = "";
  try {
    keyDetail.value = await request(`/api/dedup-keys/${item.key_date}`);
    keyDetailDate.value = item.key_date;
  } catch (error) {
    keyError.value = error.message;
  }
}

function closeKeyDetail() {
  keyDetail.value = null;
  keyDetailDate.value = "";
}

async function submitBatch() {
  formError.value = "";
  if (!keyReady.value) {
    formError.value = "当天去重键还没保存，定好键组成才能归集";
    return;
  }
  busy.value = true;
  try {
    const data = await request("/api/batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: title.value,
        raw_text: rawText.value,
        key_parts: currentSetting.value.parts,
      }),
    });
    title.value = "";
    rawText.value = "";
    await refreshAll();
    current.value = data;
  } catch (error) {
    const detail = error.detail;
    if (detail && typeof detail === "object" && detail.message) {
      formError.value = `${detail.message}：页面的键是 ${partsText(detail.carried_parts)}，当天钉住的是 ${partsText(detail.pinned_parts)}`;
    } else {
      formError.value = error.message;
    }
    await refreshKeys().catch(() => {});
    await refresh().catch(() => {});
  } finally {
    busy.value = false;
  }
}

const PART_LABELS = {
  station_code: "站码",
  observed_at: "观测时刻",
  quality_flag: "质量旗",
};

function partsText(parts) {
  if (!parts || !parts.length) return "—";
  return parts.map((part) => PART_LABELS[part] || part).join(" + ");
}

function logout() {
  token.value = "";
  profile.value = null;
  current.value = null;
  keyState.value = { today: "", setting: null, options: [], ready: false };
  keyList.value = [];
  keyDetail.value = null;
  conflicts.value = [];
  localStorage.removeItem("tide.token");
  localStorage.removeItem("tide.profile");
}

if (token.value) {
  refreshAll().catch(() => logout());
}
</script>

<template>
  <header class="top">
    <div>
      <p class="eyebrow">水文组 · 报文归集</p>
      <h1>潮位报文归集台</h1>
    </div>
    <div v-if="profile" class="who">
      <span>{{ profile.title }} {{ profile.username }}</span>
      <button class="ghost" @click="logout">离开</button>
    </div>
  </header>

  <main v-if="!profile" class="gate">
    <form @submit.prevent="submitLogin">
      <p>站码、时刻、潮位厘米、质量旗。一行一条，竖线分开。</p>
      <label>用户<input v-model="username" autocomplete="username" /></label>
      <label>口令<input v-model="password" type="password" autocomplete="current-password" /></label>
      <button>进入归集台</button>
      <p v-if="loginError" class="warn">{{ loginError }}</p>
    </form>
  </main>

  <main v-else class="desk">
    <div class="left">
      <section class="keybox">
        <h2>去重键</h2>
        <p class="rule">
          当前用
          <strong>{{ currentSetting ? currentSetting.name : "还没有定" }}</strong>
          <template v-if="currentSetting">（{{ currentSetting.label }}）</template>
        </p>
        <p v-if="currentSetting" class="quiet">
          {{ currentSetting.key_date }} 钉住，当天不得更换。被挡住的组合 {{ currentSetting.blocked_count }} 条。
        </p>
        <div v-if="!currentSetting && keyState.today" class="options">
          <p class="quiet">{{ keyState.today }} 还没有键设置，先选一种，保存后即钉住。</p>
          <button
            v-for="option in keyState.options"
            :key="option.name"
            class="option"
            :disabled="!canWrite || keyBusy"
            @click="pinKey(option.parts)"
          >
            <strong>{{ option.name }}</strong>
            <span>{{ option.label }}</span>
          </button>
          <p v-if="!canWrite" class="quiet">档案员只能查看键设置。</p>
        </div>
        <p v-if="keyError" class="warn">{{ keyError }}</p>
      </section>

      <section class="paste">
        <h2>贴入这一批</h2>
        <p class="rule">
          质量旗 A 实测，M 插补，B 可疑。这一批按
          <strong>{{ currentSetting ? currentSetting.label : "当日去重键" }}</strong>
          去重。
        </p>
        <label>批次名<input v-model="title" :disabled="!canWrite" placeholder="例如 堡镇 11-13 午班" /></label>
        <textarea
          v-model="rawText"
          :disabled="!canWrite || !keyReady"
          placeholder="BZ03|202611130900|176|A"
          spellcheck="false"
        />
        <button :disabled="!canWrite || !keyReady || busy" @click="submitBatch">归集这一批</button>
        <p v-if="!canWrite" class="quiet">档案员只能翻已落入的报文。</p>
        <p v-else-if="!keyReady" class="quiet">键设置保存完才能开始归集。</p>
        <p v-if="formError" class="warn">{{ formError }}</p>
      </section>

      <section class="queue">
        <h2>冲突队列 <em>{{ queuedConflicts }}</em></h2>
        <p class="rule">键里缺站码或时刻的行留在这里。</p>
        <ul>
          <li v-for="row in conflicts" :key="row.id">
            <code>{{ row.raw_line }}</code>
            <span>{{ row.reason }} · {{ row.batch_title }} 第 {{ row.line_no }} 行</span>
          </li>
          <li v-if="!conflicts.length" class="quiet">队列是空的。</li>
        </ul>
      </section>
    </div>

    <div class="right">
      <section class="keys">
        <h2>键列表 <em>被挡住 {{ blockedTotal }} 条</em></h2>
        <p class="rule">一天一种键。点开看被挡住的组合。</p>
        <button
          v-for="item in keyList"
          :key="item.id"
          class="keyrow"
          :class="{ on: keyDetailDate === item.key_date }"
          @click="openKeyDetail(item)"
        >
          <span class="keyname">{{ item.name }}</span>
          <strong>{{ item.label }}</strong>
          <span class="counts">
            {{ item.key_date }} · 冲突 {{ item.conflict_count }} 行 ·
            争用 {{ item.key_clash_count }} · 键不符 {{ item.key_mismatch_count }}
          </span>
        </button>
        <p v-if="!keyList.length" class="quiet">还没有键设置。</p>

        <article v-if="keyDetail" class="tape">
          <header>
            <h3>{{ keyDetail.key_date }} · {{ keyDetail.name }}</h3>
            <button class="ghost dark" @click="closeKeyDetail">收起</button>
          </header>
          <p class="rule">{{ keyDetail.label }}，由 {{ keyDetail.pinned_by }} 钉住。</p>
          <h4>被挡住的组合</h4>
          <table>
            <thead>
              <tr><th>组合</th><th>原因</th><th>次数</th><th>行号</th></tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in keyDetail.blocked" :key="index">
                <td>{{ row.kind }} · {{ row.identity }}</td>
                <td>{{ row.reason }}</td>
                <td>{{ row.hits }}</td>
                <td>{{ row.line_nos && row.line_nos.length ? row.line_nos.join("、") : "整批" }}</td>
              </tr>
              <tr v-if="!keyDetail.blocked.length"><td colspan="4">这一天没有被挡住的组合</td></tr>
            </tbody>
          </table>
        </article>
      </section>

      <section class="slips">
        <h2>已收批次</h2>
        <button
          v-for="batch in batches"
          :key="batch.id"
          class="slip"
          :class="{ on: current && current.batch.id === batch.id }"
          @click="openBatch(batch.id)"
        >
          <strong>{{ batch.title }}</strong>
          <em>{{ batch.status }}</em>
          <span>{{ batch.dedup_label }} · 落入 {{ batch.accepted_count }} 行 · 未接纳 {{ batch.rejected_count }} 行 · 冲突 {{ batch.conflict_count }} 行</span>
        </button>

        <article v-if="current" class="tape">
          <header>
            <h3>{{ current.batch.title }}</h3>
            <span>{{ current.batch.status }}</span>
          </header>
          <p class="rule">按 {{ current.batch.dedup_key_name }}（{{ current.batch.dedup_label }}）去重</p>
          <h4>已落库</h4>
          <table>
            <thead>
              <tr><th>行</th><th>站</th><th>时刻</th><th>厘米</th><th>旗</th><th>键</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in current.landed" :key="row.line_no">
                <td>{{ row.line_no }}</td>
                <td>{{ row.station_code }}</td>
                <td>{{ row.observed_at }}</td>
                <td>{{ row.tide_cm }}</td>
                <td>{{ row.flag_label }}</td>
                <td><code>{{ row.dedup_key_name }}</code></td>
              </tr>
              <tr v-if="!current.landed.length"><td colspan="6">这一批没有落入的行</td></tr>
            </tbody>
          </table>
          <h4>未接纳</h4>
          <ul>
            <li v-for="row in current.rejected" :key="row.line_no">
              <code>{{ row.raw_line }}</code>
              <span>{{ row.reason }}</span>
            </li>
            <li v-if="!current.rejected.length">没有被挡下的行</li>
          </ul>
          <h4>冲突队列</h4>
          <ul>
            <li v-for="row in current.conflicts" :key="row.line_no">
              <code>{{ row.raw_line }}</code>
              <span>{{ row.reason }}</span>
            </li>
            <li v-if="!current.conflicts.length">这一批没有进冲突队列的行</li>
          </ul>
        </article>
      </section>
    </div>
  </main>
</template>

<style scoped>
.top, .desk, .gate { width: min(1240px, calc(100% - 32px)); margin: 0 auto; }
.top { display: flex; justify-content: space-between; align-items: end; padding: 28px 0 12px; }
.eyebrow { letter-spacing: 0.18em; text-transform: uppercase; color: #d7a15f; font-size: 12px; margin: 0 0 6px; }
h1 { margin: 0; font-weight: 560; font-size: 34px; }
h2, h3, h4 { margin: 0; }
h2 em { font-style: normal; font-size: 13px; color: #d7a15f; margin-left: 6px; }
.who { display: flex; gap: 12px; align-items: center; }
.ghost { background: transparent; color: #f3efe2; border: 1px solid rgba(243, 239, 226, 0.35); padding: 6px 12px; }
.ghost.dark { color: #1c2430; border-color: rgba(28, 36, 48, 0.35); }
.gate { padding: 24px 0 48px; }
.gate form, .keybox, .paste, .queue, .keys, .tape {
  background: #f3efe2;
  color: #1c2430;
  padding: 22px;
}
.gate form { width: min(420px, 100%); display: grid; gap: 12px; }
label { display: grid; gap: 6px; font-size: 14px; }
input, textarea {
  width: 100%;
  border: 0;
  background: #fffdf8;
  color: #1c2430;
  padding: 10px 12px;
  box-shadow: inset 0 0 0 1px #d9d0bc;
}
textarea { min-height: 180px; font-family: "IBM Plex Mono", Consolas, monospace; line-height: 1.6; }
button { background: #9d2c2c; color: #fffaf2; padding: 10px 14px; }
.desk { display: grid; grid-template-columns: 380px 1fr; gap: 22px; padding-bottom: 48px; }
.left, .right { display: grid; gap: 18px; align-content: start; }
.keybox, .paste, .queue, .keys, .slips { display: grid; gap: 12px; align-content: start; }
.rule, .quiet { margin: 0; color: #5c5648; font-size: 14px; }
.options { display: grid; gap: 10px; }
.option {
  text-align: left;
  background: #fffdf8;
  color: #1c2430;
  box-shadow: inset 0 0 0 1px #d9d0bc;
  display: grid;
  gap: 2px;
}
.option span { color: #5c5648; font-size: 13px; }
.keyrow {
  text-align: left;
  background: transparent;
  color: #f3efe2;
  border: 1px solid rgba(243, 239, 226, 0.2);
  padding: 12px 14px;
  display: grid;
  gap: 4px;
}
.keyrow.on { border-color: #d7a15f; }
.keyname { color: #d7a15f; font-size: 13px; letter-spacing: 0.08em; }
.keyrow .counts { color: #b9b2a2; font-size: 13px; }
.slip {
  text-align: left;
  background: transparent;
  color: #f3efe2;
  border: 1px solid rgba(243, 239, 226, 0.2);
  padding: 12px 14px;
  display: grid;
  gap: 4px;
}
.slip.on { border-color: #d7a15f; }
.slip em { font-style: normal; color: #d7a15f; }
.slip span, .tape li span, .queue li span { color: #5c5648; }
.tape header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.tape h4 { margin: 14px 0 8px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid #e4dcc8; font-size: 14px; vertical-align: top; }
ul { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
li { display: grid; gap: 2px; }
code { font-family: Consolas, monospace; }
.warn { color: #9d2c2c; margin: 0; }
@media (max-width: 980px) {
  .desk { grid-template-columns: 1fr; }
}
</style>
