import reactHooks from 'eslint-plugin-react-hooks';
import tsParser from '@typescript-eslint/parser';
import tsPlugin from '@typescript-eslint/eslint-plugin';
import importPlugin from 'eslint-plugin-import-x';

export default [
  {
    ignores: ['coverage/**', 'dist/**', 'node_modules/**'],
  },
  {
    files: ['**/*.{js,mjs,cjs}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
    },
  },
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaFeatures: { jsx: true },
        ecmaVersion: 'latest',
        sourceType: 'module',
      },
    },
    plugins: {
      '@typescript-eslint': tsPlugin,
      'react-hooks': reactHooks,
      'import-x': importPlugin,
    },
    settings: {
      'import-x/resolver': {
        typescript: true,
      },
    },
    rules: {
      // TypeScript handles undefined names more accurately for this codebase.
      'no-undef': 'off',
      'react-hooks/exhaustive-deps': 'warn',
      'react-hooks/rules-of-hooks': 'error',
      // Discourage `any` — warn for now to allow incremental cleanup.
      '@typescript-eslint/no-explicit-any': 'warn',
      // Enforce type-only imports for better tree-shaking and bundle size.
      '@typescript-eslint/consistent-type-imports': [
        'warn',
        { prefer: 'type-imports', fixStyle: 'inline-type-imports' },
      ],
      // Catch unused variables (underscore-prefixed names are allowed).
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      // Prefer const over let when variable is never reassigned.
      'prefer-const': 'warn',
      // Forbid console.log in production code (use a logger instead).
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      // Import ordering — group by external, internal, relative.
      'import-x/order': [
        'warn',
        {
          groups: ['builtin', 'external', 'internal', 'parent', 'sibling', 'index'],
          pathGroups: [
            { pattern: '@/**', group: 'internal', position: 'before' },
          ],
          'newlines-between': 'never',
        },
      ],
      // Detect circular dependencies.
      'import-x/no-cycle': ['warn', { maxDepth: 3 }],
      // Ensure all imports resolve.
      'import-x/no-unresolved': 'off', // TypeScript handles this.
    },
  },
];
