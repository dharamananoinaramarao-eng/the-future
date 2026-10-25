import asyncio
import re
import socket
import sqlite3
import threading
from importlib import import_module
from io import BytesIO
import os
from pathlib import Path
import secrets
import shutil
import uuid

REQUIRED_MODULES = [
    "flask",
    "edge_tts",
    "PIL",
    "gtts",
    "langdetect",
    "pytesseract",
]

missing_modules = []
for module_name in REQUIRED_MODULES:
    try:
        import_module(module_name)
    except ModuleNotFoundError:
        missing_modules.append(module_name)

if missing_modules:
    raise SystemExit(
        "Missing required Python modules: "
        + ", ".join(missing_modules)
        + ". Install them with: python -m pip install -r requirements.txt"
    )

import edge_tts
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps
from gtts.lang import tts_langs
from langdetect import DetectorFactory, LangDetectException, detect
import pytesseract

BASE_DIR = Path(__file__).resolve().parent


def load_secret_key():
    """Load the configured key or create a persistent user-local key."""
    configured_key = os.environ.get("FLASK_SECRET_KEY")
    if configured_key:
        if len(configured_key) < 32:
            raise RuntimeError("FLASK_SECRET_KEY must be at least 32 characters.")
        return configured_key

    config_root = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_CONFIG_HOME")
        or str(Path.home() / ".config")
    )
    key_directory = Path(config_root) / "VoiceGenerator"
    key_directory.mkdir(parents=True, exist_ok=True)
    key_path = key_directory / "secret_key"

    try:
        with key_path.open("x", encoding="utf-8") as key_file:
            key_file.write(secrets.token_hex(32))
    except FileExistsError:
        pass

    secret_key = key_path.read_text(encoding="utf-8").strip()
    if len(secret_key) < 32:
        raise RuntimeError(f"The Flask secret key file is invalid: {key_path}")
    return secret_key


app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config.update(
    SECRET_KEY=load_secret_key(),
    DATABASE=Path(os.environ.get("DATABASE_PATH", BASE_DIR / "users.db")),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower()
    in {"1", "true", "yes"},
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)

AUDIO_DIR = BASE_DIR / "static" / "audio"
UPLOAD_DIR = BASE_DIR / "uploads"

AUDIO_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

