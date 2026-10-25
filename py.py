import os
import re
import sqlite3
from pathlib import Path

import click
from flask import Flask, g, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

from app import app as voice_app, load_secret_key


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    DATABASE=Path(
        os.environ.get("DATABASE_PATH", Path(__file__).with_name("users.db"))
    ),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower()
    in {"1", "true", "yes"},
    MAX_CONTENT_LENGTH=16 * 1024,
)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


def init_db():
    database = Path(app.config["DATABASE"])
    database.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(database)
    try:
        with db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL
                )
                """
            )
    finally:
        db.close()


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.before_request
def ensure_database():
    if not app.config.get("_DATABASE_INITIALIZED"):
        init_db()
        app.config["_DATABASE_INITIALIZED"] = True


@app.get("/")
def login_page():
    if "user_id" in session:
        return voice_app.view_functions["home"]()
    return send_from_directory(app.root_path, "login.html")


@app.get("/register.html")
def register_page():
    return send_from_directory(app.root_path, "register.html")


@app.get("/home.html")
def home_page():
    if "user_id" not in session:
        return redirect("/")
    return voice_app.view_functions["home"]()


@app.post("/api/register")
def register():
    if not request.is_json:
        return jsonify(error="A JSON request body is required."), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="A valid JSON object is required."), 400

    email = data.get("email")
    password = data.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        return jsonify(error="Enter a valid email address and password."), 400

    email = email.strip().lower()
    if (
        len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
    ):
        return jsonify(error="Enter a valid email address."), 400
    if len(password) < 12:
        return jsonify(error="Password must be at least 12 characters."), 400
    if len(password) > 1024:
        return jsonify(error="Password must be no more than 1024 characters."), 400

    try:
        cursor = get_db().execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            (email, generate_password_hash(password)),
        )
        get_db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="An account with that email already exists."), 409

    session.clear()
    session["user_id"] = cursor.lastrowid
    session.permanent = True
    return jsonify(message="Account created successfully."), 201


@app.post("/api/login")
def login():
    if not request.is_json:
        return jsonify(error="A JSON request body is required."), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="A valid JSON object is required."), 400

    email = data.get("email")
    password = data.get("password")
    remember = data.get("remember", False)

    if (
        not isinstance(email, str)
        or not isinstance(password, str)
        or not isinstance(remember, bool)
        or not email.strip()
        or len(email.strip()) > 254
        or len(password) > 1024
    ):
        return jsonify(error="Enter a valid email address and password."), 400

    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(error="Enter a valid email address and password."), 400

    user = get_db().execute(
        "SELECT id, password_hash FROM users WHERE email = ?",
        (email,),
    ).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        return jsonify(error="Login failed. Check your email and password."), 401

    session.clear()
    session["user_id"] = user["id"]
    session.permanent = remember
    return jsonify(message="Login successful.")


@app.cli.command("create-user")
@click.argument("email")
def create_user(email):
    """Create a user account for the login form."""
    email = email.strip().lower()
    if (
        len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
    ):
        raise click.ClickException("Enter a valid email address.")

    password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    if len(password) < 12:
        raise click.ClickException("Password must be at least 12 characters.")

    init_db()
    db = sqlite3.connect(app.config["DATABASE"])
    try:
        with db:
            db.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                (email, generate_password_hash(password)),
            )
    except sqlite3.IntegrityError as error:
        raise click.ClickException("An account with that email already exists.") from error
    finally:
        db.close()

    click.echo(f"Created account for {email}.")


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
    )
