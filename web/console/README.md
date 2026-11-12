# Console

The Engineering Control console: React on the gntik-ui design system, built by Vite into
`src/engineering_control/dashboard/console/`, which the dashboard service serves. The built
bundle is committed; CI fails when it does not match the source.

```bash
npm ci
npm run build
```

## gntik-ui

| Package | Version | Used for |
| --- | --- | --- |
| `@gntik-ai/ui` | 0.13.0 | every component and layout |
| `@gntik-ai/tokens` | 0.4.0 | colours, type and the page baseline (`base.css`); the version `ui` requires |
| `@gntik-ai/icons` | 0.5.0 | icons |

`gntik-ui.json` is the configuration of the kit's command line (`npx @gntik-ai/cli`), which
adds components and copies blocks or page templates into `src/components/`.

Rules the console keeps:

- **One root.** `KitProvider` in `src/shell.tsx` supplies the theme, links, tooltips, toasts
  and announcements to both pages (console and sign-in).
- **Kit components before own code.** Search is `SearchField`, counters are `showCount`,
  the browser tab is named with `useDocumentTitle`. Before writing a part, look for it with
  `npx @gntik-ai/cli search <words>`.
- **Blocks are copied in, not imported.** `@gntik-ai/blocks` is published as one file that
  imports the code editor, the flow canvas and the charts. Importing a single block from the
  package adds the editor's workers (about 9 MB of files) and a quarter more script to the
  build, measured at 0.13.0. `npx @gntik-ai/cli add <block>` copies only that block's source,
  which depends on `@gntik-ai/ui` alone.
- **Strict content security policy.** Scripts, styles, fonts and images come from the
  console's own address; the theme script is a file, never inline. The fonts are the Latin
  files named in `src/index.css`, which is what the sign-in page is allowed to load.

## Upgrading gntik-ui

1. `npm view @gntik-ai/ui version`, and the `@gntik-ai/tokens` version that release depends
   on (`npm view @gntik-ai/ui dependencies`); install both exactly, with the icons.
2. Read the release notes in `packages/ui/CHANGELOG.md` of the gntik-ui repository, and run
   the kit's own migration check: `npx @gntik-ai/cli upgrade --from <old version>` (it needs
   `@gntik-ai/codemods` installed; a dry run unless `--apply`).
3. `npm run build`, then look at every page in a browser, dialogs included.

From 0.11.0 to 0.13.0 no migration applied. Taken from those releases: `KitProvider`,
`SearchField`, character counters, document titles and the page baseline.

Blocks worth copying in when their page is next worked on: `error-panel` (load errors with
a retry), `confirm-destructive` (removal dialogs), `page-header` (breadcrumbs on the project
and execution pages) and `data-table` (the backlog, with row selection). The shell blocks
(`app-sidebar`, `app-topbar`) are made for products with workspaces and user accounts; the
console has one administrator and uses the layout they are built on directly. Not needed
yet: `@gntik-ai/charts` (usage and budgets), `@gntik-ai/flow` and `@gntik-ai/editor` (the
flow designer), `@gntik-ai/chat` (talking to the supervisor in the console).
