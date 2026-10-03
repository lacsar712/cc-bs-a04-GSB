import m from "mithril";

const TOKEN_KEY = "bridge_strain_token";
const USER_KEY = "bridge_strain_user";

function verdictClass(verdict, status) {
  if (verdict === "合格") return "tag pass";
  if (verdict === "越界") return "tag fail";
  return "tag wait";
}

function displayVerdict(row) {
  if (row.verdict) return row.verdict;
  if (row.status === "pending") return "待处理";
  if (row.status === "processing") return "处理中";
  return "—";
}

function batchStatusTag(status) {
  if (status === "done") return m("span.tag.pass", "已判定");
  if (status === "pending") return m("span.tag.wait", "候审");
  if (status === "processing") return m("span.tag.wait", "处理中");
  if (status === "rejected") return m("span.tag.fail", "退回");
  return m("span.tag.wait", status);
}

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  user: null,
  page: "home",
  loginForm: { username: "surveyor", password: "surv123456" },
  submitForm: { span_code: "", primary_value: "", secondary_value: "" },
  pairingForm: { span_code: "", primary_point: "", secondary_point: "" },
  thresholdForm: { diff_threshold: "" },
  pairings: [],
  batches: [],
  rows: [],
  threshold: null,
  error: "",
  msg: "",
  pairingError: "",
  pairingMsg: "",
  thresholdError: "",
  thresholdMsg: "",
  loading: false,
  timer: null,
};

try {
  state.user = JSON.parse(localStorage.getItem(USER_KEY) || "null");
} catch {
  state.user = null;
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, { ...opts, headers });
  const text = await res.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    const err = new Error(data.detail || res.statusText);
    err.payload = data;
    throw err;
  }
  return data;
}

async function loadAll() {
  if (!state.token) return;
  const results = await Promise.allSettled([
    api("/api/pairings"),
    api("/api/batches"),
    api("/api/readings"),
    api("/api/config"),
  ]);
  if (results[0].status === "fulfilled") state.pairings = results[0].value;
  if (results[1].status === "fulfilled") state.batches = results[1].value;
  if (results[2].status === "fulfilled") state.rows = results[2].value;
  if (results[3].status === "fulfilled") {
    state.threshold = results[3].value.diff_threshold;
    if (state.thresholdForm.diff_threshold === "") {
      state.thresholdForm.diff_threshold = String(state.threshold);
    }
  }
  m.redraw();
}

function startPolling() {
  if (state.timer) clearInterval(state.timer);
  if (!state.token) return;
  state.timer = setInterval(loadAll, 3000);
}

function logout() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  state.token = "";
  state.user = null;
  state.pairings = [];
  state.batches = [];
  state.rows = [];
  if (state.timer) clearInterval(state.timer);
}

const LoginView = {
  view: () =>
    m("div.wrap", [
      m("h1", "桥梁应变班交台"),
      m("p.sub", "同一跨段双测成对：测量员先在双测配对专页登记主副测点，再在首页一次提交两个读数。"),
      m("div.card", [
        m(
          "form",
          {
            onsubmit: async (e) => {
              e.preventDefault();
              state.error = "";
              state.loading = true;
              try {
                const data = await api("/api/auth/login", {
                  method: "POST",
                  body: JSON.stringify(state.loginForm),
                });
                state.token = data.access_token;
                state.user = { username: data.username, role: data.role };
                localStorage.setItem(TOKEN_KEY, state.token);
                localStorage.setItem(USER_KEY, JSON.stringify(state.user));
                await loadAll();
                startPolling();
              } catch {
                state.error = "用户名或密码错误";
              } finally {
                state.loading = false;
                m.redraw();
              }
            },
          },
          [
            m("div.row", [
              m("label", [
                "用户名",
                m("input", {
                  value: state.loginForm.username,
                  oninput: (e) => {
                    state.loginForm.username = e.target.value;
                  },
                }),
              ]),
              m("label", [
                "密码",
                m("input", {
                  type: "password",
                  value: state.loginForm.password,
                  oninput: (e) => {
                    state.loginForm.password = e.target.value;
                  },
                }),
              ]),
              m("button", { type: "submit", disabled: state.loading }, "登录"),
            ]),
            state.error ? m("p.err", state.error) : null,
          ]
        ),
        m("p.sub", { style: { marginBottom: 0 } },
          "测量员 surveyor / surv123456 · 复核员 reviewer / rev123456"),
      ]),
    ]),
};

