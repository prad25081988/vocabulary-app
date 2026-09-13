from flask import Flask, request, jsonify, send_from_directory, redirect, url_for
import random
import string
import bcrypt
import jwt
import os
import requests
import json
import re
import psycopg2
import psycopg2.extras
from functools import wraps
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import date

load_dotenv()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ['FLASK_SECRET']
SECRET = os.environ['OTP_SECRET']
otp_store = {}

# ---------- DAILY WORDS ----------
# Primary source: the word bank of PRIMARY_USER_EMAIL (words they've added themselves).
# Backup source: words_list.txt (used only when the primary account doesn't have
# enough words yet). As soon as the account has 5+ words, it switches back to
# using the account automatically — no manual toggle needed.
PRIMARY_USER_EMAIL = 'prad25081988@gmail.com'
WORDS_FILE_PATH = os.path.join(os.path.dirname(__file__), 'words_list.txt')
daily_words_cache = {'date': None, 'words': []}

def get_primary_user_id():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT id FROM users WHERE email = %s', (PRIMARY_USER_EMAIL,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user['id'] if user else None

def get_primary_user_word_count(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM words WHERE user_id = %s', (user_id,))
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count

def pick_and_mark_daily_words(user_id, group_size, today):
    # Words never shown (last_shown_date IS NULL) are treated as "oldest" and
    # always come first, so newly added words always take priority over
    # anything that has already been shown. Once every word has a date, this
    # naturally rotates to the least-recently-shown ones, giving a full
    # no-repeat cycle before anything repeats.
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('''
        SELECT id, word, meaning, example FROM words
        WHERE user_id = %s
        ORDER BY last_shown_date ASC NULLS FIRST, id ASC
        LIMIT %s
    ''', (user_id, group_size))
    picks = cur.fetchall()
    ids = [p['id'] for p in picks]
    if ids:
        cur2 = conn.cursor()
        cur2.execute('UPDATE words SET last_shown_date = %s WHERE id = ANY(%s)', (today, ids))
        conn.commit()
        cur2.close()
    cur.close()
    conn.close()
    return [dict(p) for p in picks]

def load_words_from_file():
    try:
        with open(WORDS_FILE_PATH, 'r') as f:
            words = [line.strip() for line in f if line.strip()]
        return words
    except Exception as e:
        print('words_list.txt read error:', str(e))
        return []

def clean_mw_markup(text):
    if not text:
        return text
    text = re.sub(r'\{it\}|\{/it\}|\{b\}|\{/b\}|\{inf\}|\{/inf\}|\{sup\}|\{/sup\}|\{phrase\}|\{/phrase\}|\{wi\}|\{/wi\}', '', text)
    text = re.sub(r'\{sx\|([^|}]+)\|[^}]*\}', r'\1', text)
    text = re.sub(r'\{a_link\|([^}]+)\}', r'\1', text)
    text = re.sub(r'\{d_link\|([^|}]+)\|[^}]*\}', r'\1', text)
    text = re.sub(r'\{dx[^}]*\}.*?\{/dx\}', '', text)
    text = re.sub(r'\{[^}]*\}', '', text)
    return text.strip()

def extract_mw_examples(def_list):
    examples = []
    def walk(node):
        if isinstance(node, dict):
            if 'vis' in node and isinstance(node['vis'], list):
                for v in node['vis']:
                    if isinstance(v, dict) and v.get('t'):
                        examples.append(clean_mw_markup(v['t']))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(def_list)
    return examples

def build_mw_audio_url(filename):
    if not filename:
        return None
    if filename.startswith('bix'):
        subdir = 'bix'
    elif filename.startswith('gg'):
        subdir = 'gg'
    elif not filename[0].isalpha():
        subdir = 'number'
    else:
        subdir = filename[0]
    return f'https://media.merriam-webster.com/audio/prons/en/us/mp3/{subdir}/{filename}.mp3'

def fetch_mw_dict(word, dict_slug, api_key):
    if not api_key:
        return None, False
    for t in (6, 8):
        try:
            resp = requests.get(
                f'https://www.dictionaryapi.com/api/v3/references/{dict_slug}/json/{word}',
                params={'key': api_key}, timeout=t
            )
            if resp.status_code == 200:
                return resp.json(), False
            return None, False
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f'Merriam-Webster ({dict_slug}) fetch error:', str(e))
            return None, False
    return None, True

