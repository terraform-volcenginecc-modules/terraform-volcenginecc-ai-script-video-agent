const form = document.querySelector("#登录");
const message = document.querySelector("#提示");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  message.textContent = "正在登录…";
  try {
    const response = await fetch("/studio-api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: document.querySelector("#密码").value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "登录失败");
    location.replace("/");
  } catch (error) {
    message.textContent = error.message || "连接失败，请稍后重试";
    button.disabled = false;
  }
});
