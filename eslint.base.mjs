// Shared ESLint flat config for every TypeScript workspace package (specs/02 §4).
// Usage in a package `eslint.config.mjs`:
//   export default base({ tsconfigRootDir: import.meta.dirname, react: true });
import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

const IGNORES = [
  "**/dist/**",
  "**/.next/**",
  "**/coverage/**",
  "**/node_modules/**",
  "**/schema.d.ts",
  "**/next-env.d.ts",
];

/**
 * @param {{ tsconfigRootDir: string, react?: boolean }} options
 */
export function base({ tsconfigRootDir, react = false }) {
  const reactConfigs = react
    ? [reactHooks.configs.flat["recommended-latest"], jsxA11y.flatConfigs.recommended]
    : [];
  return tseslint.config(
    { ignores: IGNORES },
    js.configs.recommended,
    ...tseslint.configs.strictTypeChecked,
    ...tseslint.configs.stylisticTypeChecked,
    {
      languageOptions: {
        globals: { ...globals.browser, ...globals.node },
        parserOptions: { projectService: true, tsconfigRootDir },
      },
      rules: {
        "@typescript-eslint/restrict-template-expressions": ["error", { allowNumber: true }],
        "@typescript-eslint/consistent-type-imports": "error",
      },
    },
    {
      files: ["**/*.mjs", "**/*.cjs", "**/*.js"],
      ...tseslint.configs.disableTypeChecked,
    },
    ...reactConfigs,
    prettier,
  );
}
