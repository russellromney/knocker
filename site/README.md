# knocker.dev

Astro Starlight site for Knocker. Docs-first, deployed to Cloudflare Pages.

## Develop

```bash
cd site
npm install
npm run dev
```

## Build

```bash
npm run build
npm run preview
```

## Deploy

One-time setup:

```bash
npx wrangler login
npx wrangler pages project create knocker --production-branch=main
```

Then point `knocker.dev` at the Cloudflare Pages project in the dashboard.

Regular deploys:

```bash
npm run deploy
npm run deploy:preview
```
