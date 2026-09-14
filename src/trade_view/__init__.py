def main() -> None:
    import uvicorn

    uvicorn.run("trade_view.main:app", host="127.0.0.1", port=8000, reload=True)
