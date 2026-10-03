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
  if (row.status === "pending") return "候审";
  if (row.status === "processing") return "处理中";
  return "—";
}

function statusText(status) {
  if (status === "pending") return "候审";
  if (status === "processing") return "处理中";
  if (status === "done") return "已判定";
  return status;
}

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  user: null,
  loginForm: { username: "surveyor", password: "surv123456" },
  submitForm: { pair_id: "", main: "", sub: "" },
  pairForm: { span_code: "", main_point_code: "", sub_point_code: "" },
  editPair: null, // 正在编辑的配对 id
  thresholdDraft: "",
  pairs: [],
  rows: [],
  settings: null,
  rejections: [],
  error: "",
  msg: "",
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
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

async function loadReadings() {
  if (!state.token) return;
  try {
    state.rows = await api("/api/readings");
    state.error = "";
  } catch {
    /* 轮询静默 */
  }
  m.redraw();
}

async function loadPairs() {
  state.pairs = await api("/api/pairs");
  if (!state.submitForm.pair_id && state.pairs.length) {
    state.submitForm.pair_id = String(state.pairs[0].id);
  }
}

async function loadSettings() {
  state.settings = await api("/api/settings");
  if (state.thresholdDraft === "") {
    state.thresholdDraft = String(state.settings.diff_threshold);
  }
}

async function loadRejections() {
  state.rejections = await api("/api/rejections");
}

function logout() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  state.token = "";
  state.user = null;
  state.rows = [];
  state.pairs = [];
  if (state.timer) clearInterval(state.timer);
  m.route.set("/login");
}

function selectedPair() {
  return (
    state.pairs.find((p) => String(p.id) === String(state.submitForm.pair_id)) ||
    null
  );
}

/* ---------------- 登录 ---------------- */

const Login = {
  view: () =>
    m("div.wrap", [
      m("h1", "桥梁应变班交台"),
      m("p.sub", "同一跨段主副双测成对报送，差值超门槛整笔退回。"),
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
                m.route.set("/");
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
                  oninput: (e) => (state.loginForm.username = e.target.value),
                }),
              ]),
              m("label", [
                "密码",
                m("input", {
                  type: "password",
                  value: state.loginForm.password,
                  oninput: (e) => (state.loginForm.password = e.target.value),
                }),
              ]),
              m("button", { type: "submit", disabled: state.loading }, "登录"),
            ]),
            state.error ? m("p.err", state.error) : null,
          ]
        ),
        m(
          "p.sub",
          { style: { marginBottom: 0 } },
          "测量员 surveyor / surv123456 · 复核员 reviewer / rev123456"
        ),
      ]),
    ]),
};

/* ---------------- 顶栏（各页共用） ---------------- */

function TopBar() {
  const isWriter = state.user?.role === "writer";
  const current = m.route.get();
  const navItem = (path, label) =>
    m(
      "a.nav" + (current === path ? ".active" : ""),
      {
        href: `#!${path}`,
        onclick: (e) => {
          e.preventDefault();
          m.route.set(path);
        },
      },
      label
    );
  return m("div.topbar", [
    m("div", [
      m("h1", "桥梁应变班交台"),
      m("div.navrow", [
        navItem("/", "首页 · 双测报送"),
        navItem("/pairs", "双测配对专页"),
      ]),
    ]),
    m("div", [
      `${state.user?.username}（${isWriter ? "测量员" : "复核员"}） `,
      m("button.secondary", { type: "button", onclick: logout }, "退出"),
    ]),
  ]);
}

/* ---------------- 首页：双测报送 + 候审队列 ---------------- */

