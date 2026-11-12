import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

let sessionPromise;

function session() {
  sessionPromise ??= fetch("/studio-api/session").then(async (response) => {
    if (response.status === 401) {
      location.replace("/login");
      throw new Error("请先登录");
    }
    if (!response.ok) throw new Error("获取登录状态失败，请重新登录");
    return (await response.json()).csrf;
  }).catch((error) => {
    sessionPromise = undefined;
    throw error;
  });
  return sessionPromise;
}

const originalFetch = api.fetchApi.bind(api);
api.fetchApi = async function (route, options = {}) {
  const headers = new Headers(options.headers);
  headers.set("x-csrf-token", await session());
  const response = await originalFetch(route, { ...options, headers: Object.fromEntries(headers) });
  if (response.status === 401) {
    sessionPromise = undefined;
    location.replace("/login");
  }
  return response;
};

app.registerExtension({
  name: "Volcengine.ArkAuthentication",
  async setup() {
    await session();
  },
});
