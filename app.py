import os
from flask import Flask

app = Flask(__name__)

@app.route("/")
def hello_world():
    return "<p>Hello, World!</p>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))  # Важно: используем PORT из переменных окружения
    app.run(host="0.0.0.0", port=port)         # Важно: слушаем на 0.0.0.0