const Home = {
  oninit: async () => {
    loadReadings();
    try {
      await loadPairs();
    } catch {
      /* 忽略，轮询会重试读数 */
    }
    m.redraw();
  },
  view: () => {
    const isWriter = state.user?.role === "writer";
    const pair = selectedPair();
    return m("div.wrap", [
      TopBar(),
      isWriter
        ? m("div.card", [
            m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "双测报送（主副同交）"),
            m(
              "p.sub",
              { style: { margin: "0 0 0.75rem" } },
              "同一跨段须主、副测点成对，一次提交两个微应变；" +
                "差值绝对值超门槛整笔退回，配对关系随单冻结。"
            ),
            m(
              "form",
              {
                onsubmit: async (e) => {
                  e.preventDefault();
                  state.error = "";
                  state.msg = "";
                  state.loading = true;
                  try {
                    const data = await api("/api/readings", {
                      method: "POST",
                      body: JSON.stringify({
                        pair_id: Number(state.submitForm.pair_id),
                        main_microstrain: parseFloat(state.submitForm.main),
                        sub_microstrain: parseFloat(state.submitForm.sub),
                      }),
                    });
                    state.msg = data.message || "已入队候审";
                    state.submitForm.main = "";
                    state.submitForm.sub = "";
                    await loadReadings();
                  } catch (err) {
                    state.error = err.message || "报送失败";
                  } finally {
                    state.loading = false;
                    m.redraw();
                  }
                },
              },
              [
                m("div.row", [
                  m("label", [
                    "双测配对",
                    m(
                      "select",
                      {
                        onchange: (e) =>
                          (state.submitForm.pair_id = e.target.value),
                      },
                      state.pairs.map((p) =>
                        m(
                          "option",
                          {
                            value: p.id,
                            selected: String(p.id) ===
                              String(state.submitForm.pair_id),
                          },
                          `${p.span_code}（主：${p.main_point_code} / 副：${p.sub_point_code}）`
                        )
                      )
                    ),
                  ]),
                  m("label", [
                    `主测点微应变（με）${pair ? "· " + pair.main_point_code : ""}`,
                    m("input", {
                      required: true,
                      type: "number",
                      step: "0.1",
                      value: state.submitForm.main,
                      oninput: (e) => (state.submitForm.main = e.target.value),
                    }),
                  ]),
                  m("label", [
                    `副测点微应变（με）${pair ? "· " + pair.sub_point_code : ""}`,
                    m("input", {
                      required: true,
                      type: "number",
                      step: "0.1",
                      value: state.submitForm.sub,
                      oninput: (e) => (state.submitForm.sub = e.target.value),
                    }),
                  ]),
                  m("button", { type: "submit", disabled: state.loading }, "成对提交"),
                ]),
                state.error ? m("p.err", state.error) : null,
                state.msg ? m("p.ok", state.msg) : null,
              ]
            ),
          ])
        : m("div.card", [
            m("h2", { style: { marginTop: 0, fontSize: "1.1rem" } }, "候审/判定队列"),
            m("p.sub", { style: { margin: 0 } }, "复核员可查看配对关系与主副差值，不能报送、不能改配对。"),
          ]),
      m("div.card", [
        m("table", [
          m("thead", [
            m("tr", [
              m("th", "单号"),
              m("th", "跨段（冻结）"),
              m("th", "主测点"),
              m("th", "副测点"),
              m("th", "主值"),
              m("th", "副值"),
              m("th", "差值|με|"),
              m("th", "结论"),
              m("th", "说明"),
              m("th", "状态"),
              m("th", "提交人"),
            ]),
          ]),
          m(
            "tbody",
            state.rows.length
              ? state.rows.map((r) =>
                  m("tr", { key: r.id }, [
                    m("td", r.id),
                    m("td", r.span_code_frozen || r.span_code),
                    m("td", `${r.main_point_code || "—"}`),
                    m("td", `${r.sub_point_code || "—"}`),
                    m("td", r.main_microstrain ?? "—"),
                    m("td", r.sub_microstrain ?? "—"),
                    m("td", r.abs_diff ?? "—"),
                    m("td", [
                      m("span", { class: verdictClass(r.verdict, r.status) }, displayVerdict(r)),
                    ]),
                    m("td", r.reason || "—"),
                    m("td", statusText(r.status)),
                    m("td", r.created_by),
                  ])
                )
              : [m("tr", m("td", { colspan: 11 }, "暂无数据"))]
          ),
        ]),
      ]),
    ]);
  },
};

/* ---------------- 双测配对专页 ---------------- */

