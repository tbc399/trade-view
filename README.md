Trade View
==========

A FastAPI trading dashboard shell built with server-rendered Jinja templates, HTMX partial swaps, and DaisyUI components.

Configure Tradier:

```bash
cp .env.example .env
```

Then fill in `TRADIER_ACCOUNT_ID` and `TRADIER_API_TOKEN` in `.env`.

Configure login:

```bash
uv run python hash_password.py "your password"
```

Set `USERNAME` to your login name and `PASSWORD` to the generated hash. Login sessions are signed JWTs stored in an HTTP-only cookie and expire after 48 hours.

The password hash includes a random salt, so the same text password produces a different hash each time. Any generated hash for that password is valid; the login check uses the salt embedded in the stored hash.

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
