# 前端 vendor 依赖（pin + 校验）

本目录只存放浏览器直接加载的第三方 UMD 构建，不参与打包，无构建链。
所有文件均从 npm registry 官方 tarball 解出，版本与 sha256 固定如下。

| 文件 | 版本 | 来源 | License |
| --- | --- | --- | --- |
| `markdown-it.umd.min.js` | 15.0.3 | https://registry.npmjs.org/markdown-it/-/markdown-it-15.0.3.tgz → `package/dist/browser/markdown-it.umd.min.js` | MIT（见 `LICENSE-markdown-it.txt`） |
| `purify.min.js` | 3.4.17 | https://registry.npmjs.org/dompurify/-/dompurify-3.4.17.tgz → `package/dist/purify.min.js` | MPL-2.0 OR Apache-2.0（见 `LICENSE-dompurify.txt`、`LICENSE-dompurify-MPL.txt`） |

## sha256

```
391f2aa638e75e6c7859d0a5c5650ef0956722a45998f17e05b83bc655778e1e  markdown-it.umd.min.js
cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31  purify.min.js
```

## 用途与升级规则

- `web/index.html` 在主脚本之前以 `<script src="assets/vendor/...">` 同步加载两者。
- `mdToHtml()` 用 markdown-it 渲染模型回答（`html:false`，原文 HTML 不生效），
  再用 DOMPurify 以 `ALLOWED_TAGS`/`ALLOWED_ATTR` 白名单清洗；任一 bundle 缺失时
  退回纯文本/内置硬化清洗，不阻塞对话。
- 升级必须同时更新上表版本、sha256 与对应 LICENSE 文件，并重新跑
  `node --test tests/xss-guard.test.cjs`（该测试会现场校验 sha256）。
- 只允许 MIT / Apache-2.0 / MPL-2.0 / BSD 等宽松许可；禁止 GPL/AGPL 依赖。
