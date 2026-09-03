# API 环境配置

Vue 前端的请求统一经过 `src/api.js` 的 `apiUrl()`，组件不需要为开发和生产分别改接口路径。

## 开发环境

`npm run dev` 使用 `.env.development`：浏览器请求 `/api/...`，Vite 会把 API、全局静态资源和登录页
代理到 `VITE_DEV_API_PROXY_TARGET`（默认 `http://127.0.0.1:8787`）。本地后端换端口时只需修改这个变量。
开发构建使用 `npm run build:dev`。

## 生产环境

`npm run build` 或 `npm run build:prod` 使用 `.env.production`。将 `VITE_API_BASE_URL` 改成生产 API 地址，例如：

```text
VITE_API_BASE_URL=https://api.example.com
```

如果前端和 API 通过同一个域名或反向代理提供，则保持 `VITE_API_BASE_URL=/api`，不需要跨域配置。
登录页、兑换页和旧版面板使用 `web/static/api-config.js`；同源部署保持默认 `/api`，独立域名部署时
在该文件顶部通过 `window.__GPT_REGISTER_API_BASE_URL__` 覆盖为生产 API 地址。独立域名还需要 API
服务允许前端来源的 CORS，并允许携带会话凭据；Vue 和旧版客户端都使用 `credentials: "include"`。

环境变量会在构建时写入浏览器代码，只放公开地址，不要放密钥。
