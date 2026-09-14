Trade View
==========

A FastAPI trading dashboard shell built with server-rendered Jinja templates, HTMX partial swaps, and DaisyUI components.

Configure Tradier:

```bash
cp .env.example .env
```

Then fill in `TRADIER_ACCOUNT_ID` and `TRADIER_API_TOKEN` in `.env`.

Run locally:

```bash
npm install
npm run build:css
uv run fastapi dev src/trade_view/main.py
```

Or use the package entry point:

```bash
uv run trade-view
```

During UI work, run the Tailwind/DaisyUI watcher in a separate terminal:

```bash
npm run dev:css
```
