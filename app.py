from flask import Flask, request, jsonify, send_from_directory, redirect, url_for
import random
import string
import bcrypt
import jwt
import os
import requests
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
app.secret_key = os.environ.get('FLASK_SECRET', 'someflasksecretkey123')
SECRET = 'vocabsecretkey123'
otp_store = {}

# ---------- DAILY WORDS: READ FROM words_list.txt, AUTO-FETCH MEANING + EXAMPLE ----------
# To add or change words: just edit words_list.txt (one word per line) and push.
# No Python code changes needed. Meanings and examples are looked up automatically.
WORDS_FILE_PATH = os.path.join(os.path.dirname(__file__), 'words_list.txt')
daily_words_cache = {'date': None, 'words': []}

def load_words_from_file():
    try:
        with open(WORDS_FILE_PATH, 'r') as f:
            words = [line.strip() for line in f if line.strip()]
        return words
    except Exception as e:
        print('words_list.txt read error:', str(e))
        return []

def fetch_word_definition(word):
    try:
        resp = requests.get(f'https://api.dictionaryapi.dev/api/v2/entries/en/{word.lower()}', timeout=6)
        if resp.status_code != 200:
            return None
        data = resp.json()
        meanings = data[0].get('meanings', [])
        if not meanings:
            return None
        definitions = meanings[0].get('definitions', [])
        if not definitions:
            return None
        meaning = definitions[0].get('definition', '')
        example = definitions[0].get('example', '')
        if not meaning:
            return None
        if not example:
            example = f'Try using "{word}" in a sentence of your own!'
        return {'word': word.capitalize(), 'meaning': meaning, 'example': example}
    except Exception:
        return None

def get_daily_words():
    today = date.today()
    if daily_words_cache['date'] == today and daily_words_cache['words']:
        return daily_words_cache['words']

    all_words = load_words_from_file()
    if not all_words:
        return []

    group_size = 5
    total_groups = max(len(all_words) // group_size, 1)
    day_index = today.toordinal() % total_groups
    picks = all_words[day_index * group_size: day_index * group_size + group_size]
    if len(picks) < group_size:
        picks += all_words[:group_size - len(picks)]

    result = []
    for w in picks:
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

# ---------- WORDS (unchanged) ----------

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

@app.route('/api/practice', methods=['GET'])
@authenticate
def practice():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM words WHERE user_id = %s', (request.user['id'],))
    words = cur.fetchall()
    cur.close()
    conn.close()
    words_list = [dict(w) for w in words]
    random.shuffle(words_list)
    return jsonify(words_list)

if __name__ == '__main__':
    app.run(port=5000)