TESSERACT_CMD = (
    os.environ.get("TESSERACT_CMD")
    or shutil.which("tesseract")
    or str(Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"))
)
USER_TESSDATA_DIR = (
    Path(os.environ["LOCALAPPDATA"]) / "Tesseract-OCR" / "tessdata"
    if os.environ.get("LOCALAPPDATA")
    else None
)
if (
    not os.environ.get("TESSDATA_PREFIX")
    and USER_TESSDATA_DIR is not None
    and USER_TESSDATA_DIR.is_dir()
):
    os.environ["TESSDATA_PREFIX"] = str(USER_TESSDATA_DIR)

if Path(TESSERACT_CMD).is_file():
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

# gTTS lists 69 voices; four regional/alternate entries are not supported as
# Google Translate targets, so only the shared language codes are offered.
UNTRANSLATABLE_VOICE_CODES = {"fr-CA", "pt-PT", "yue", "zh"}
LANGUAGES = {
    code: name
    for code, name in sorted(tts_langs().items(), key=lambda item: item[1].casefold())
    if code not in UNTRANSLATABLE_VOICE_CODES
}
DetectorFactory.seed = 0
OCR_LANGUAGE_NAMES = {
    "eng": "English",
    "tel": "Telugu",
    "hin": "Hindi",
    "tam": "Tamil",
    "kan": "Kannada",
    "chi_sim": "Chinese (Simplified)",
    "chi_tra": "Chinese (Traditional)",
    "osd": "Orientation / script detection",
}
EDGE_LANGUAGE_ALIASES = {
    "iw": "he",
    "jw": "jv",
    "no": "nb",
    "zh-TW": "zh",
}
EDGE_VOICE_CACHE = None
SMOOTH_VOICE_PERSONALITIES = {
    "approachable",
    "caring",
    "clear",
    "comfort",
    "conversational",
    "friendly",
    "pleasant",
    "sincere",
    "warm",
}

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp"}


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


class OCRLanguageUnavailableError(Exception):
    """Raised when the selected OCR language data is not installed."""


class TranslationRateLimitError(Exception):
    """Raised when translation providers cannot complete a request."""


def detect_language(text):
    """Detect the source language using its ISO code."""
    try:
        return detect(text)
    except LangDetectException:
        return "unknown"


def get_installed_ocr_languages():
    """Return all installed Tesseract OCR language packs."""
    try:
        installed = pytesseract.get_languages(config="")
    except pytesseract.TesseractNotFoundError:
        return {}

    return {
        code: OCR_LANGUAGE_NAMES.get(code, code.replace("_", " ").title())
        for code in sorted(installed)
        if code != "osd"
    }


def get_mymemory_language_name(language_code):
    """Map a language code to a supported MyMemory language name."""
    from deep_translator.constants import MY_MEMORY_LANGUAGES_TO_CODES

    aliases = {
        "no": "norwegian bokmål",
    }
    if language_code in aliases:
        return aliases[language_code]

    supported_languages = {
        name.casefold(): code
        for name, code in MY_MEMORY_LANGUAGES_TO_CODES.items()
    }
    display_name = LANGUAGES.get(language_code, "").casefold()
    if display_name:
        name_candidates = (
            display_name,
            display_name.split(" (", 1)[0],
            display_name.split(",", 1)[0],
        )
        for name in name_candidates:
            if name in supported_languages:
                return name

    normalized_code = language_code.casefold().replace("_", "-")
    if normalized_code in {
        code.casefold() for code in supported_languages.values()
    }:
        for name, code in supported_languages.items():
            if code.casefold() == normalized_code:
                return name

    base_code = normalized_code.split("-", 1)[0]
    for name, code in supported_languages.items():
        if code.casefold().split("-", 1)[0] == base_code:
            return name
    return None


def translate_text(text, source_language, target_language):
    """Translate text into the selected output language."""
    if source_language == target_language:
        return text, False

    try:
        translator_module = import_module("deep_translator")
    except ModuleNotFoundError as exc:
        if exc.name != "deep_translator":
            raise
        raise RuntimeError(
            "Translation support is unavailable. Install it with "
            "'python -m pip install deep-translator'."
        ) from exc

    from deep_translator.exceptions import TooManyRequests

    try:
        translated = translator_module.GoogleTranslator(
            source="auto",
            target=target_language
        ).translate(text)
        return translated, False
    except TooManyRequests:
        source_name = get_mymemory_language_name(source_language)
        target_name = get_mymemory_language_name(target_language)

        if not source_name or not target_name:
            raise TranslationRateLimitError(
                "The primary translator is rate-limited, and the free "
                "backup cannot identify the input language. Select the "
                "input language manually and try again."
            )

        try:
            backup_translation = translator_module.MyMemoryTranslator(
                source=source_name,
                target=target_name
            )
            translated_chunks = []
            words = text.split()
            chunk_words = []
            chunk_length = 0

            for word in words:
                if len(word) > 500:
                    raise ValueError(
                        "A single word exceeds the backup translation limit."
                    )
                next_length = chunk_length + len(word) + bool(chunk_words)
                if next_length > 500:
                    translated_chunks.append(
                        backup_translation.translate(" ".join(chunk_words))
                    )
                    chunk_words = []
                    chunk_length = 0
                chunk_words.append(word)
                chunk_length += len(word) + bool(chunk_words[:-1])

            if chunk_words:
                translated_chunks.append(
                    backup_translation.translate(" ".join(chunk_words))
                )

            translated_text = " ".join(translated_chunks)
            if not translated_text:
                raise ValueError("The backup translation returned no text.")
            return translated_text, True
        except Exception as exc:
            app.logger.exception("Backup translation failed.")
            raise TranslationRateLimitError(
                "The primary translator is rate-limited, and the free "
                "backup translation failed. Wait a minute and try again."
            ) from exc


def get_server_port(default_port=5000, port_range=100):
    """Choose the first available local port in a safe fallback range."""
    env_port = os.environ.get("PORT")
    candidate_ports = []

    if env_port is not None:
        try:
            env_port = int(env_port)
        except ValueError:
            env_port = None
        if env_port is not None and env_port > 0:
            candidate_ports.append(env_port)

    candidate_ports.extend(range(default_port, default_port + port_range))

    for port in candidate_ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue

    return default_port


def run_async(coro):
    """Run an async coroutine even when the current thread already has a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result = []
    error = []

    def runner():
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # pragma: no cover - exercised by tests
            error.append(exc)

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()

    if error:
        raise error[0]
    return result[0]


def get_edge_voices(language):
    """Return available Edge TTS voices for a supported language."""
    global EDGE_VOICE_CACHE
    if EDGE_VOICE_CACHE is None:
        EDGE_VOICE_CACHE = run_async(edge_tts.list_voices())

    edge_language = (
        EDGE_LANGUAGE_ALIASES.get(language, language)
        .split("-", 1)[0]
        .casefold()
    )
    voices = []
    for voice in EDGE_VOICE_CACHE:
        locale = str(voice.get("Locale", ""))
        if locale.split("-", 1)[0].casefold() != edge_language:
            continue

        voice_tag = voice.get("VoiceTag") or {}
        personalities = voice_tag.get("VoicePersonalities") or []
        smooth = bool(
            SMOOTH_VOICE_PERSONALITIES
            & {
                str(personality).casefold()
                for personality in personalities
                if isinstance(personality, str)
            }
        )
        short_name = str(voice.get("ShortName", ""))
        gender = str(voice.get("Gender", ""))
        voice_name = short_name.rsplit("-", 1)[-1]
        if voice_name.endswith("Neural"):
            voice_name = voice_name[:-len("Neural")]

        voices.append(
            {
                "name": short_name,
                "locale": locale,
                "gender": gender,
                "smooth": smooth,
                "label": f"{locale} - {voice_name} ({gender})",
            }
        )
    voices.sort(key=lambda voice: (not voice["smooth"], voice["name"]))
    return voices


def generate_voice(text, voice, voice_style="natural"):
    filename = f"{uuid.uuid4().hex}.mp3"
    output_path = AUDIO_DIR / filename

    async def save_speech():
        speech = create_speech(text, voice, voice_style)
        await speech.save(str(output_path))

    run_async(save_speech())
    return filename


def create_speech(text, voice, voice_style="natural"):
    """Create an Edge TTS stream using the selected voice style."""
    if voice_style == "robot":
        return edge_tts.Communicate(
            text,
            voice=voice,
            rate="+10%",
            pitch="+20Hz",
        )
    return edge_tts.Communicate(text, voice=voice, rate="-5%")


def generate_voice_preview(voice, voice_style, language):
    """Generate a short in-memory audio sample for a selected voice."""
    preview_texts = {
        "en": "Hello, this is a sample of the selected voice.",
        "te": "నమస్కారం, ఇది ఎంచుకున్న స్వరం యొక్క నమూనా.",
        "hi": "नमस्कार, यह चुनी हुई आवाज़ का नमूना है।",
        "ta": "வணக்கம், இது தேர்ந்தெடுக்கப்பட்ட குரலின் மாதிரி.",
        "kn": "ನಮಸ್ಕಾರ, ಇದು ಆಯ್ಕೆ ಮಾಡಿದ ಧ್ವನಿಯ ಮಾದರಿ.",
    }

    async def generate():
        stream = create_speech(
            preview_texts.get(
                language,
                "Hello, this is a sample of the selected voice.",
            ),
            voice,
            voice_style,
        )
        audio = BytesIO()
        async for chunk in stream.stream():
            if chunk["type"] == "audio":
                audio.write(chunk["data"])
        return audio.getvalue()

    return run_async(generate())


def read_image(image_path, ocr_choice):
    """Extract text using the selected or all installed OCR languages."""
    with Image.open(image_path) as original:
        image = ImageOps.exif_transpose(original)
        image = ImageOps.grayscale(image)
        available_languages = set(get_installed_ocr_languages())

        if ocr_choice == "auto":
            installed_languages = sorted(available_languages)
            if not installed_languages:
                raise OCRLanguageUnavailableError(
                    "No Tesseract OCR language packs are installed. "
                    "Install a Tesseract language pack, then try again."
                )
            ocr_lang = "+".join(installed_languages)
        else:
            if ocr_choice not in available_languages:
                raise OCRLanguageUnavailableError(
                    f"The Tesseract OCR language pack ({ocr_choice}) is "
                    "not installed. Select Auto / mixed languages or "
                    "install that Tesseract language pack."
                )
            ocr_lang = ocr_choice

        return pytesseract.image_to_string(
            image, lang=ocr_lang
        ).strip()


@app.get("/")
def login_page():
    if "user_id" in session:
        return redirect("/home.html")
    return send_from_directory(app.root_path, "login.html")


@app.get("/register.html")
def register_page():
    return send_from_directory(app.root_path, "register.html")


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


@app.route("/api/voices")
def voices():
    language = request.args.get("language", "")
    if language not in LANGUAGES:
        return jsonify({"error": "Choose a supported output language."}), 400

    try:
        available_voices = get_edge_voices(language)
    except Exception:
        app.logger.exception("Could not load available speech voices.")
        return jsonify(
            {"error": "Could not load voices. Check your internet connection."}
        ), 503

    if not available_voices:
        return jsonify(
            {"error": "No speech voices are available for this language."}
        ), 404
    return jsonify(available_voices)


@app.route("/api/voices/preview")
def voice_preview():
    voice_name = request.args.get("voice", "")
    voice_style = request.args.get("style", "natural")
    language = request.args.get("language", "")
    if language not in LANGUAGES:
        return jsonify({"error": "Choose a supported output language."}), 400
    if voice_style not in {"natural", "robot"}:
        return jsonify({"error": "Choose a supported voice style."}), 400

    try:
        voice_exists = any(
            voice["name"] == voice_name
            for voice in get_edge_voices(language)
        )
        if not voice_exists:
            return jsonify({"error": "Choose an available voice."}), 400
        audio = generate_voice_preview(voice_name, voice_style, language)
    except Exception:
        app.logger.exception("Could not generate a voice preview.")
        return jsonify(
            {"error": "Could not generate the voice preview. Try again."}
        ), 502

    return Response(
        audio,
        mimetype="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.route("/home.html", methods=["GET", "POST"])
def home():
    if "user_id" not in session:
        return redirect("/")

    result = None
    error = None
    ocr_languages = get_installed_ocr_languages()

    if request.method == "POST":
        mode = request.form.get("mode", "")
        source_language = request.form.get("source_language", "auto")
        target_language = request.form.get("target_language", "en")
        selected_voice = request.form.get("voice", "")
        voice_style = request.form.get("voice_style", "natural")

        if (
            source_language != "auto"
            and source_language not in LANGUAGES
        ):
            error = "Please select a valid input language."
        elif target_language not in LANGUAGES:
            error = "Please select a valid output language."
        elif voice_style not in {"natural", "robot"}:
            error = "Please select a valid voice style."

        else:
            input_text = ""

            if mode == "text":
                input_text = request.form.get("text", "").strip()

            elif mode == "image":
                uploaded = request.files.get("image")
                ocr_choice = request.form.get(
                    "ocr_language", "auto"
                )

                valid_ocr_choices = {"auto", *ocr_languages.keys()}

                if ocr_choice not in valid_ocr_choices:
                    error = "Please select a valid image language."

                elif not uploaded or not uploaded.filename:
                    error = "Please upload an image."

                else:
                    safe_name = secure_filename(uploaded.filename)
                    if (
                        "." not in safe_name
                        or safe_name.rsplit(".", 1)[-1].lower()
                        not in ALLOWED_EXTENSIONS
                    ):
                        error = "Upload a PNG, JPG, WEBP or BMP image."
                    else:
                        image_path = (
                            UPLOAD_DIR
                            / f"{uuid.uuid4().hex}."
                            f"{safe_name.rsplit('.', 1)[-1].lower()}"
                        )
                        try:
                            uploaded.save(str(image_path))
                            input_text = read_image(
                                image_path, ocr_choice
                            )
                        except OCRLanguageUnavailableError as exc:
                            error = str(exc)
                        except pytesseract.TesseractNotFoundError:
                            app.logger.exception(
                                "Tesseract executable was not found."
                            )
                            error = (
                                "Tesseract OCR was not found. Install it "
                                "or set the TESSERACT_CMD environment "
                                "variable to the full path of tesseract.exe."
                            )
                        except pytesseract.TesseractError:
                            app.logger.exception("Tesseract OCR failed.")
                            error = (
                                "Tesseract could not recognize this image. "
                                "Check that the selected language pack is "
                                "installed and the image is clear."
                            )
                        except Image.UnidentifiedImageError:
                            error = (
                                "This file is not a readable image. "
                                "Try a PNG, JPG, WEBP or BMP image."
                            )
                        except OSError:
                            app.logger.exception(
                                "Could not open the uploaded image."
                            )
                            error = (
                                "Could not open this image. Try saving it "
                                "as a PNG or JPG and upload it again."
                            )
                        finally:
                            image_path.unlink(missing_ok=True)
            else:
                error = "Choose text or image input."

            if not error:
                if not input_text:
                    error = "No text was found. Enter text or try a clearer image."
                elif len(input_text) > 5000:
                    error = "Please use no more than 5,000 characters."
                else:
                    detected_language = (
                        source_language
                        if source_language != "auto"
                        else detect_language(input_text)
                    )

                    try:
                        translated_text, used_backup = translate_text(
                            input_text, detected_language, target_language
                        )
                        if not translated_text:
                            raise ValueError("Translation returned no text.")

                        if len(translated_text) > 5000:
                            raise ValueError(
                                "Translated text is too long."
                            )
                    except TranslationRateLimitError as exc:
                        app.logger.warning("Translation unavailable: %s", exc)
                        error = str(exc)
                    except RuntimeError as exc:
                        error = str(exc)
                    except Exception:
                        app.logger.exception("Text translation failed.")
                        error = (
                            "Translation failed. Check your internet "
                            "connection and selected language, then try again."
                        )

                    if not error:
                        try:
                            available_voices = get_edge_voices(target_language)
                            valid_voices = {
                                voice["name"] for voice in available_voices
                            }
                            if not selected_voice and available_voices:
                                selected_voice = available_voices[0]["name"]
                            if selected_voice not in valid_voices:
                                raise ValueError(
                                    "Select an available voice for the "
                                    "chosen language."
                                )
                            audio_filename = generate_voice(
                                translated_text,
                                selected_voice,
                                voice_style
                            )
                        except ValueError as exc:
                            error = str(exc)
                        except Exception:
                            app.logger.exception("Voice generation failed.")
                            error = (
                                "Could not load or generate the selected "
                                "voice. Check your internet connection and "
                                "try again."
                            )

                    if not error:
                        result = {
                            "original_text": input_text,
                            "detected_language": LANGUAGES.get(
                                detected_language,
                                "Unknown / could not detect"
                            ),
                            "target_language": LANGUAGES[target_language],
                            "voice": selected_voice,
                            "voice_style": voice_style,
                            "translated_text": translated_text,
                            "used_backup": used_backup,
                            "audio_url": url_for(
                                "static",
                                filename=f"audio/{audio_filename}"
                            ),
                        }

    return render_template(
        "index.html",
        languages=LANGUAGES,
        ocr_languages=ocr_languages,
        result=result,
        error=error
    )


@app.errorhandler(413)
def file_too_large(error):
    return "Upload too large. The limit is 10 MB.", 413


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=get_server_port(),
        debug=False,
    )