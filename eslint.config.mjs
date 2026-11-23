// ESLint 扁平配置（T1.14）：只覆盖 Node 测试脚本与前端非压缩 JS。
// 部分测试用 vm 注入前端全局函数（S/el/renderGraph 等），无法静态穷举 globals，
// 因此 tests/ 内 no-undef 降为 warn（仍能发现明显笔误，但不阻塞历史测试）；
// web/assets 交给运行时测试与 Playwright 验收。
export default [
  {
    ignores: ["**/*.min.js", "web/assets/vendor/**", "node_modules/**", "output/**", "_archive/**"],
  },
  {
    files: ["tests/**/*.cjs"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "commonjs",
      globals: {
        require: "readonly",
        module: "readonly",
        exports: "readonly",
        process: "readonly",
        console: "readonly",
        Buffer: "readonly",
        __dirname: "readonly",
        __filename: "readonly",
        URL: "readonly",
        URLSearchParams: "readonly",
        fetch: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        setInterval: "readonly",
        clearInterval: "readonly",
        setImmediate: "readonly",
      },
    },
    rules: {
      "no-undef": "warn",
      "no-redeclare": "error",
      "no-unused-vars": ["warn", { "argsIgnorePattern": "^_" }],
    },
  },
];
