def main() -> None:
    import uvicorn

    uvicorn.run("trade_view.main:app", host="127.0.0.1", port=8000, reload=True)


def serve() -> None:
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("trade_view.main:app", host="0.0.0.0", port=port)
