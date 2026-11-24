# Local document preview libraries

These files are served by the workspace. Previewing a document requires no CDN access at runtime.

| File | Upstream | License |
| --- | --- | --- |
| `markdown-it-15.0.3.mjs` | npm `markdown-it@15.0.3`, `dist/browser/markdown-it.esm.min.mjs` with the source map reference removed | `markdown-it-LICENSE.txt` |
| `xlsx-0.20.3.js` | [SheetJS CE 0.20.3 release](https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz), `dist/xlsx.mini.min.js` | `sheetjs-LICENSE.txt` |
| `pptx-browser-4.1.5.mjs` | npm `pptx-browser@4.1.5`, built for the browser | `pptx-browser-LICENSE.txt` |

To rebuild the PowerPoint bundle, unpack the npm release and apply the following changes before bundling.

- In `src/fonts.js`, replace the body of `loadGoogleFontsFor` with an empty body and change its font resolution fallback from `return mapped.google || name;` to `return name;`.
- In `src/render.js`, move the `defRPr` declaration before the call to `parseBullet`. Assign `run.color || '#000000'` directly to `ctx.fillStyle` because the inherited text color is already a CSS string.

The bundled entry is `export {default as PptxRenderer} from './package/src/index.js';`, alongside the unpacked `package` directory. Build it with `npx --yes esbuild@0.25.12 reader-entry.mjs --bundle --format=esm --platform=browser --minify --legal-comments=inline --outfile=pptx-browser-4.1.5.mjs`.