function topbar(isWriter) {
  return m("div.topbar", [
    m("div", [
      m("h1", "桥梁应变班交台"),
      m("p.sub", "微应变 80～220 με 为合格；双测差值超门槛整笔退回。"),
      m("nav.tabs", [
        m(
          "button.secondary" + (state.page === "home" ? ".active" : ""),
          { type: "button", onclick: () => { state.page = "home"; } },
          "首页 · 双测报送"
        ),
        m(
          "button.secondary" + (state.page === "pairings" ? ".active" : ""),
          { type: "button", onclick: () => { state.page = "pairings"; } },
          "双测配对专页"
        ),
      ]),
    ]),
    m("div", [
      `${state.user?.username}（${isWriter ? "测量员" : "复核员"}） `,
      m("button.secondary", { type: "button", onclick: logout }, "退出"),
    ]),
  ]);
}

function submitCard() {
  return m("div.card", [
    m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "双测报送（同一跨段主副两个读数一次提交）"),
    m(
      "form",
      {
        onsubmit: async (e) => {
          e.preventDefault();
          state.error = "";
          state.msg = "";
          state.loading = true;
          try {
            const data = await api("/api/batches", {
              method: "POST",
              body: JSON.stringify({
                span_code: state.submitForm.span_code,
                primary_value: parseFloat(state.submitForm.primary_value),
                secondary_value: parseFloat(state.submitForm.secondary_value),
              }),
            });
            state.msg = data.message || "已入候审队列";
            state.submitForm = { span_code: "", primary_value: "", secondary_value: "" };
            await loadAll();
          } catch (err) {
            state.error = err.message || "提交失败，整笔退回";
          } finally {
            state.loading = false;
            m.redraw();
          }
        },
      },
      [
        m("div.row", [
          m("label", [
            "跨段编号（须已登记配对）",
            m("input", {
              required: true,
              placeholder: "例如 跨中甲",
              value: state.submitForm.span_code,
              oninput: (e) => { state.submitForm.span_code = e.target.value; },
            }),
          ]),
          m("label", [
            "主测点微应变（με）",
            m("input", {
              required: true,
              type: "number",
              step: "0.1",
              value: state.submitForm.primary_value,
              oninput: (e) => { state.submitForm.primary_value = e.target.value; },
            }),
          ]),
          m("label", [
            "副测点微应变（με）",
            m("input", {
              required: true,
              type: "number",
              step: "0.1",
              value: state.submitForm.secondary_value,
              oninput: (e) => { state.submitForm.secondary_value = e.target.value; },
            }),
          ]),
          m("button", { type: "submit", disabled: state.loading }, "成对提交"),
        ]),
        state.threshold !== null
          ? m("p.sub", { style: { marginTop: "0.5rem" } },
              `当前差值门槛：${state.threshold} με；|主−副| 超门槛整笔退回。`)
          : null,
        state.error ? m("p.err", state.error) : null,
        state.msg ? m("p.ok", state.msg) : null,
      ]
    ),
  ]);
}

