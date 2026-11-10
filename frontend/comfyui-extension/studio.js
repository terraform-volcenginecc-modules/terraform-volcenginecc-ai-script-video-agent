import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

let csrf = "";
let currentProject = "";
const originalFetch = api.fetchApi.bind(api);
const activeJobs = new Set(["queued", "running", "waiting", "uncertain"]);
const jobNames = { script: "剧本", video: "视频", compose: "拼接" };
const jobStatuses = {
  draft: "待生成", queued: "排队中", running: "执行中", waiting: "等待中",
  uncertain: "待核对", failed: "失败", succeeded: "已生成", cancelled: "已取消",
};

async function session() {
  const response = await fetch("/studio-api/session");
  if (response.status === 401) { location.replace("/login"); throw new Error("请先登录"); }
  if (!response.ok) throw new Error("获取登录状态失败");
  csrf = (await response.json()).csrf;
}

// 官方队列仍走 ComfyUI 接口；提交工作流时附带本项目登录校验。
api.fetchApi = async function (route, options = {}) {
  if (!csrf) await session();
  const headers = { ...Object.fromEntries(new Headers(options.headers)), "x-csrf-token": csrf };
  return originalFetch(route, { ...options, headers });
};

async function request(path, method = "GET", data) {
  if (!csrf) await session();
  const response = await fetch("/studio-api" + path, {
    method, headers: { "Content-Type": "application/json", "x-csrf-token": csrf },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  if (response.status === 401) { location.replace("/login"); throw new Error("请先登录"); }
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "操作失败");
  return result;
}

function element(tag, text, parent, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  if (parent) parent.append(item);
  return item;
}

function button(parent, text, handler) {
  const item = element("button", text, parent);
  item.type = "button";
  item.addEventListener("click", async () => {
    if (item.disabled || item.getAttribute("aria-busy") === "true") return;
    item.setAttribute("aria-busy", "true");
    try { await handler(); } catch (error) { alert(error.message || "操作失败"); }
    finally { item.removeAttribute("aria-busy"); }
  });
  return item;
}

async function workflow(stage, projectId) {
  if (app.graph._nodes?.length && !confirm("加载工作流将替换当前画布，是否继续？")) return false;
  const values = ["我的视频", "1", "请创作一段有连贯故事的视频", "", "", 30, "16:9", projectId];
  const project = {
    id: 1, type: "VolcProject", pos: [30, 30], size: [380, 380], flags: {}, order: 0, mode: 0,
    inputs: [], outputs: [{ name: "项目", type: "ANIMATION_PROJECT", links: [1], slot_index: 0 }],
    properties: {}, widgets_values: values,
  };
  const action = {
    id: 2, type: { script: "VolcScript", video: "VolcVideo", compose: "VolcCompose" }[stage],
    pos: [470, 30], size: [300, 140], flags: {}, order: 1, mode: 0,
    inputs: [{ name: "项目", type: "ANIMATION_PROJECT", link: 1 }],
    outputs: [{ name: "项目", type: "ANIMATION_PROJECT", links: null },
              { name: "结果", type: "STRING", links: null }],
    properties: {}, widgets_values: [],
  };
  await app.loadGraphData({
    last_node_id: 2, last_link_id: 1, nodes: [project, action],
    links: [[1, 1, 0, 2, 0, "ANIMATION_PROJECT"]], groups: [], config: {}, extra: {}, version: 0.4,
  });
  return true;
}

async function preview(parent, assetId) {
  const video = element("video", undefined, parent);
  video.controls = true;
  video.preload = "metadata";
  const renew = async () => {
    const access = await request(`/assets/${assetId}/access`);
    if (!access.url) throw new Error("素材仍在上传，请稍后刷新");
    const local = access.url.startsWith("/api/") ? "/studio-api/" + access.url.slice(5) : access.url;
    const position = video.currentTime;
    video.src = local;
    video.addEventListener("loadedmetadata", () => { video.currentTime = position; }, { once: true });
  };
  let renewed = false;
  video.addEventListener("error", () => {
    if (!renewed) { renewed = true; renew().catch(() => {}); }
  });
  await renew();
  button(parent, "续签预览地址", renew);
  button(parent, "下载视频", async () => {
    const access = await request(`/assets/${assetId}/access?download=true`);
    if (!access.url) throw new Error("素材仍在上传");
    const link = element("a");
    link.href = access.url.startsWith("/api/") ? "/studio-api/" + access.url.slice(5) : access.url;
    link.target = "_blank"; link.rel = "noopener";
    link.click();
  });
}

function step(parent, number, title, load) {
  const section = element("section", undefined, parent, "volc-studio-step");
  const header = element("div", undefined, section, "volc-studio-step-header");
  element("span", number, header, "volc-studio-index").setAttribute("aria-hidden", "true");
  element("h4", title, header);
  const state = element("span", "", header, "volc-studio-state");
  const hint = element("p", "", section, "volc-studio-hint");
  const content = element("div", undefined, section, "volc-studio-content");
  const action = load ? button(section, load.label, async () => {
    feedback.textContent = await load.run();
  }) : null;
  const feedback = element("p", "", section, "volc-studio-feedback");
  if (action) feedback.setAttribute("role", "status");
  return {
    content,
    show(status, reason, enabled = false) {
      state.textContent = status;
      hint.textContent = reason;
      feedback.textContent = "";
      section.setAttribute("data-ready", String(enabled));
      if (action) action.disabled = !enabled;
    },
  };
}

function stageProblem(stage) {
  if (stage.asset_id) return ["上传失败", "在本步骤重试上传，上传成功后刷新状态。"];
  if (stage.status === "failed" || stage.status === "uncertain") {
    return [jobStatuses[stage.status], "在下方「任务记录」核对并恢复原任务。"];
  }
  if (stage.status === "cancelled") return ["已取消", "如需重新制作，请创建新的创作轮次。"];
  return null;
}

function mediaResult(parent, assetId, ready, backup, refresh) {
  if (ready) {
    preview(parent, assetId).catch(error => element("p", error.message, parent, "volc-studio-hint"));
  } else if (backup?.status === "failed") {
    button(parent, "只重试 TOS 上传", async () => {
      await request(`/assets/${assetId}/backup/retry`, "POST", {});
      await refresh();
    });
  } else if (assetId) {
    element("p", "视频已生成，正在等待素材可用。", parent, "volc-studio-hint");
  }
}

function render(container) {
  container.replaceChildren();
  const panel = element("div", undefined, container, "volc-studio");
  element("h3", "火山视频工作室", panel);
  element("p", "选定项目后按顺序制作。每次画布运行结束后，回这里刷新状态。", panel, "volc-studio-intro");
  const toolbar = element("div", undefined, panel, "volc-studio-toolbar");
  button(toolbar, "刷新状态", () => refresh());
  const notice = element("p", "", panel, "volc-studio-notice");
  notice.setAttribute("role", "status");

  const load = (label, stage, newProject = false) => ({
    label,
    async run() {
      if (!await workflow(stage, newProject ? "" : currentProject)) return "";
      return newProject
        ? "已加载新项目和剧本节点。请填写项目节点，再去画布点「运行」。"
        : `已加载${jobNames[stage]}工作流。请去画布点「运行」。`;
    },
  });
  const steps = element("div", undefined, panel, "volc-studio-steps");
  const selection = step(steps, "1", "选择项目");
  const picker = element("select", undefined, selection.content);
  picker.setAttribute("aria-label", "选择项目");
  const newProject = button(selection.content, "加载新项目到画布", async () => {
    notice.textContent = await load("", "script", true).run();
  });
  element("p", "选已有项目会直接读取分镜。新项目需在画布填写项目节点并点「运行」，生成剧本后刷新再选择。", selection.content);

  const review = step(steps, "2", "审阅分镜", load("加载剧本到画布", "script"));
  const video = step(steps, "3", "生成视频", load("加载视频到画布", "video"));
  const compose = step(steps, "4", "拼接成片", load("加载拼接到画布", "compose"));
  const tasks = element("details", undefined, panel, "volc-studio-tasks");
  element("summary", "任务记录", tasks);
  const taskList = element("div", undefined, tasks);
  button(panel, "退出登录", async () => { await request("/logout", "POST", {}); location.replace("/login"); });

  let refreshVersion = 0;
  async function refresh() {
    const version = ++refreshVersion;
    picker.disabled = true;
    newProject.disabled = true;
    selection.show("读取中", "正在读取项目列表。");
    for (const item of [review, video, compose]) {
      item.show("读取中", "正在核对本步骤状态。");
      item.content.replaceChildren();
    }
    taskList.replaceChildren();
    notice.textContent = "";
    try {
      const projects = await request("/projects");
      if (version !== refreshVersion) return;
      const selected = projects.find(project => project.id === currentProject);
      if (!selected) currentProject = "";
      picker.replaceChildren();
      element("option", projects.length ? "请选择项目" : "暂无项目", picker).value = "";
      for (const project of projects) {
        element("option", `${project.name} · ${project.id.slice(0, 8)}`, picker).value = project.id;
      }
      picker.value = currentProject;
      if (!currentProject) {
        selection.show("待选择", projects.length ? "选择一个已有项目，或从新项目开始。" : "从新项目开始，先在画布生成剧本。");
        review.show("待选择", "选择项目后查看或生成分镜。");
        video.show("待分镜", "审阅分镜后才能加载视频工作流。");
        compose.show("待视频", "所有镜头视频可用后才能加载拼接工作流。");
        return;
      }
      const projectId = currentProject;
      const stage = kind => request(`/native/projects/${projectId}/stages/${kind}`);
      const [project, scriptState, videoState, composeState] = await Promise.all([
        request(`/projects/${projectId}`), stage("script"), stage("video"), stage("compose"),
      ]);
      if (version !== refreshVersion) return;
      const projectBusy = Boolean(selected.active_jobs) || project.jobs.some(job => activeJobs.has(job.status));
      selection.show("已选择", project.name);
      renderReview(project, scriptState, projectBusy);
      renderVideo(project, videoState, projectBusy);
      renderCompose(project, composeState, projectBusy);
      renderTasks(project);
    } catch (error) {
      if (version !== refreshVersion) return;
      notice.textContent = error.message || "读取失败，请刷新状态。";
      selection.show("读取失败", "请点击「刷新状态」重试。");
      for (const item of [review, video, compose]) item.show("待刷新", "刷新状态后再继续。");
    } finally {
      if (version === refreshVersion) {
        picker.disabled = picker.children.length < 2;
        newProject.disabled = false;
      }
    }
  }

  function renderReview(project, state, projectBusy) {
    if (!project.shots.length) {
      review.show(...(stageProblem(state) || (state.status === "missing" && !projectBusy
        ? ["可加载", "加载剧本工作流后，去画布点「运行」。", true]
        : ["等待中", "等项目任务完成后刷新状态。"])));
      return;
    }
    review.show("可审阅", projectBusy
      ? "可查看分镜。等项目任务结束后再修改。"
      : "审阅和保存都在侧栏完成。审阅完即可进入视频步骤。");
    if (project.script?.synopsis) element("p", project.script.synopsis, review.content);
    for (const [index, shot] of project.shots.entries()) {
      const row = element("div", undefined, review.content, "volc-studio-item");
      element("h5", `镜头 ${index + 1} · ${shot.spec.duration} 秒`, row);
      element("p", shot.spec.scene, row);
      element("p", `${shot.spec.action} · ${shot.spec.camera}`, row, "volc-studio-hint");
      const edit = element("details", undefined, row);
      element("summary", "编辑分镜", edit);
      const editor = element("textarea", undefined, edit);
      const original = JSON.stringify(shot.spec, null, 2);
      editor.value = original;
      editor.rows = 8;
      editor.readOnly = projectBusy;
      editor.setAttribute("aria-label", `镜头 ${index + 1} 的分镜内容`);
      if (shot.asset_id || project.jobs.some(job => job.kind === "video")) {
        element("p", "修改会使原视频失效。此轮次如已有视频任务，请用新轮次重新生成。", edit, "volc-studio-hint");
      }
      button(edit, "保存分镜", async () => {
        const spec = JSON.parse(editor.value);
        if (JSON.stringify(spec, null, 2) === original) return;
        await request(`/projects/${project.id}/shots/${shot.id}`, "PUT", spec);
        await refresh();
      }).disabled = projectBusy;
    }
  }

  function renderVideo(project, state, projectBusy) {
    const shots = project.shots;
    const ready = shots.filter(shot => shot.media_ready && shot.status === "succeeded" &&
      !shot.needs_regeneration && !shot.reference_stale).length;
    const problem = stageProblem(state);
    if (!shots.length) video.show("待分镜", "先生成剧本并审阅分镜。");
    else if (problem) video.show(...problem);
    else if (shots.some(shot => shot.reference_stale)) video.show("参考素材待确认", "请先确认失效的分镜参考素材。");
    else if (ready === shots.length) video.show("已完成", `全部 ${ready} 个镜头可用，可以进入拼接。`);
    else if (projectBusy) video.show("等待中", "项目任务正在处理或待核对。查看任务记录，完成后刷新。");
    else if (state.status === "missing") video.show("可加载", "审阅分镜后加载工作流，再去画布点「运行」。", true);
    else if (shots.some(shot => shot.needs_regeneration)) {
      video.show("需新轮次", "此轮次已有视频任务，且分镜或设置有更新。请用新轮次重新生成。");
    } else video.show("等待素材", "生成的素材仍在准备中。素材可用后刷新状态。");
    for (const [index, shot] of shots.entries()) {
      const row = element("div", undefined, video.content, "volc-studio-item");
      const status = shot.status === "succeeded" && shot.needs_regeneration ? "需重新生成" : jobStatuses[shot.status] || shot.status;
      element("h5", `镜头 ${index + 1} · ${status}`, row);
      mediaResult(row, shot.asset_id, shot.media_ready, shot.backup, refresh);
    }
  }

  function renderCompose(project, state, projectBusy) {
    const ready = project.shots.length && project.shots.every(shot => shot.asset_id &&
      shot.status === "succeeded" && shot.media_ready && !shot.needs_regeneration && !shot.reference_stale);
    const problem = stageProblem(state);
    if (problem) compose.show(...problem);
    else if (state.status === "succeeded" && project.export_ready) compose.show("已完成", "在此预览或下载最终成片。");
    else if (!ready) compose.show("待视频", "所有镜头生成成功且素材可用后，才能拼接。");
    else if (projectBusy) compose.show("等待中", "等项目任务结束后刷新状态。");
    else if (state.status === "missing") compose.show("可加载", "加载拼接工作流后，去画布点「运行」。", true);
    else if (project.export_asset) compose.show("等待素材", "成片已生成，素材可用后刷新状态。");
    else compose.show("需新轮次", "此轮次已有拼接记录。需要制作新成片时，请使用新轮次。");
    mediaResult(compose.content, project.export_asset, project.export_ready, project.export_backup, refresh);
  }

  function renderTasks(project) {
    if (!project.jobs.length) element("p", "此项目暂无任务。", taskList);
    for (const job of project.jobs) {
      const row = element("div", undefined, taskList, "volc-studio-item");
      element("p", `${jobNames[job.kind] || job.kind} · ${jobStatuses[job.status] || job.status}`, row);
      element("p", job.external_id || job.id, row, "volc-studio-hint");
      if (job.error) element("p", job.error, row, "volc-studio-hint");
      if (job.status === "failed" || job.status === "uncertain") {
        button(row, "核对并恢复任务", async () => {
          const externalId = prompt("填写已受理的远程任务 ID。留空沿用已有 ID。");
          if (externalId === null) return;
          const body = externalId.trim() ? { external_id: externalId.trim() } : {};
          if ((job.status === "uncertain" && !job.external_id && !body.external_id) || job.phase === "rejected") {
            if (!confirm("重新提交可能重复计费。确认已核对原任务并同意重提？")) return;
            body.acknowledge_duplicate_cost = true;
          }
          await request(`/jobs/${job.id}/recover`, "POST", body);
          await refresh();
        });
      }
    }
  }

  picker.addEventListener("change", () => { currentProject = picker.value; refresh(); });
  refresh();
}

app.registerExtension({
  name: "Volcengine.AnimationStudio",
  async setup() {
    await session();
    const stylesheet = element("link", undefined, document.head);
    stylesheet.rel = "stylesheet";
    stylesheet.href = new URL("./studio.css", import.meta.url).href;
    app.extensionManager.registerSidebarTab({
      id: "volcengine-studio", icon: "pi pi-video", title: "视频工作室",
      tooltip: "火山视频工作室", type: "custom", render,
    });
  },
});
