import os
from sanic import Sanic 
from sanic.response import html
from datastar_py import ServerSentEventGenerator as SSE 
from datastar_py.sanic import datastar_response 

app = Sanic("ConductionApp")

app.static("/static", "./static")

def render_template(filename: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "templates", filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/")
async def home(request):
        """Serves the main app"""
        return html(render_template("index.html"))

@app.get("/api/ping")
@datastar_response
async def ping(request):
     """grab sse for datastar"""
     fragment = '<div>Hello! Welcome to the D A T A S T A R </div>'
     yield SSE.patch_elements(fragment)

if __name__ == "__main__":
     app.run(host="0.0.0.0", port=8000, dev=True)