function batchesCard() {
  return m("div.card", [
    m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "双测批次"),
    m("table", [
      m("thead", [
        m("tr", [
          m("th", "批次"),
          m("th", "跨段"),
          m("th", "主测点"),
          m("th", "副测点"),
          m("th", "主读数"),
          m("th", "副读数"),
          m("th", "差值"),
          m("th", "门槛"),
          m("th", "状态"),
          m("th", "退回原因"),
          m("th", "提交人"),
        ]),
      ]),
      m(
        "tbody",
        state.batches.length
          ? state.batches.map((b) =>
              m("tr", { key: b.id, class: b.status === "rejected" ? "rejected" : "" }, [
                m("td", b.id),
                m("td", b.span_code),
                m("td", b.primary_point),
                m("td", b.secondary_point),
                m("td", b.primary_value),
                m("td", b.secondary_value),
                m("td", b.diff_value),
                m("td", b.threshold_value),
                m("td", batchStatusTag(b.status)),
                m("td", b.reject_reason || "—"),
                m("td", b.created_by),
              ])
            )
          : [m("tr", m("td", { colspan: 11 }, "暂无批次"))]
      ),
    ]),
  ]);
}

function readingsCard() {
  return m("div.card", [
    m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "读数明细"),
    m("table", [
      m("thead", [
        m("tr", [
          m("th", "编号"), m("th", "批次"), m("th", "跨段"), m("th", "测点"),
          m("th", "主/副"), m("th", "微应变"), m("th", "双测差值"),
          m("th", "结论"), m("th", "说明"), m("th", "状态"), m("th", "提交人"),
        ]),
      ]),
      m(
        "tbody",
        state.rows.length
          ? state.rows.map((r) =>
              m("tr", { key: r.id }, [
                m("td", r.id),
                m("td", r.batch_id ?? "—"),
                m("td", r.span_code),
                m("td", r.point_code || "—"),
                m("td", r.point_role === "primary" ? "主"
                  : r.point_role === "secondary" ? "副" : "—"),
                m("td", r.microstrain),
                m("td", r.diff_value ?? "—"),
                m("td", m("span", { class: verdictClass(r.verdict, r.status) }, displayVerdict(r))),
                m("td", r.reason || "—"),
                m("td", r.status),
                m("td", r.created_by),
              ])
            )
          : [m("tr", m("td", { colspan: 11 }, "暂无数据"))]
      ),
    ]),
  ]);
}

function pairingMaintenanceCard(isWriter) {
  return m("div.card", [
    m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "配对维护"),
    isWriter
      ? m(
          "form",
          {
            onsubmit: async (e) => {
              e.preventDefault();
              state.pairingError = "";
              state.pairingMsg = "";
              try {
                await api("/api/pairings", {
                  method: "POST",
                  body: JSON.stringify(state.pairingForm),
                });
                state.pairingMsg = "配对已登记";
                state.pairingForm = { span_code: "", primary_point: "", secondary_point: "" };
                await loadAll();
              } catch (err) {
                state.pairingError = err.message || "登记失败";
              }
              m.redraw();
            },
          },
          [
            m("div.row", [
              m("label", [
                "跨段编号",
                m("input", {
                  required: true,
                  placeholder: "例如 跨中甲",
                  value: state.pairingForm.span_code,
                  oninput: (e) => { state.pairingForm.span_code = e.target.value; },
                }),
              ]),
              m("label", [
                "主测点编号",
                m("input", {
                  required: true,
                  placeholder: "例如 跨中甲-主",
                  value: state.pairingForm.primary_point,
                  oninput: (e) => { state.pairingForm.primary_point = e.target.value; },
                }),
              ]),
              m("label", [
                "副测点编号",
                m("input", {
                  required: true,
                  placeholder: "例如 跨中甲-副",
                  value: state.pairingForm.secondary_point,
                  oninput: (e) => { state.pairingForm.secondary_point = e.target.value; },
                }),
              ]),
              m("button", { type: "submit" }, "登记配对"),
            ]),
            state.pairingError ? m("p.err", state.pairingError) : null,
            state.pairingMsg ? m("p.ok", state.pairingMsg) : null,
          ]
        )
      : m("p.sub", "复核员仅有只读权限，不能登记或修改配对。"),
    m("table", { style: { marginTop: "0.75rem" } }, [
      m("thead", [
        m("tr", [
          m("th", "编号"), m("th", "跨段"), m("th", "主测点"), m("th", "副测点"),
          m("th", "冻结状态"), m("th", "登记人"),
        ]),
      ]),
      m(
        "tbody",
        state.pairings.length
          ? state.pairings.map((p) =>
              m("tr", { key: p.id }, [
                m("td", p.id),
                m("td", p.span_code),
                m("td", p.primary_point),
                m("td", p.secondary_point),
                m("td", p.frozen ? m("span.tag.fail", "已随单冻结") : m("span.tag.pass", "未冻结")),
                m("td", p.created_by),
              ])
            )
          : [m("tr", m("td", { colspan: 6 }, "暂无配对"))]
      ),
    ]),
    m("p.sub", { style: { marginBottom: 0 } },
      "配对一旦随双测单提交即冻住：事后修改配对表不影响已入队旧单上的点号与门槛。"),
  ]);
}

