"""A small application on top of kendi_sunucu: a JSON notes API, a cookie-based visit counter, a timing middleware and static files.

    python examples/demo.py            # then try:  curl -i localhost:8000/api/notes
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kendi_sunucu import App, HTTPError, Server, StaticFiles, json_response  # noqa: E402

PUBLIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")


def build_app():
    app = App()
    notes, lock = {}, threading.Lock()
    counter = {"next": 1}

    @app.use
    def timing(request, call_next):
        started = time.perf_counter()
        response = call_next(request)
        response.headers.set("Server-Timing", "app;dur=%.2f" % ((time.perf_counter() - started) * 1000))
        return response

    @app.get("/api/notes")
    def list_notes(request):
        with lock:
            return {"notes": [dict(id=i, **n) for i, n in sorted(notes.items())]}

    @app.post("/api/notes")
    def create_note(request):
        data = request.json()
        if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not data["text"].strip():
            raise HTTPError(422, "a note needs a non-empty 'text'")
        with lock:
            note_id = counter["next"]
            counter["next"] += 1
            notes[note_id] = {"text": data["text"].strip()}
        return json_response({"id": note_id, **notes[note_id]}, 201, [("Location", "/api/notes/%d" % note_id)])

    @app.get("/api/notes/<int:id>")
    def get_note(request, id):
        with lock:
            if id not in notes:
                raise HTTPError(404, "no such note")
            return {"id": id, **notes[id]}

    @app.delete("/api/notes/<int:id>")
    def delete_note(request, id):
        with lock:
            if notes.pop(id, None) is None:
                raise HTTPError(404, "no such note")
        return ("", 204)

    @app.get("/api/visits")
    def visits(request):
        cookie = request.cookies.get("visits", "")
        seen = int(cookie) if cookie.isdigit() else 0
        return json_response({"visits": seen + 1}, headers=[("Set-Cookie", "visits=%d; Max-Age=3600; HttpOnly; SameSite=Lax" % (seen + 1))])

    app.mount("", StaticFiles(PUBLIC, cache_control="max-age=60"))
    return app


if __name__ == "__main__":
    server = Server(build_app(), "127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8000, access_log=True).start()
    print("demo on", server.url, "- Ctrl+C to stop", file=sys.stderr)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown(5.0)