def fetch_mw_raw(word):
    collegiate_key = os.environ.get('MERRIAM_WEBSTER_API_KEY')
    intermediate_key = os.environ.get('MERRIAM_WEBSTER_INTERMEDIATE_KEY')

    data, timed_out = fetch_mw_dict(word, 'collegiate', collegiate_key)
    if data and isinstance(data[0], dict):
        return data, False

    data2, timed_out2 = fetch_mw_dict(word, 'sd3', intermediate_key)
    if data2 and isinstance(data2[0], dict):
        return data2, False

    if timed_out or timed_out2:
        return None, True

    # Neither dictionary had a direct entry - prefer whichever gave spelling
    # suggestions (a list of strings) so the caller can still offer them.
    if data:
        return data, False
    if data2:
        return data2, False
    return None, False

def parse_mw_entries(data, used_word, original_word):
    phonetic = None
    audio = None
    meanings_out = []

    for entry in data:
        if not isinstance(entry, dict):
            continue
        hwi = entry.get('hwi', {})
        prs = hwi.get('prs', [])
        if prs:
            if not phonetic and prs[0].get('mw'):
                phonetic = prs[0].get('mw')
            if not audio and prs[0].get('sound', {}).get('audio'):
                audio = build_mw_audio_url(prs[0]['sound']['audio'])

        pos = entry.get('fl', '')
        shortdefs = [clean_mw_markup(sd) for sd in entry.get('shortdef', [])]
        examples_all = extract_mw_examples(entry.get('def', []))

        defs_out = []
        for i, sd in enumerate(shortdefs[:5]):
            defs_out.append({
                'definition': sd,
                'example': examples_all[i] if i < len(examples_all) else '',
                'synonyms': [],
                'antonyms': []
            })
        if defs_out:
            meanings_out.append({'partOfSpeech': pos, 'definitions': defs_out, 'synonyms': [], 'antonyms': []})

    note = None
    if used_word.lower() != original_word.lower():
        note = f'No exact entry for "{original_word}" — showing results for "{used_word}".'

    return {
        'word': original_word.capitalize(),
        'found': bool(meanings_out),
        'timed_out': False,
        'phonetic': phonetic,
        'audio': audio,
        'origin': None,
        'meanings': meanings_out,
        'note': note
    }

def fetch_word_definition(word):
    data, timed_out = fetch_mw_raw(word)
    if timed_out or not data:
        return None

    if isinstance(data[0], str):
        suggestion = data[0]
        data2, _ = fetch_mw_raw(suggestion)
        if data2 and isinstance(data2[0], dict):
            data = data2
        else:
            return None

    entry = data[0]
    if not isinstance(entry, dict):
        return None

    shortdefs = entry.get('shortdef', [])
    if not shortdefs:
        return None

    meaning = clean_mw_markup(shortdefs[0])
    examples = extract_mw_examples(entry.get('def', []))
    example = clean_mw_markup(examples[0]) if examples else None
    if not example:
        clean_meaning = meaning.rstrip('.').lower()
        example = f'"{word.capitalize()}" means {clean_meaning}.'
    return {'word': word.capitalize(), 'meaning': meaning, 'example': example}

def get_cached_word_details(word_key):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT phonetic, audio, origin, meanings, note FROM word_details_cache WHERE word = %s', (word_key,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return {
        'phonetic': row['phonetic'],
        'audio': row['audio'],
        'origin': row['origin'],
        'meanings': json.loads(row['meanings']) if row['meanings'] else [],
        'note': row['note']
    }