function thresholdCard(isWriter) {
  return m("div.card", [
    m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "差值门槛"),
    isWriter
      ? m(
          "form",
          {
            onsubmit: async (e) => {
              e.preventDefault();
              state.thresholdError = "";
              state.thresholdMsg = "";
              try {
                const data = await api("/api/config/threshold", {
                  method: "PUT",
                  body: JSON.stringify({
                    diff_threshold: parseFloat(state.thresholdForm.diff_threshold),
                  }),
                });
                state.threshold = data.diff_threshold;
                state.thresholdMsg = `门槛已更新为 ${data.diff_threshold} με（旧单仍按提交时门槛）`;
                await loadAll();
              } catch (err) {
                state.thresholdError = err.message || "更新失败";
              }
              m.redraw();
            },
          },
          [
            m("div.row", [
              m("label", [
                "双测差值门槛（με）",
                m("input", {
                  required: true,
                  type: "number",
                  min: "0",
                  step: "0.1",
                  value: state.thresholdForm.diff_threshold,
                  oninput: (e) => { state.thresholdForm.diff_threshold = e.target.value; },
                }),
              ]),
              m("button", { type: "submit" }, "保存门槛"),
            ]),
            state.thresholdError ? m("p.err", state.thresholdError) : null,
            state.thresholdMsg ? m("p.ok", state.thresholdMsg) : null,
          ]
        )
      : m("p.sub", `复核员只读：当前差值门槛 ${state.threshold ?? "—"} με，不能修改。`),
  ]);
}

function rejectedSamplesCard() {
  const rejected = state.batches.filter((b) => b.status === "rejected");
  return m("div.card", [
    m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "退回样例（双测超差）"),
    rejected.length
      ? m("table", [
          m("thead", [
            m("tr", [
              m("th", "批次"), m("th", "跨段"), m("th", "主测点/读数"),
              m("th", "副测点/读数"), m("th", "差值"), m("th", "门槛"),
              m("th", "退回说明"), m("th", "提交人"),
            ]),
          ]),
          m("tbody",
            rejected.map((b) =>
              m("tr.rejected", { key: b.id }, [
                m("td", b.id),
                m("td", b.span_code),
                m("td", `${b.primary_point} / ${b.primary_value}`),
                m("td", `${b.secondary_point} / ${b.secondary_value}`),
                m("td", b.diff_value),
                m("td", b.threshold_value),
                m("td", b.reject_reason),
                m("td", b.created_by),
              ])
            )
          ),
        ])
      : m("p.sub", { style: { marginBottom: 0 } }, "暂无双测超差退回样例。"),
  ]);
}

const App = {
  oninit() {
    loadAll();
    startPolling();
  },
  onremove() {
    if (state.timer) clearInterval(state.timer);
  },
  view() {
    if (!state.token) return m(LoginView);

    const isWriter = state.user?.role === "writer";

    return m("div.wrap", [
      topbar(isWriter),
      state.page === "home"
        ? [
            isWriter ? submitCard() : null,
            batchesCard(),
            readingsCard(),
          ]
        : [
            pairingMaintenanceCard(isWriter),
            thresholdCard(isWriter),
            rejectedSamplesCard(),
          ],
    ]);
  },
};

export default App;