function PairMaintenance(isWriter) {
  return m("section.block", [
    m("h3", "配对维护"),
    m(
      "table",
      [
        m("thead", m("tr", [
          m("th", "跨段编号"),
          m("th", "主测点编号"),
          m("th", "副测点编号"),
          m("th", "操作"),
        ])),
        m(
          "tbody",
        state.pairs.length
          ? state.pairs.map((p) => {
              const editing = state.editPair === p.id;
              if (editing && isWriter) {
                const draft = state.pairForm;
                return m("tr", { key: p.id }, [
                  m("td", m("input", {
                    value: draft.span_code,
                    oninput: (e) => (draft.span_code = e.target.value),
                  })),
                  m("td", m("input", {
                    value: draft.main_point_code,
                    oninput: (e) => (draft.main_point_code = e.target.value),
                  })),
                  m("td", m("input", {
                    value: draft.sub_point_code,
                    oninput: (e) => (draft.sub_point_code = e.target.value),
                  })),
                  m("td", [
                    m(
                      "button.mini",
                      {
                        type: "button",
                        onclick: async () => {
                          state.error = "";
                          state.msg = "";
                          try {
                            await api(`/api/pairs/${p.id}`, {
                              method: "PATCH",
                              body: JSON.stringify({ ...draft }),
                            });
                            state.editPair = null;
                            state.msg = "配对已更新（旧单点号已冻结不受影响）";
                            await loadPairs();
                          } catch (err) {
                            state.error = err.message;
                          }
                          m.redraw();
                        },
                      },
                      "保存"
                    ),
                    m(
                      "button.mini.secondary",
                      { type: "button", onclick: () => (state.editPair = null) },
                      "取消"
                    ),
                  ]),
                ]);
              }
              return m("tr", { key: p.id }, [
                m("td", p.span_code),
                m("td", p.main_point_code),
                m("td", p.sub_point_code),
                m(
                  "td",
                  isWriter
                    ? m(
                        "button.mini.secondary",
                        {
                          type: "button",
                          onclick: () => {
                            state.editPair = p.id;
                            state.pairForm = {
                              span_code: p.span_code,
                              main_point_code: p.main_point_code,
                              sub_point_code: p.sub_point_code,
                            };
                            state.error = "";
                            state.msg = "";
                          },
                        },
                        "改配对"
                      )
                    : "只读"
                ),
              ]);
            })
          : [m("tr", m("td", { colspan: 4 }, "暂无配对"))]
        )
      ]
    ),
    isWriter
      ? m(
          "form.addform",
          {
            onsubmit: async (e) => {
              e.preventDefault();
              state.error = "";
              state.msg = "";
              try {
                await api("/api/pairs", {
                  method: "POST",
                  body: JSON.stringify({ ...state.pairForm }),
                });
                state.pairForm = { span_code: "", main_point_code: "", sub_point_code: "" };
                state.msg = "配对已登记";
                await loadPairs();
              } catch (err) {
                state.error = err.message;
              }
              m.redraw();
            },
          },
          [
            m("div.row", [
              m("label", ["跨段编号", m("input", {
                placeholder: "例如 跨中乙",
                value: state.pairForm.span_code,
                oninput: (e) => (state.pairForm.span_code = e.target.value),
              })]),
              m("label", ["主测点编号", m("input", {
                placeholder: "例如 跨中乙-主",
                value: state.pairForm.main_point_code,
                oninput: (e) => (state.pairForm.main_point_code = e.target.value),
              })]),
              m("label", ["副测点编号", m("input", {
                placeholder: "例如 跨中乙-副",
                value: state.pairForm.sub_point_code,
                oninput: (e) => (state.pairForm.sub_point_code = e.target.value),
              })]),
              m("button", { type: "submit" }, "登记配对"),
            ]),
          ]
        )
      : null,
  ]);
}