def save_cached_word_details(word_key, phonetic, audio, origin, meanings, note):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO word_details_cache (word, phonetic, audio, origin, meanings, note)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (word) DO UPDATE SET
                phonetic = EXCLUDED.phonetic,
                audio = EXCLUDED.audio,
                origin = EXCLUDED.origin,
                meanings = EXCLUDED.meanings,
                note = EXCLUDED.note
        ''', (word_key, phonetic, audio, origin, json.dumps(meanings), note))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print('save_cached_word_details error:', str(e))

def save_word_example(word_id, example):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE words SET example = %s WHERE id = %s', (example, word_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print('save_word_example error:', str(e))

def get_daily_words():
    today = date.today()
    if daily_words_cache['date'] == today and daily_words_cache['words']:
        return daily_words_cache['words']

    group_size = 5
    day_index = today.toordinal()
    result = []

    # ---- Primary source: the user's own word bank ----
    # Meaning always comes from the user's own database entry (never overwritten).
    # Example sentence: if we've already fetched one for this word before, reuse
    # it from the database instead of calling the dictionary API again. This is
    # what keeps daily words fast and avoids repeated slow external calls (and
    # the server timeouts that can come from an unreliable third-party API).
    user_id = get_primary_user_id()
    if user_id and get_primary_user_word_count(user_id) >= group_size:
        picks = pick_and_mark_daily_words(user_id, group_size, today)
        for p in picks:
            if p.get('example'):
                example = p['example']
            else:
                dict_info = fetch_word_definition(p['word'])
                example = dict_info['example'] if dict_info and dict_info.get('example') else None
                if not example:
                    clean_meaning = p['meaning'].rstrip('.').lower()
                    example = f'"{p["word"].capitalize()}" means {clean_meaning}.'
                save_word_example(p['id'], example)
            result.append({
                'word': p['word'].capitalize(),
                'meaning': p['meaning'],
                'example': example
            })

    # ---- Backup source: words_list.txt, only fills whatever is still short ----
    if len(result) < group_size:
        needed = group_size - len(result)
        notepad_words = load_words_from_file()
        if notepad_words:
            total_groups = max(len(notepad_words) // group_size, 1)
            n_index = day_index % total_groups
            fallback_picks = notepad_words[n_index * group_size: n_index * group_size + needed]
            if len(fallback_picks) < needed:
                fallback_picks += notepad_words[:needed - len(fallback_picks)]
            for w in fallback_picks:
                info = fetch_word_definition(w)
                if info:
                    result.append(info)
                else:
                    result.append({
                        'word': w.capitalize(),
                        'meaning': 'Definition not found — check the spelling in words_list.txt',
                        'example': ''
                    })

    daily_words_cache['date'] = today
    daily_words_cache['words'] = result
    return result

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

def get_db():
    conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            phone TEXT UNIQUE,
            email TEXT UNIQUE,
            password TEXT,
            auth_provider TEXT DEFAULT 'local'
        )
    ''')
    # Safe upgrades for a table that already existed before Google login was added
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT UNIQUE")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider TEXT DEFAULT 'local'")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS practice_session_size INTEGER DEFAULT 20")
    cur.execute("ALTER TABLE users ALTER COLUMN password DROP NOT NULL")
    cur.execute("ALTER TABLE users ALTER COLUMN phone DROP NOT NULL")
    cur.execute('''
        CREATE TABLE IF NOT EXISTS words (
            id SERIAL PRIMARY KEY,
            word TEXT NOT NULL,
            meaning TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id)
        )
    ''')
    cur.execute("ALTER TABLE words ADD COLUMN IF NOT EXISTS last_shown_date DATE")
    cur.execute("ALTER TABLE words ADD COLUMN IF NOT EXISTS shown_count INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE words ADD COLUMN IF NOT EXISTS example TEXT")
    cur.execute('''
        CREATE TABLE IF NOT EXISTS word_details_cache (
            word TEXT PRIMARY KEY,
            phonetic TEXT,
            audio TEXT,
            origin TEXT,
            meanings TEXT,
            note TEXT,
            cached_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

init_db()

def authenticate(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'error': 'No token'}), 401
        try:
            data = jwt.decode(token, SECRET, algorithms=['HS256'])
            request.user = data
        except:
            return jsonify({'error': 'Invalid token'}), 403
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def home():
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'public'), 'index.html')

@app.route('/manifest.json')
def manifest():
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'public'), 'manifest.json')

@app.route('/service-worker.js')
def service_worker():
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'public'), 'service-worker.js')

@app.route('/icon.png')
def icon():
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'public'), 'icon.png')

# ---------- GOOGLE LOGIN ----------

@app.route('/login/google')
def google_login():
    redirect_uri = url_for('google_callback', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/login/google/callback')
def google_callback():
    token = google.authorize_access_token()
    user_info = token.get('userinfo')
    if not user_info or not user_info.get('email'):
        return redirect('/?error=google_login_failed')

    email = user_info['email']

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM users WHERE email = %s', (email,))
    user = cur.fetchone()
    if not user:
        cur.execute(
            'INSERT INTO users (email, auth_provider) VALUES (%s, %s) RETURNING *',
            (email, 'google')
        )
        user = cur.fetchone()
        conn.commit()
    cur.close()
    conn.close()

    jwt_token = jwt.encode({'id': user['id'], 'identifier': email}, SECRET, algorithm='HS256')
    return redirect(f'/?token={jwt_token}&identifier={email}')

# ---------- EXISTING PHONE/PASSWORD LOGIN (unchanged, still works) ----------

@app.route('/api/send-otp', methods=['POST'])
def send_otp_route():
    data = request.json
    phone = data['phone']
    if len(phone) != 10 or not phone.isdigit():
        return jsonify({'error': 'Enter valid 10 digit phone number'}), 400
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM users WHERE phone = %s', (phone,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if user:
        return jsonify({'error': 'Phone number already registered'}), 400
    otp = ''.join(random.choices(string.digits, k=6))
    otp_store[phone] = otp
    return jsonify({'message': f'OTP (Testing mode): {otp}'})

@app.route('/api/verify-register', methods=['POST'])
def verify_register():
    data = request.json
    phone = data['phone']
    otp = data['otp']
    password = data['password']
    if phone not in otp_store or otp_store[phone] != otp:
        return jsonify({'error': 'Invalid or expired OTP'}), 400
    del otp_store[phone]
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO users (phone, password, auth_provider) VALUES (%s, %s, %s)',
            (phone, hashed.decode('utf-8'), 'local')
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'message': 'Registered successfully'})
    except Exception as e:
        print("Register error:", str(e))
        return jsonify({'error': 'Phone number already registered'}), 400

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    phone = data['phone']
    password = data['password']
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM users WHERE phone = %s', (phone,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user or not user['password']:
        return jsonify({'error': 'Phone number not registered'}), 400
    if not bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
        return jsonify({'error': 'Invalid password'}), 400
    token = jwt.encode({'id': user['id'], 'identifier': phone}, SECRET, algorithm='HS256')
    return jsonify({'token': token, 'identifier': phone})

@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    data = request.json
    phone = data['phone']
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM users WHERE phone = %s', (phone,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user:
        return jsonify({'error': 'Phone number not registered'}), 400
    otp = ''.join(random.choices(string.digits, k=6))
    otp_store[phone] = otp
    return jsonify({'message': f'OTP (Testing mode): {otp}'})

@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.json
    phone = data['phone']
    otp = data['otp']
    password = data['password']
    if phone not in otp_store or otp_store[phone] != otp:
        return jsonify({'error': 'Invalid or expired OTP'}), 400
    del otp_store[phone]
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE users SET password = %s WHERE phone = %s', (hashed.decode('utf-8'), phone))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'Password reset successfully'})

# ---------- WORDS (unchanged, plus new PUT edit endpoint) ----------

@app.route('/api/daily-words', methods=['GET'])
def daily_words():
    return jsonify(get_daily_words())

@app.route('/api/words', methods=['GET'])
@authenticate
def get_words():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM words WHERE user_id = %s', (request.user['id'],))
    words = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(w) for w in words])

@app.route('/api/words', methods=['POST'])
@authenticate
def add_word():
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT INTO words (word, meaning, user_id) VALUES (%s, %s, %s) RETURNING id',
                (data['word'], data['meaning'], request.user['id']))
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'Word added successfully', 'id': new_id})

@app.route('/api/words/<int:id>', methods=['DELETE'])
@authenticate
def delete_word(id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM words WHERE id = %s AND user_id = %s', (id, request.user['id']))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'Word deleted successfully'})

@app.route('/api/words/<int:id>', methods=['PUT'])
@authenticate
def update_word(id):
    data = request.json
    word = data.get('word', '').strip()
    meaning = data.get('meaning', '').strip()
    if not word or not meaning:
        return jsonify({'error': 'Word and meaning are required'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE words SET word = %s, meaning = %s WHERE id = %s AND user_id = %s',
                (word, meaning, id, request.user['id']))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'Word updated successfully'})

@app.route('/api/settings/practice-size', methods=['GET'])
@authenticate
def get_practice_size():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT practice_session_size FROM users WHERE id = %s', (request.user['id'],))
    row = cur.fetchone()
    cur.close()
    conn.close()
    size = row['practice_session_size'] if row and row['practice_session_size'] else 20
    return jsonify({'session_size': size})

@app.route('/api/settings/practice-size', methods=['PUT'])
@authenticate
def update_practice_size():
    data = request.json
    size = data.get('session_size')
    if not isinstance(size, int) or size <= 0:
        return jsonify({'error': 'Session size must be a positive number'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE users SET practice_session_size = %s WHERE id = %s', (size, request.user['id']))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'Practice session size updated', 'session_size': size})

def merge_mw_meanings(word):
    collegiate_key = os.environ.get('MERRIAM_WEBSTER_API_KEY')
    intermediate_key = os.environ.get('MERRIAM_WEBSTER_INTERMEDIATE_KEY')

    collegiate_data, timeout1 = fetch_mw_dict(word, 'collegiate', collegiate_key)
    intermediate_data, timeout2 = fetch_mw_dict(word, 'sd3', intermediate_key)
    timed_out = timeout1 or timeout2

    sources = []
    if collegiate_data and isinstance(collegiate_data[0], dict):
        sources.append(collegiate_data)
    if intermediate_data and isinstance(intermediate_data[0], dict):
        sources.append(intermediate_data)

    if not sources:
        suggestions = None
        if collegiate_data and isinstance(collegiate_data[0], str):
            suggestions = collegiate_data
        elif intermediate_data and isinstance(intermediate_data[0], str):
            suggestions = intermediate_data
        return None, timed_out, suggestions

    phonetic = None
    audio = None
    meanings_by_pos = {}
    seen_defs = set()

    for data in sources:
        for entry in data:
            if not isinstance(entry, dict):
                continue
            hwi = entry.get('hwi', {})
            prs = hwi.get('prs', [])
            if prs:
                if not phonetic and prs[0].get('mw'):
                    phonetic = prs[0].get('mw')
                if not audio and prs[0].get('sound', {}).get('audio'):
                    audio = build_mw_audio_url(prs[0]['sound']['audio'])

            pos = entry.get('fl', '') or 'other'
            shortdefs = [clean_mw_markup(sd) for sd in entry.get('shortdef', [])]
            examples_all = extract_mw_examples(entry.get('def', []))

            if pos not in meanings_by_pos:
                meanings_by_pos[pos] = {'partOfSpeech': pos, 'definitions': [], 'synonyms': [], 'antonyms': []}

            for i, sd in enumerate(shortdefs):
                if len(meanings_by_pos[pos]['definitions']) >= 6:
                    break
                dedup_key = (pos, sd.lower().strip())
                if dedup_key in seen_defs:
                    continue
                seen_defs.add(dedup_key)
                meanings_by_pos[pos]['definitions'].append({
                    'definition': sd,
                    'example': examples_all[i] if i < len(examples_all) else '',
                    'synonyms': [],
                    'antonyms': []
                })

    meanings_out = [m for m in meanings_by_pos.values() if m['definitions']]

    # Every definition gets its own example. Real dictionary examples are used
    # first; when the dictionaries themselves didn't supply one for a
    # particular sense, a simple sentence built from that definition fills the
    # gap so nothing in the popup is left blank.
    for m in meanings_out:
        for d in m['definitions']:
            if not d['example']:
                clean_def = d['definition'].rstrip('.').lower()
                d['example'] = f'"{word.capitalize()}" means {clean_def}.'

    return {'phonetic': phonetic, 'audio': audio, 'meanings': meanings_out}, timed_out, None

@app.route('/api/word-details', methods=['GET'])
@authenticate
def word_details():
    word = request.args.get('word', '').strip()
    if not word:
        return jsonify({'error': 'Word is required'}), 400

    word_key = word.lower()

    cached = get_cached_word_details(word_key)
    if cached:
        return jsonify({
            'word': word.capitalize(),
            'found': True,
            'timed_out': False,
            'phonetic': cached['phonetic'],
            'audio': cached['audio'],
            'origin': cached['origin'],
            'meanings': cached['meanings'],
            'note': cached['note']
        })

    if not os.environ.get('MERRIAM_WEBSTER_API_KEY') and not os.environ.get('MERRIAM_WEBSTER_INTERMEDIATE_KEY'):
        return jsonify({
            'word': word.capitalize(),
            'found': False,
            'timed_out': False,
            'phonetic': None,
            'audio': None,
            'origin': None,
            'meanings': [],
            'note': 'Dictionary lookup is not configured yet (missing API key).'
        })

    merged, timed_out, suggestions = merge_mw_meanings(word)

    if not merged:
        if suggestions:
            first = suggestions[0]
            merged2, timed_out2, _ = merge_mw_meanings(first)
            if merged2:
                merged = merged2
                merged['note'] = f'No exact entry for "{word}" — showing results for "{first}".'
            else:
                return jsonify({
                    'word': word.capitalize(),
                    'found': False,
                    'timed_out': timed_out2,
                    'phonetic': None,
                    'audio': None,
                    'origin': None,
                    'meanings': [],
                    'note': f'No exact entry for "{word}". Did you mean: {", ".join(suggestions[:5])}?'
                })
        else:
            return jsonify({
                'word': word.capitalize(),
                'found': False,
                'timed_out': timed_out,
                'phonetic': None,
                'audio': None,
                'origin': None,
                'meanings': [],
                'note': None
            })

    result = {
        'word': word.capitalize(),
        'found': bool(merged['meanings']),
        'timed_out': False,
        'phonetic': merged['phonetic'],
        'audio': merged['audio'],
        'origin': None,
        'meanings': merged['meanings'],
        'note': merged.get('note')
    }

    if result['found']:
        save_cached_word_details(word_key, result['phonetic'], result['audio'], result['origin'], result['meanings'], result['note'])

    return jsonify(result)

@app.route('/api/practice', methods=['GET'])
@authenticate
def practice():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT practice_session_size FROM users WHERE id = %s', (request.user['id'],))
    row = cur.fetchone()
    cur.close()
    conn.close()
    session_size = row['practice_session_size'] if row and row['practice_session_size'] else 20
    words_list = get_practice_session(request.user['id'], session_size=session_size)
    random.shuffle(words_list)
    return jsonify(words_list)

def get_practice_session(user_id, session_size=20):
    # Each practice session mixes two groups so that words needing more
    # practice show up more often, without ever fully freezing out older
    # or already-practiced words:
    #   - 70% of the session: the least-shown words (shown_count ASC), so
    #     brand new words (shown_count=0) and under-practiced ones surface first.
    #   - 30% of the session: a genuinely random sample from the ENTIRE word
    #     bank, regardless of shown_count, so well-practiced words still get
    #     periodically refreshed instead of disappearing from rotation forever.
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT COUNT(*) AS cnt FROM words WHERE user_id = %s', (user_id,))
    total = cur.fetchone()['cnt']
    size = min(session_size, total)
    if size == 0:
        cur.close()
        conn.close()
        return []

    priority_count = min(max(round(size * 0.7), 1), size)
    random_count = size - priority_count

    cur.execute('''
        SELECT id, word, meaning, shown_count FROM words
        WHERE user_id = %s
        ORDER BY shown_count ASC, id ASC
        LIMIT %s
    ''', (user_id, priority_count))
    priority_words = cur.fetchall()
    priority_ids = [w['id'] for w in priority_words]

    remaining_words = []
    if random_count > 0:
        if priority_ids:
            cur.execute('''
                SELECT id, word, meaning, shown_count FROM words
                WHERE user_id = %s AND id != ALL(%s)
                ORDER BY RANDOM()
                LIMIT %s
            ''', (user_id, priority_ids, random_count))
        else:
            cur.execute('''
                SELECT id, word, meaning, shown_count FROM words
                WHERE user_id = %s
                ORDER BY RANDOM()
                LIMIT %s
            ''', (user_id, random_count))
        remaining_words = cur.fetchall()

    combined = list(priority_words) + list(remaining_words)
    combined_ids = [w['id'] for w in combined]

    if combined_ids:
        cur2 = conn.cursor()
        cur2.execute('UPDATE words SET shown_count = shown_count + 1 WHERE id = ANY(%s)', (combined_ids,))
        conn.commit()
        cur2.close()

    cur.close()
    conn.close()
    return [dict(w) for w in combined]

if __name__ == '__main__':
    app.run(port=5000)