function ThresholdBlock(isWriter) {
  return m("section.block", [
    m("h3", "差值门槛"),
    m(
      "p.sub",
      { style: { margin: "0 0 0.5rem" } },
      state.settings
        ? `当前门槛：${state.settings.diff_threshold} με` +
          (state.settings.updated_by ? `（${state.settings.updated_by} 设置）` : "（默认）") +
          "。主副差值绝对值超过门槛即整笔退回。"
        : "加载中…"
    ),
    isWriter
      ? m(
          "form",
          {
            onsubmit: async (e) => {
              e.preventDefault();
              state.error = "";
              state.msg = "";
              try {
                await api("/api/settings", {
                  method: "PUT",
                  body: JSON.stringify({
                    diff_threshold: parseFloat(state.thresholdDraft),
                  }),
                });
                state.msg = "差值门槛已更新";
                await loadSettings();
              } catch (err) {
                state.error = err.message;
              }
              m.redraw();
            },
          },
          m("div.row", [
            m("label", ["门槛（με）", m("input", {
              type: "number",
              step: "0.1",
              min: "0",
              value: state.thresholdDraft,
              oninput: (e) => (state.thresholdDraft = e.target.value),
            })]),
            m("button", { type: "submit" }, "保存门槛"),
          ])
        )
      : m("p.sub", { style: { margin: 0 } }, "复核员只读，不能修改门槛。"),
  ]);
}

function RejectionBlock() {
  return m("section.block", [
    m("h3", "退回样例（双测超差留痕）"),
    m("table", [
      m("thead", [
        m("tr", [
          m("th", "退回号"),
          m("th", "跨段"),
          m("th", "主测点"),
          m("th", "副测点"),
          m("th", "主值"),
          m("th", "副值"),
          m("th", "差值"),
          m("th", "门槛"),
          m("th", "退回原因"),
          m("th", "退回时间"),
        ]),
      ]),
      m(
        "tbody",
        state.rejections.length
          ? state.rejections.map((r) =>
              m("tr", { key: r.id }, [
                m("td", r.id),
                m("td", r.span_code),
                m("td", r.main_point_code),
                m("td", r.sub_point_code),
                m("td", r.main_microstrain),
                m("td", r.sub_microstrain),
                m("td", String(r.abs_diff)),
                m("td", String(r.threshold)),
                m("td", r.reason),
                m("td", (r.rejected_at || "").replace("T", " ").slice(0, 19)),
              ])
            )
          : [m("tr", m("td", { colspan: 10 }, "暂无退回样例"))]
      ),
    ]),
  ]);
}

const PairAdmin = {
  oninit: async () => {
    state.error = "";
    state.msg = "";
    try {
      await Promise.all([loadPairs(), loadSettings(), loadRejections()]);
    } catch {
      state.error = "加载专页数据失败，请重新登录";
    }
    m.redraw();
  },
  view: () => {
    const isWriter = state.user?.role === "writer";
    return m("div.wrap", [
      TopBar(),
      m("div.card", [
        m("h2", { style: { marginTop: 0, fontSize: "1.15rem" } }, "双测配对专页"),
        m(
          "p.sub",
          { style: { margin: "0 0 1rem" } },
          "配对关系随单冻结：此处改配对只影响新单，旧单点号以报送时快照为准。"
        ),
        state.error ? m("p.err", state.error) : null,
        state.msg ? m("p.ok", state.msg) : null,
        PairMaintenance(isWriter),
        m("hr"),
        ThresholdBlock(isWriter),
        m("hr"),
        RejectionBlock(),
      ]),
    ]);
  },
};

/* ---------------- 路由 ---------------- */

m.route.prefix = "#!";

function startPolling() {
  if (state.timer) clearInterval(state.timer);
  if (!state.token) return;
  state.timer = setInterval(loadReadings, 3000);
}

function gate() {
  if (!state.token) {
    m.route.set("/login");
    return false;
  }
  return true;
}

const HomePage = {
  oninit() {
    if (!gate()) return;
    startPolling();
  },
  view: () => (state.token ? m(Home) : null),
};

const PairsPage = {
  oninit() {
    gate();
  },
  view: () => (state.token ? m(PairAdmin) : null),
};

m.route(document.getElementById("app"), "/", {
  "/": HomePage,
  "/pairs": PairsPage,
  "/login": Login,
});
